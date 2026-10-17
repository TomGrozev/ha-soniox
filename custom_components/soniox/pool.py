"""Warm Soniox TTS WebSocket pool (issue #8).

Holds ONE idle *warm* TTS connection per config entry — opened speculatively
from STT start (a TTS response is imminent) and held with keepalive frames
across a conversation — so a TTS request reuses it instead of paying the
DNS/TCP/TLS/WS-upgrade handshake. A bare ``tts.speak`` with no preceding STT
still cold-connects (issue #8 non-goal).

Lifecycle (https://soniox.com/docs/tts/rt/streams and
https://soniox.com/docs/tts/rt/connection-keepalive):
- ``warm`` conn born from ``async_warm()`` survives its own stream (keepalives
  continue) and is *allowed* to die on Soniox's ~3-minute no-audio timer; it
  is re-warmed on the next STT start — there are no reconnect/retry loops.
- ``borrowed`` conn born from a request that finds no usable warm conn is
  closed after its stream ends and is never promoted to warm.
- Each connection runs ONE recv-loop task demuxing incoming frames by
  ``stream_id`` into per-stream queues; outgoing frames are serialized with a
  per-conn lock (concurrent aiohttp ``send_json`` on one socket is not
  guaranteed safe).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .audio_join import StreamJoiner
from .const import (
    CONF_API_KEY,
    CONF_TTS_AUDIO_FORMAT,
    CONF_TTS_LANGUAGE,
    CONF_TTS_MODEL,
    CONF_TTS_SAMPLE_RATE,
    CONF_TTS_VOICE,
    DEFAULT_TTS_AUDIO_FORMAT,
    DEFAULT_TTS_LANGUAGE,
    DEFAULT_TTS_MODEL,
    DEFAULT_TTS_SAMPLE_RATE,
    DEFAULT_TTS_VOICE,
)

if TYPE_CHECKING:
    from . import SonioxConfigEntry

_LOGGER = logging.getLogger(__name__)

# Keepalive cadence for an idle warm conn. Soniox bills only for synthesized
# work, so these frames cost nothing; 25s sits inside the documented 20–30s
# window (https://soniox.com/docs/tts/rt/connection-keepalive,
# https://soniox.com/pricing). Keepalives reset Soniox's idle-connection timer
# but NOT the separate ~3-minute no-audio timer, so an idle conn still closes
# naturally once the conversation goes quiet and is re-warmed at the next STT
# start — the client never holds it open with its own timers.
KEEPALIVE_INTERVAL = 25

# Soniox accepts up to 5 concurrent streams per connection; past that an
# additional connection is dialed rather than erroring on a 6th stream
# (https://soniox.com/docs/tts/rt/streams).
MAX_STREAMS_PER_CONN = 5

# Seconds of text silence on a stream that has already received text before we
# end it cleanly. Soniox retires a stream that gets no text for "a few seconds"
# (undocumented; observed ~5–18 s) and offers NO per-stream keepalive; its
# documented remedy is to send text_end and open a new stream
# (https://soniox.com/docs/tts/rt/connection-keepalive). Closing at 3 s — well
# inside that window — lets an LLM pipeline pause (e.g. a tool call) without the
# stream dying mid-synthesis under our feet.
STREAM_IDLE_CLOSE = 3.0


@dataclass
class _Stream:
    """Per-stream receive state on one TTS connection."""

    stream_id: str
    warm_up: bool = False
    error: str | None = None
    queue: asyncio.Queue[bytes | None] = field(default_factory=asyncio.Queue)
    finished: bool = False
    # True once Soniox retired this stream for lack of text (a 408); the request
    # pump replays any unsynthesized text on a fresh stream.
    retired: bool = False
    # True once any audio frame was routed here, so a retired stream's text is
    # replayed only when it never reached synthesis (else speech would repeat).
    had_audio: bool = False
    # Woken when this stream ends (cleanly, by retirement, or by conn failure)
    # so a request pump notices a retirement even when no new text arrives.
    on_finish: Callable[[], None] | None = None

    def finish(self, error: str | None = None) -> None:
        """Terminate the stream (cleanly or with an error for its consumer)."""
        if self.finished:
            return
        self.finished = True
        if error is not None:
            self.error = error
        self.queue.put_nowait(None)
        if self.on_finish is not None:
            self.on_finish()


@dataclass
class _Segment:
    """One Soniox stream within a request, plus the pump's send-side state."""

    conn: _TTSConnection
    stream: _Stream
    sent_text: list[str] = field(default_factory=list)
    text_end_sent: bool = False
    retired_handled: bool = False
    # True when this segment was opened to carry another segment's replayed
    # text, so a second retirement of that same text fails instead of looping.
    replayed: bool = False

    @property
    def closed_for_text(self) -> bool:
        """True when no further text may go to this stream.

        Only an explicit end closes a segment to more text: our own idle close
        (``text_end_sent``), a Soniox 408 retirement, or a dead connection. A
        bare ``terminated`` frame does not — Soniox never ends a stream we have
        not text_end'ed without first reporting a 408.
        """
        return (
            self.text_end_sent
            or self.stream.retired
            or self.stream.error is not None
        )


