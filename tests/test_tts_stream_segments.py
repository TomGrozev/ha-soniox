"""Streaming TTS across Soniox stream retirements (issue #8 follow-up).

An LLM pipeline opens the TTS request before any speakable text exists and can
pause for seconds (a tool call). Soniox retires a stream left without text for
a few seconds (a 408 / request_timeout) and has no per-stream keepalive; the
pool keeps no timer of its own — that retirement is the signal to open a fresh
stream for the next text, every segment's audio joined into the one byte stream
HA asked for. A stream Soniox retires under us must not fail the request: any
text that never reached synthesis is replayed on the next stream, and a later
400 for the retired stream is ignored.

Frames are tagged with the ``stream_id`` of the config/text that precedes them
(a callable scripted item reads the fake's ``.sent``), so the demuxer routes
each frame to the segment the test means — the untagged legacy protocol is only
exercised by the older single-segment tests.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest
from homeassistant.components.tts import ATTR_PREFERRED_FORMAT, TTSAudioRequest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import DATA_ENTITY_PLATFORM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soniox.const import (
    CONF_API_KEY,
    CONF_REGION,
    DOMAIN,
    REGION_US,
    endpoints_for_region,
)

FIXTURES = Path(__file__).parent / "fixtures"
_TTS_URL = endpoints_for_region(REGION_US).tts_websocket_url


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _mock_catalog(aioclient_mock):
    endpoints = endpoints_for_region(REGION_US)
    aioclient_mock.get(
        endpoints.tts_models_url,
        text=_load_fixture("models_payload.json"),
        headers={"Content-Type": "application/json"},
    )
    aioclient_mock.get(
        endpoints.voices_url,
        text=_load_fixture("voices_payload.json"),
        headers={"Content-Type": "application/json"},
    )


async def _setup_entry(hass, aioclient_mock):
    """Set up one config entry (mocked catalog); return entry + TTS entity."""
    _mock_catalog(aioclient_mock)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_API_KEY: "test-api-key", CONF_REGION: REGION_US},
        options={},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    entities = [
        entity
        for platform in hass.data[DATA_ENTITY_PLATFORM][DOMAIN]
        for entity in platform.entities.values()
    ]
    tts = next(e for e in entities if e.entity_id.endswith("_text_to_speech"))
    return entry, tts


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _configs(ws) -> list:
    """Per-stream config frames (they carry the api_key)."""
    return [s for s in ws.sent if isinstance(s, dict) and "api_key" in s]


def _texts(ws) -> list:
    """Non-empty text frames."""
    return [
        s
        for s in ws.sent
        if isinstance(s, dict) and s.get("text") and not s.get("text_end")
    ]


def _text_ends(ws) -> list:
    """text_end frames (the end-of-message finalize or a replay)."""
    return [s for s in ws.sent if isinstance(s, dict) and s.get("text_end")]


def _tts_conns(factory) -> list:
    return [c for c in factory.connections if c.url.endswith("tts-websocket")]


async def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    for _ in range(max(1, int(timeout / 0.005))):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never became true")


class _ScriptedGen:
    """An async text generator a test drives step by step."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    def push(self, text: str) -> None:
        self._queue.put_nowait(text)

    def close(self) -> None:
        self._queue.put_nowait(None)


async def _stream_gen(entity, message_gen, options=None):
    """Start a streaming request; return its audio generator."""
    response = await entity.async_stream_tts_audio(
        TTSAudioRequest(
            language="en", options=options or {}, message_gen=message_gen
        )
    )
    return response.data_gen


async def _collect(entity, message_gen, options=None) -> list[bytes]:
    gen = await _stream_gen(entity, message_gen, options)
    return [chunk async for chunk in gen]


async def _unload(hass, entry) -> None:
    if entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(entry.entry_id) is True


def _pcm() -> dict:
    # pcm_s16le is headerless, so the joiner passes audio straight through and
    # the tests observe the pool's segment ordering, not header handling.
    return {ATTR_PREFERRED_FORMAT: "pcm_s16le"}


def _sid_for_text(ws, index: int) -> str:
    return _texts(ws)[index]["stream_id"]


