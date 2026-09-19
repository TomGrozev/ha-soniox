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
import uuid
from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

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


@dataclass
class _Stream:
    """Per-stream receive state on one TTS connection."""

    stream_id: str
    warm_up: bool = False
    error: str | None = None
    queue: asyncio.Queue[bytes | None] = field(default_factory=asyncio.Queue)
    finished: bool = False

    def finish(self, error: str | None = None) -> None:
        """Terminate the stream (cleanly or with an error for its consumer)."""
        if self.finished:
            return
        self.finished = True
        if error is not None:
            self.error = error
        self.queue.put_nowait(None)


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
        """Live request streams (excludes the anchor warm-up stream)."""
        return sum(1 for s in self._streams.values() if not s.warm_up)

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

    async def pump_text(self, stream_id: str, message_gen: AsyncIterable[str]) -> None:
        """Forward message_gen chunks as Soniox text frames, then signal EOF."""
        async for chunk in message_gen:
            if chunk:
                await self.send_json(
                    {"text": chunk, "text_end": False, "stream_id": stream_id}
                )
        await self.send_json({"text": "", "text_end": True, "stream_id": stream_id})

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
            self._on_error_frame(stream_id, err_code, payload.get("error_message"))
            return
        if audio_b64 := payload.get("audio"):
            audio = base64.b64decode(audio_b64)
            for stream in self._targets(stream_id):
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
        self, stream_id: str | None, err_code: int | str, err_message: Any
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
        after its stream. A warm-up still in flight is never awaited: the
        request dials its own borrowed conn instead (issue #8 AC2b).
        """
        conn = await self._claim()
        stream_id = uuid.uuid4().hex
        stream = _Stream(stream_id)
        conn._streams[stream_id] = stream
        try:
            await conn.send_json({**options, "stream_id": stream_id})
        except Exception as err:  # noqa: BLE001 — dead socket at config time
            conn._streams.pop(stream_id, None)
            await conn.close(reason=str(err))
            raise HomeAssistantError(
                f"Soniox TTS streaming connection failed: {err}"
            ) from err

        pump_task = asyncio.create_task(conn.pump_text(stream_id, message_gen))
        try:
            while True:
                item = await stream.queue.get()
                if item is None:
                    if stream.error is not None:
                        raise HomeAssistantError(stream.error)
                    return
                yield item
        finally:
            if not pump_task.done():
                pump_task.cancel()
            try:
                await pump_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            conn._streams.pop(stream_id, None)
            if conn.warm is False:
                await conn.close(reason="stream finished")

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