class _TTSConnection:
    """One Soniox TTS WebSocket with a demuxing recv loop."""

    def __init__(
        self,
        pool: SonioxTTSPool,
        ws: aiohttp.ClientWebSocketResponse,
        *,
        warm: bool,
    ) -> None:
        self._pool = pool
        self.ws = ws
        # warm conns survive their streams; borrowed conns close after theirs.
        self.warm = warm
        self._streams: dict[str, _Stream] = {}
        self._send_lock = asyncio.Lock()
        self._done = asyncio.Event()
        self._dead = False
        self._recv_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._drop = False

    @property
    def alive(self) -> bool:
        return not self._dead

    @property
    def active_streams(self) -> int:
        """Live request streams (excludes the anchor warm-up and finished ones)."""
        return sum(
            1 for s in self._streams.values() if not s.warm_up and not s.finished
        )

    def start(self) -> None:
        """Spawn the recv-loop task; the conn lives outside any request scope."""
        self._recv_task = asyncio.create_task(self._run_recv())

    async def send_json(self, payload: dict[str, Any]) -> None:
        """Send one JSON frame, serialized per-conn (aiohttp send_json is not task-safe)."""
        async with self._send_lock:
            await self.ws.send_json(payload)

    async def prime_warm_up(self, options: dict[str, Any]) -> None:
        """Open this conn's anchor warm-up stream, then start keepalives.

        The warm-up stream carries a full valid per-stream config with NO text;
        Soniox retires it on its own request timeout — treated as benign, the
        conn stays warm. Keepalives only count after the first stream, so the
        anchor must exist before the keepalive loop starts
        (https://soniox.com/docs/tts/rt/connection-keepalive).
        """
        stream_id = uuid.uuid4().hex
        self._streams[stream_id] = _Stream(stream_id, warm_up=True)
        await self.send_json({**options, "stream_id": stream_id})
        self._keepalive_task = asyncio.create_task(self._run_keepalive())

    async def _run_recv(self) -> None:
        """Demux incoming frames by stream_id; tear the conn down when it dies."""
        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    payload = json.loads(msg.data)
                    self._dispatch(payload)
                    if self._drop:
                        break
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — any recv failure kills the conn
            _LOGGER.debug("Soniox TTS stream recv error: %s", err)
        finally:
            await self.close(reason="peer closed connection")

    def _dispatch(self, payload: dict[str, Any]) -> None:
        stream_id = payload.get("stream_id")
        if err_code := payload.get("error_code"):
            self._on_error_frame(
                stream_id,
                err_code,
                payload.get("error_message"),
                payload.get("error_type"),
            )
            return
        if audio_b64 := payload.get("audio"):
            audio = base64.b64decode(audio_b64)
            for stream in self._targets(stream_id):
                stream.had_audio = True
                stream.queue.put_nowait(audio)
        if payload.get("terminated") or payload.get("audio_end"):
            for stream in self._targets(stream_id):
                stream.finish()

    def _targets(self, stream_id: str | None) -> list[_Stream]:
        """Resolve a frame's destination streams.

        Soniox always tags frames with a ``stream_id``; an untagged frame only
        appears from the legacy single-stream protocol (and the test fakes), so
        route it to every live request stream to keep that contract.
        """
        if stream_id is None:
            return [s for s in self._streams.values() if not s.warm_up]
        stream = self._streams.get(stream_id)
        return [stream] if stream is not None and not stream.warm_up else []

    def _on_error_frame(
        self,
        stream_id: str | None,
        err_code: int | str,
        err_message: Any,
        err_type: Any = None,
    ) -> None:
        err_msg = err_message or str(err_code)
        stream = self._streams.get(stream_id) if stream_id is not None else None
        if stream is not None and stream.warm_up:
            # Soniox retiring the no-text anchor stream is expected, not an
            # error: debug-log and keep the warm conn alive.
            _LOGGER.debug(
                "Soniox TTS warm-up stream retired (benign): %s: %s",
                err_code,
                err_msg,
            )
            return
        if stream is not None and stream.finished:
            # A frame for a stream we already ended — e.g. the 400
            # invalid_stream_state for text that crossed a retirement. Ignore
            # it and keep the conn up.
            _LOGGER.debug(
                "Soniox TTS ignoring error for finished stream %s: %s: %s",
                stream_id,
                err_code,
                err_msg,
            )
            return
        if stream is not None and (
            str(err_code) == "408" or err_type == "request_timeout"
        ):
            # Soniox retired the stream for lack of text (no per-stream
            # keepalive). End it cleanly for the consumer — the pump opens a
            # fresh stream for any text that never reached synthesis — and keep
            # the conn alive (https://soniox.com/docs/tts/rt/connection-keepalive).
            stream.retired = True
            stream.finish()
            _LOGGER.debug(
                "Soniox TTS request stream %s retired (request timeout)", stream_id
            )
            return
        if stream is not None:
            # A request-stream protocol error: surface it to that consumer and
            # drop the conn (defensive — a fresh warm is raised at the next
            # STT start; issue #8).
            stream.finish(
                error=f"Soniox TTS streaming error {err_code}: {err_msg}"
            )
        self._drop = True

    async def _run_keepalive(self) -> None:
        """Send keepalives at KEEPALIVE_INTERVAL until the conn dies or closes."""
        try:
            while not self._done.is_set():
                try:
                    await asyncio.wait_for(
                        self._done.wait(), timeout=KEEPALIVE_INTERVAL
                    )
                except asyncio.TimeoutError:
                    await self.send_json({"keep_alive": True})
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — conn died mid-send; recv reports it
            pass

    async def close(self, reason: str = "connection closed") -> None:
        """Tear this conn down: stop keepalive, end the recv loop, wake waiters."""
        if self._dead:
            return
        self._dead = True
        self._done.set()
        current = asyncio.current_task()
        for task in (self._recv_task, self._keepalive_task):
            if task is not None and task is not current:
                task.cancel()
        self._pool._forget(self)
        self._wake_all(f"Soniox TTS streaming connection failed: {reason}")
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001 — best-effort socket close
            pass
        for task in (self._recv_task, self._keepalive_task):
            if task is not None and task is not current:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._streams.clear()

    def _wake_all(self, message: str) -> None:
        """Wake every unfinished request stream with a connection failure."""
        for stream in self._streams.values():
            if not stream.warm_up:
                stream.finish(error=message)