# -- pause, retirement, and reopen ---------------------------------------


async def test_pause_retires_segment_and_reopens_same_conn(
    hass, aioclient_mock, mock_soniox_ws
):
    """A pause after text retires stream A; the next text opens stream B."""
    entry, tts = await _setup_entry(hass, aioclient_mock)
    gen = _ScriptedGen()
    retired = asyncio.Event()

    async def _a_audio(ws):
        await _wait_for(lambda: _texts(ws))
        return {"audio": _b64(b"AAA"), "stream_id": _sid_for_text(ws, 0)}

    async def _a_retire(ws):
        return {
            "stream_id": _sid_for_text(ws, 0),
            "error_code": 408,
            "error_type": "request_timeout",
            "error_message": "request timeout",
        }

    async def _a_term(ws):
        # Only reached after the 408 above was dispatched, so A is retired.
        retired.set()
        return {"terminated": True, "stream_id": _sid_for_text(ws, 0)}

    async def _b_audio(ws):
        await _wait_for(lambda: len(_texts(ws)) >= 2)
        return {"audio": _b64(b"BBB"), "stream_id": _sid_for_text(ws, 1)}

    async def _b_end(ws):
        await _wait_for(lambda: _text_ends(ws))
        return {"terminated": True, "stream_id": _sid_for_text(ws, 1)}

    ws = mock_soniox_ws(
        [_a_audio, _a_retire, _a_term, _b_audio, _b_end], url=_TTS_URL
    )

    task = asyncio.create_task(_collect(tts, gen, _pcm()))
    gen.push("Hello")
    # The next text must arrive only after A retired, else it would go to A.
    await retired.wait()
    gen.push("World")
    gen.close()
    chunks = await task

    assert chunks == [b"AAA", b"BBB"]
    # One connection for both segments: B reused A's conn.
    assert len(_tts_conns(mock_soniox_ws)) == 1
    configs = _configs(ws)
    assert len(configs) == 2
    assert configs[0]["stream_id"] != configs[1]["stream_id"]
    # The second text went to the fresh stream B.
    assert _texts(ws)[1]["stream_id"] == configs[1]["stream_id"]
    await _unload(hass, entry)


async def test_segment_with_text_stays_open_through_pause(
    hass, aioclient_mock, mock_soniox_ws
):
    """A stream with text is not closed for lack of text before message_gen ends."""
    entry, tts = await _setup_entry(hass, aioclient_mock)
    gen = _ScriptedGen()

    async def _end_after_text_end(ws):
        await _wait_for(lambda: _text_ends(ws))
        return {"terminated": True, "stream_id": _configs(ws)[0]["stream_id"]}

    ws = mock_soniox_ws([_end_after_text_end], url=_TTS_URL)

    task = asyncio.create_task(_collect(tts, gen, _pcm()))
    gen.push("Hello")
    await _wait_for(lambda: _texts(ws))
    await asyncio.sleep(0.2)  # a long pause with text on the open stream
    assert _text_ends(ws) == []

    gen.close()
    chunks = await task

    assert chunks == []
    # Only the end-of-message text_end, never a client-side close.
    assert len(_text_ends(ws)) == 1
    await _unload(hass, entry)


# -- Soniox retirement ---------------------------------------------------


async def test_retired_preemptive_segment_keeps_conn_and_opens_new(
    hass, aioclient_mock, mock_soniox_ws
):
    """A 408 on the text-less segment is benign; the next text opens a stream."""
    entry, tts = await _setup_entry(hass, aioclient_mock)
    gen = _ScriptedGen()
    delivered = asyncio.Event()

    async def _retire(ws):
        await _wait_for(lambda: _configs(ws))
        delivered.set()
        return {
            "stream_id": _configs(ws)[0]["stream_id"],
            "error_code": 408,
            "error_type": "request_timeout",
            "error_message": "request timeout",
        }

    async def _b_audio(ws):
        await _wait_for(lambda: len(_configs(ws)) >= 2)
        return {"audio": _b64(b"BBB"), "stream_id": _configs(ws)[1]["stream_id"]}

    async def _b_end(ws):
        await _wait_for(lambda: _text_ends(ws))
        return {"terminated": True, "stream_id": _configs(ws)[1]["stream_id"]}

    ws = mock_soniox_ws([_retire, _b_audio, _b_end], url=_TTS_URL)

    task = asyncio.create_task(_collect(tts, gen, _pcm()))
    await delivered.wait()
    gen.push("Hello")
    gen.close()
    chunks = await task

    assert chunks == [b"BBB"]
    configs = _configs(ws)
    assert len(configs) == 2
    assert configs[1]["stream_id"] != configs[0]["stream_id"]
    # The 408 did not drop the connection.
    assert len(_tts_conns(mock_soniox_ws)) == 1
    await _unload(hass, entry)