class _RequestSession:
    """Drive one streaming request as a sequence of Soniox streams.

    An LLM pipeline opens the TTS request before any speakable text exists and
    can pause for seconds (e.g. a tool call). Soniox retires a stream that gets
    no text for a few seconds and has NO per-stream keepalive, so a segment
    that has received text is closed cleanly after ``STREAM_IDLE_CLOSE`` and
    the next text opens a fresh stream — every segment's audio joined into the
    one container byte stream Home Assistant asked for
    (https://soniox.com/docs/tts/rt/connection-keepalive).
    """

    def __init__(
        self,
        pool: SonioxTTSPool,
        conn: _TTSConnection,
        options: dict[str, Any],
        *,
        started: float,
    ) -> None:
        self._pool = pool
        self._conn = conn
        self._options = options
        self._started = started
        self._warm = conn.warm
        self._joiner = StreamJoiner(options.get("audio_format", ""))
        self._output: asyncio.Queue[_Segment | None] = asyncio.Queue()
        self._text: asyncio.Queue[str | None] = asyncio.Queue()
        # Set whenever any of this request's streams ends, so the pump notices
        # a Soniox retirement even when no new text arrives.
        self._changed = asyncio.Event()
        self._segments: list[_Segment] = []
        self._current: _Segment | None = None
        self._gen_task: asyncio.Task[None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._error: BaseException | None = None
        self._first_text_at: float | None = None
        self._first_chunk = True

    async def open_first(self) -> None:
        """Open the pre-emptive segment at request start (config sent now)."""
        await self._open_segment()

    def start(self, message_gen: AsyncIterable[str]) -> None:
        """Spawn the text reader and the segment pump."""
        self._gen_task = asyncio.create_task(self._read_gen(message_gen))
        self._task = asyncio.create_task(self._manage())

    async def consume(self) -> AsyncGenerator[bytes]:
        """Yield each segment's audio in open order, joined into one stream."""
        while True:
            seg = await self._output.get()
            if seg is None:
                if self._error is not None:
                    raise HomeAssistantError(
                        f"Soniox TTS streaming connection failed: {self._error}"
                    ) from self._error
                return
            self._joiner.start_segment()
            while True:
                item = await seg.stream.queue.get()
                if item is None:
                    break
                data = self._joiner.feed(item)
                if data:
                    self._log_first_audio()
                    yield data
            tail = self._joiner.end_segment()
            if tail:
                self._log_first_audio()
                yield tail
            if seg.stream.error is not None:
                raise HomeAssistantError(seg.stream.error)

    async def aclose(self, *, close_all: bool = False) -> None:
        """Cancel this request's tasks, pop its streams, close borrowed conns."""
        for task in (self._task, self._gen_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._task, self._gen_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        conns = {seg.conn for seg in self._segments}
        if self._conn is not None:
            conns.add(self._conn)
        for seg in self._segments:
            seg.conn._streams.pop(seg.stream.stream_id, None)
        # Borrowed conns close only when the whole request ends; a warm conn
        # stays warm (issue #8). A dead socket at config time is closed too.
        for conn in conns:
            if close_all or not conn.warm:
                await conn.close(reason="stream finished")

    def _log_first_audio(self) -> None:
        if not self._first_chunk:
            return
        self._first_chunk = False
        now = time.monotonic()
        _LOGGER.debug(
            "Soniox TTS first audio frame %.0f ms after request, "
            "%.0f ms after first text (%s connection)",
            (now - self._started) * 1000,
            (now - (self._first_text_at or self._started)) * 1000,
            "warm" if self._warm else "cold",
        )

    # -- pump internals --------------------------------------------------

    async def _read_gen(self, message_gen: AsyncIterable[str]) -> None:
        """Forward message_gen's non-empty chunks to the pump; None at EOF.

        Reading in its own task means the pump times out on its own queue and
        never cancels a pending ``message_gen.__anext__`` (which would close
        the caller's generator).
        """
        try:
            async for chunk in message_gen:
                if chunk:
                    # Streaming LLM pipelines open the TTS request long before
                    # any speakable text exists, so first-audio latency is only
                    # meaningful measured from the first text chunk.
                    if self._first_text_at is None:
                        self._first_text_at = time.monotonic()
                    await self._text.put(chunk)
        finally:
            self._text.put_nowait(None)

    async def _manage(self) -> None:
        try:
            await self._run_manage()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — surfaced to the consumer below
            _LOGGER.debug("Soniox TTS request pump failed: %s", err)
            self._error = err
        finally:
            self._output.put_nowait(None)

    async def _run_manage(self) -> None:
        text_task: asyncio.Task[str | None] | None = asyncio.create_task(
            self._text.get()
        )
        changed_task = asyncio.create_task(self._changed.wait())
        gen_done = False
        try:
            while True:
                if gen_done and not self._any_unfinished():
                    return
                waiters: list[asyncio.Task[Any]] = [changed_task]
                if text_task is not None:
                    waiters.append(text_task)
                # Only a segment that has text and is still open can go idle.
                current = self._current
                timeout = (
                    STREAM_IDLE_CLOSE
                    if current is not None
                    and current.sent_text
                    and not current.closed_for_text
                    else None
                )
                done, _ = await asyncio.wait(
                    waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    await self._idle_close(current)
                    continue
                if changed_task in done:
                    self._changed.clear()
                    changed_task = asyncio.create_task(self._changed.wait())
                    await self._on_stream_ended()
                if text_task is not None and text_task in done:
                    chunk = text_task.result()
                    if chunk is None:
                        gen_done = True
                        text_task = None
                        await self._finalize()
                    else:
                        text_task = asyncio.create_task(self._text.get())
                        await self._on_text(chunk)
        finally:
            if text_task is not None:
                text_task.cancel()
            changed_task.cancel()

    def _any_unfinished(self) -> bool:
        return any(not seg.stream.finished for seg in self._segments)

    async def _on_stream_ended(self) -> None:
        """Handle a Soniox retirement of the segment still open for text."""
        current = self._current
        if (
            current is None
            or not current.stream.retired
            or current.retired_handled
        ):
            return
        current.retired_handled = True
        await self._on_retired(current)

    async def _open_segment(self) -> _Segment:
        """Open a fresh Soniox stream and queue it for the consumer."""
        conn = self._conn
        if (
            conn is None
            or not conn.alive
            or conn.active_streams >= MAX_STREAMS_PER_CONN
        ):
            conn = await self._pool._claim()
            self._conn = conn
        stream_id = uuid.uuid4().hex
        stream = _Stream(stream_id)
        stream.on_finish = self._changed.set
        conn._streams[stream_id] = stream
        seg = _Segment(conn=conn, stream=stream)
        self._segments.append(seg)
        try:
            await conn.send_json({**self._options, "stream_id": stream_id})
        except Exception:
            conn._streams.pop(stream_id, None)
            self._segments.remove(seg)
            raise
        self._current = seg
        await self._output.put(seg)
        _LOGGER.debug(
            "Soniox TTS opened stream %s (%s connection)",
            stream_id,
            "warm" if conn.warm else "cold",
        )
        return seg

    async def _on_text(self, chunk: str) -> None:
        seg = self._current
        if seg is None or seg.closed_for_text:
            seg = await self._open_next_segment(seg)
        await self._send_text(seg, chunk)

    async def _on_retired(self, seg: _Segment | None) -> None:
        """Soniox retired a stream; replay its text if it never spoke."""
        if seg is None:
            return
        _LOGGER.debug("Soniox TTS stream %s retired by server", seg.stream.stream_id)
        if self._current is seg:
            self._current = None
        if seg.stream.had_audio or not seg.sent_text:
            # Already spoke (replaying would repeat speech) or carried no text.
            return
        await self._open_next_segment(seg)

    async def _open_next_segment(self, old: _Segment | None) -> _Segment:
        """Open a fresh segment, replaying ``old``'s unsynthesized text if any."""
        # One replay covers text that crossed a retirement; a second retirement
        # of the same text means Soniox is not synthesizing it, so fail rather
        # than replay forever.
        replay = (
            old is not None
            and old.stream.retired
            and not old.stream.had_audio
            and bool(old.sent_text)
        )
        if replay and old is not None and old.replayed:
            raise HomeAssistantError(
                f"Soniox TTS stream {old.stream.stream_id} retired twice "
                "before speaking"
            )
        seg = await self._open_segment()
        if replay and old is not None:
            seg.replayed = True
            _LOGGER.debug(
                "Soniox TTS replaying %d text chunk(s) onto stream %s",
                len(old.sent_text),
                seg.stream.stream_id,
            )
            for text in old.sent_text:
                await self._send_text(seg, text)
            if old.text_end_sent:
                await self._send_text_end(seg)
        return seg

    async def _idle_close(self, seg: _Segment) -> None:
        await self._send_text_end(seg)
        _LOGGER.debug(
            "Soniox TTS idle-closed stream %s after %.0fs without text",
            seg.stream.stream_id,
            STREAM_IDLE_CLOSE,
        )

    async def _finalize(self) -> None:
        """message_gen exhausted: end the open segment (today's behaviour)."""
        seg = self._current
        if seg is not None and not seg.closed_for_text:
            await self._send_text_end(seg)

    async def _send_text(self, seg: _Segment, text: str) -> None:
        await seg.conn.send_json(
            {"text": text, "text_end": False, "stream_id": seg.stream.stream_id}
        )
        seg.sent_text.append(text)

    async def _send_text_end(self, seg: _Segment) -> None:
        if seg.text_end_sent:
            return
        seg.text_end_sent = True
        await seg.conn.send_json(
            {"text": "", "text_end": True, "stream_id": seg.stream.stream_id}
        )


class SonioxTTSPool:
    """Per-config-entry pool owning the Soniox TTS WebSocket protocol (issue #8)."""

    def __init__(
        self, hass: HomeAssistant, entry: SonioxConfigEntry, tts_ws_url: str
    ) -> None:
        self._hass = hass
        self._entry = entry
        # The URL is passed in: HA deletes entry.runtime_data on unload, yet
        # shutdown must leave the pool able to cold-dial a later request
        # (issue #8 AC6) without touching a torn-down runtime object — so the
        # pool never reads runtime_data itself.
        self._tts_ws_url = tts_ws_url
        self._idle_warm: _TTSConnection | None = None
        self._conns: set[_TTSConnection] = set()
        self._warm_task: asyncio.Task[None] | None = None
        self._shutdown = False

    def async_warm(self) -> None:
        """Speculatively open the TTS websocket for the next request's text.

        Called synchronously from STT start and returns immediately. Idempotent:
        an already-warm conn or an in-flight warm-up is left alone. A failed
        warm is debug-logged and simply retried at the next STT start — no
        reconnect/retry loop, and STT never waits on this (issue #8 AC1).
        """
        if self._shutdown or self._warm_task is not None or self._idle_warm is not None:
            return
        self._warm_task = asyncio.create_task(self._run_warm_up())

    async def stream(
        self, *, options: dict[str, Any], message_gen: AsyncIterable[str]
    ) -> AsyncGenerator[bytes]:
        """Synthesize message_gen on the warm conn or a fresh borrowed one.

        Claims an idle warm conn (no re-handshake — one ``ws_connect`` across
        warm + request, issue #8 AC2) or dials a borrowed conn that closes
        after the request. The pre-emptive segment opens immediately (before
        any text exists); later segments are opened as text resumes after an
        idle close or a Soniox retirement, all joined into one byte stream.
        """
        started = time.monotonic()
        conn = await self._claim()
        session = _RequestSession(self, conn, options, started=started)
        try:
            await session.open_first()
        except Exception as err:  # noqa: BLE001 — dead socket at config time
            await session.aclose(close_all=True)
            raise HomeAssistantError(
                f"Soniox TTS streaming connection failed: {err}"
            ) from err
        session.start(message_gen)
        try:
            async for chunk in session.consume():
                yield chunk
        finally:
            await session.aclose()

    async def async_shutdown(self) -> None:
        """Cancel every pool task and close every connection (idempotent).

        Hooked via ``entry.async_on_unload``; unload while streaming aborts the
        stream with HomeAssistantError, mirroring today's failure mode. The
        pool stays functional afterwards (a later request cold-dials a fresh
        conn) but a new warm is never scheduled post-unload (issue #8 AC6).
        """
        self._shutdown = True
        warm_task = self._warm_task
        self._warm_task = None
        if warm_task is not None:
            warm_task.cancel()
        conns = list(self._conns)
        self._conns.clear()
        self._idle_warm = None
        for conn in conns:
            await conn.close(reason="entry unloaded")
        if warm_task is not None:
            try:
                await warm_task
            except asyncio.CancelledError:
                pass

    async def _run_warm_up(self) -> None:
        """Dial and prime the idle warm conn; failures are silent by design."""
        conn: _TTSConnection | None = None
        try:
            conn = await self._dial(warm=True)
            await conn.prime_warm_up(self._warm_up_options())
            self._idle_warm = conn
            _LOGGER.debug(
                "Soniox TTS WebSocket warmed (entry %s)", self._entry.entry_id
            )
        except asyncio.CancelledError:
            if conn is not None:
                await conn.close(reason="warm-up cancelled")
            raise
        except Exception as err:  # noqa: BLE001 — warming must never fail STT
            _LOGGER.debug("Soniox TTS warm-up failed: %s", err)
            if conn is not None:
                await conn.close(reason="warm-up failed")
        finally:
            self._warm_task = None

    async def _claim(self) -> _TTSConnection:
        warm = self._idle_warm
        if warm is not None:
            if warm.alive and warm.active_streams < MAX_STREAMS_PER_CONN:
                return warm
            # Stale or saturated warm conn — forget it and dial fresh.
            self._idle_warm = None
        return await self._dial(warm=False)

    async def _dial(self, *, warm: bool) -> _TTSConnection:
        """Open a TTS websocket and register it under this pool."""
        session = async_get_clientsession(self._hass)
        ws = await session.ws_connect(
            self._tts_ws_url,
            heartbeat=30,
            max_msg_size=0,
        )
        conn = _TTSConnection(self, ws, warm=warm)
        self._conns.add(conn)
        conn.start()
        return conn

    def _forget(self, conn: _TTSConnection) -> None:
        self._conns.discard(conn)
        if self._idle_warm is conn:
            self._idle_warm = None
            _LOGGER.debug("Soniox TTS warm connection dropped")

    def _warm_up_options(self) -> dict[str, Any]:
        """Default per-stream config for the anchor warm-up stream."""
        audio_format = self._entry.options.get(
            CONF_TTS_AUDIO_FORMAT, DEFAULT_TTS_AUDIO_FORMAT
        )
        options: dict[str, Any] = {
            "api_key": self._entry.data[CONF_API_KEY],
            "model": self._entry.options.get(CONF_TTS_MODEL, DEFAULT_TTS_MODEL),
            "language": self._entry.options.get(
                CONF_TTS_LANGUAGE, DEFAULT_TTS_LANGUAGE
            ).split("-", 1)[0].lower(),
            "voice": self._entry.options.get(CONF_TTS_VOICE, DEFAULT_TTS_VOICE),
            "audio_format": audio_format,
        }
        if audio_format.startswith("pcm") or audio_format == "wav":
            options["sample_rate"] = int(
                self._entry.options.get(
                    CONF_TTS_SAMPLE_RATE, DEFAULT_TTS_SAMPLE_RATE
                )
            )
        return options