async def test_retired_text_is_replayed_and_stray_400_ignored(
    hass, aioclient_mock, mock_soniox_ws
):
    """Text that never reached synthesis is replayed; a late 400 is benign."""
    entry, tts = await _setup_entry(hass, aioclient_mock)
    gen = _ScriptedGen()

    async def _retire_after_text(ws):
        await _wait_for(lambda: _texts(ws))
        return {
            "stream_id": _sid_for_text(ws, 0),
            "error_code": 408,
            "error_type": "request_timeout",
            "error_message": "request timeout",
        }

    async def _stray_400(ws):
        await _wait_for(lambda: len(_configs(ws)) >= 2)
        return {
            "stream_id": _sid_for_text(ws, 0),
            "error_code": 400,
            "error_type": "invalid_stream_state",
            "error_message": "invalid stream state",
        }

    async def _b_audio(ws):
        return {"audio": _b64(b"BBB"), "stream_id": _sid_for_text(ws, 1)}

    async def _b_end(ws):
        await _wait_for(lambda: len(_text_ends(ws)) >= 2)
        return {"terminated": True, "stream_id": _sid_for_text(ws, 1)}

    ws = mock_soniox_ws([_retire_after_text, _stray_400, _b_audio, _b_end], url=_TTS_URL)

    task = asyncio.create_task(_collect(tts, gen, _pcm()))
    gen.push("Hello")
    gen.close()
    chunks = await task

    assert chunks == [b"BBB"]
    texts = _texts(ws)
    # "Hello" went to A, then was replayed on the fresh stream B.
    assert texts[0]["stream_id"] != texts[1]["stream_id"]
    assert texts[1]["text"] == "Hello"
    assert len(_tts_conns(mock_soniox_ws)) == 1
    await _unload(hass, entry)


# -- connection lifetime -------------------------------------------------


async def test_borrowed_conn_survives_segments_and_closes_at_end(
    hass, aioclient_mock, mock_soniox_ws
):
    """A borrowed conn spans both segments and closes only at request end."""
    entry, tts = await _setup_entry(hass, aioclient_mock)
    gen = _ScriptedGen()
    retired = asyncio.Event()

    async def _a_audio(ws):
        await _wait_for(lambda: _texts(ws))
        return {"audio": _b64(b"AAA"), "stream_id": _sid_for_text(ws, 0)}

    async def _a_retire(ws):
        return {
            "stream_id": _sid_for_text(ws, 0),
            "error_code": 408,
            "error_type": "request_timeout",
            "error_message": "request timeout",
        }

    async def _a_term(ws):
        retired.set()
        return {"terminated": True, "stream_id": _sid_for_text(ws, 0)}

    async def _b_audio(ws):
        await _wait_for(lambda: len(_texts(ws)) >= 2)
        return {"audio": _b64(b"BBB"), "stream_id": _sid_for_text(ws, 1)}

    async def _b_end(ws):
        await _wait_for(lambda: _text_ends(ws))
        return {"terminated": True, "stream_id": _sid_for_text(ws, 1)}

    ws = mock_soniox_ws(
        [_a_audio, _a_retire, _a_term, _b_audio, _b_end], url=_TTS_URL
    )

    task = asyncio.create_task(_collect(tts, gen, _pcm()))
    gen.push("Hello")
    await retired.wait()
    gen.push("World")
    await _wait_for(lambda: len(_configs(ws)) >= 2)  # B open on the same conn
    assert ws.closed is False  # not torn down between segments

    gen.close()
    chunks = await task

    assert chunks == [b"AAA", b"BBB"]
    assert len(_tts_conns(mock_soniox_ws)) == 1
    assert ws.closed is True  # closed once the whole request ended
    await _unload(hass, entry)


async def test_warm_conn_survives_segments(
    hass, aioclient_mock, mock_soniox_ws
):
    """A warm conn carries both segments and stays warm after the request."""
    entry, tts = await _setup_entry(hass, aioclient_mock)
    gen = _ScriptedGen()
    retired = asyncio.Event()

    async def _a_audio(ws):
        await _wait_for(lambda: _texts(ws))
        return {"audio": _b64(b"AAA"), "stream_id": _sid_for_text(ws, 0)}

    async def _a_retire(ws):
        return {
            "stream_id": _sid_for_text(ws, 0),
            "error_code": 408,
            "error_type": "request_timeout",
            "error_message": "request timeout",
        }

    async def _a_term(ws):
        retired.set()
        return {"terminated": True, "stream_id": _sid_for_text(ws, 0)}

    async def _b_audio(ws):
        await _wait_for(lambda: len(_texts(ws)) >= 2)
        return {"audio": _b64(b"BBB"), "stream_id": _sid_for_text(ws, 1)}

    async def _b_end(ws):
        await _wait_for(lambda: _text_ends(ws))
        return {"terminated": True, "stream_id": _sid_for_text(ws, 1)}

    # The trailing never-set gate keeps the fake recv loop open after the last
    # frame, so the conn is only closed by the pool (unload), not by script EOF.
    hold = asyncio.Event()
    ws = mock_soniox_ws(
        [_a_audio, _a_retire, _a_term, _b_audio, _b_end, hold], url=_TTS_URL
    )

    entry.runtime_data.tts_pool.async_warm()
    await _wait_for(lambda: _configs(ws))  # the warm-up config
    assert len(_tts_conns(mock_soniox_ws)) == 1  # the warm conn

    task = asyncio.create_task(_collect(tts, gen, _pcm()))
    gen.push("Hello")
    await retired.wait()
    gen.push("World")
    # configs = warm-up, A, B
    await _wait_for(lambda: len(_configs(ws)) >= 3)
    gen.close()
    chunks = await task

    assert chunks == [b"AAA", b"BBB"]
    assert len(_tts_conns(mock_soniox_ws)) == 1  # no extra dial for segment B
    assert ws.closed is False  # warm conn survived the request
    await _unload(hass, entry)
    assert ws.closed is True  # pool shutdown closed it


# -- replay loop guard ----------------------------------------------------


async def test_replayed_text_retired_again_fails_instead_of_looping(
    hass, aioclient_mock, mock_soniox_ws
):
    """A second retirement of already-replayed text fails, not replays forever."""
    entry, tts = await _setup_entry(hass, aioclient_mock)
    gen = _ScriptedGen()

    async def _retire(ws, index):
        # Every text-carrying stream is retired with a 408 and produces no audio.
        await _wait_for(lambda: len(_texts(ws)) >= index + 1)
        return {
            "stream_id": _sid_for_text(ws, index),
            "error_code": 408,
            "error_type": "request_timeout",
            "error_message": "no text",
        }

    async def _a_retire(ws):
        return await _retire(ws, 0)

    async def _a_term(ws):
        return {"terminated": True, "stream_id": _sid_for_text(ws, 0)}

    async def _b_retire(ws):
        return await _retire(ws, 1)

    async def _b_term(ws):
        return {"terminated": True, "stream_id": _sid_for_text(ws, 1)}

    ws = mock_soniox_ws([_a_retire, _a_term, _b_retire, _b_term], url=_TTS_URL)

    task = asyncio.create_task(_collect(tts, gen, _pcm()))
    gen.push("Hello")
    with pytest.raises(HomeAssistantError):
        # Before the guard this loops (replay -> retire -> replay ...); bound it
        # so a regression fails the test instead of hanging the suite.
        await asyncio.wait_for(task, timeout=3)
    gen.close()
    assert len(_configs(ws)) == 2  # pre-emptive stream + exactly one replay
    await _unload(hass, entry)
