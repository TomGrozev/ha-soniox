"""Issue #8 — warm TTS WebSocket pool, tested at public seams.

STT start opens + authenticates the TTS websocket with no TTS request present
(A); a TTS request after warm reuses that one connection — no second
handshake — with its own stream_id/format (B); in-flight / dropped / cold
requests fall back to a fresh borrowed connection that closes after its stream
(C); keepalives hold an idle warm conn at cadence only after the warm-up
config, and the server's close is respected with no reconnect storm (D); entry
unload tears the pool down and a later request cold-dials a new connection (E).

The harness autouse ``verify_cleanup`` fails any test leaving lingering
tasks/timers, so every test that warms the pool unloads the config entry (pool
shutdown) before returning.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import aiohttp
from homeassistant.components.stt import (
    AudioBitRates,
    AudioChannels,
    AudioCodecs,
    AudioFormats,
    AudioSampleRates,
    SpeechMetadata,
    SpeechResultState,
    SpeechToTextEntity,
)
from homeassistant.components.tts import ATTR_PREFERRED_FORMAT, TTSAudioRequest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.entity_platform import DATA_ENTITY_PLATFORM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soniox import pool as soniox_pool
from custom_components.soniox.const import (
    CONF_API_KEY,
    CONF_REGION,
    DOMAIN,
    REGION_US,
    endpoints_for_region,
)

FIXTURES = Path(__file__).parent / "fixtures"

# A single realtime-STT response frame: one finalized token + finished.
_STT_RESULT = [{"tokens": [{"text": "hello", "is_final": True}], "finished": True}]


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _mock_catalog(aioclient_mock):
    """Mock the GET /v1/tts-models and GET /v1/voices endpoints."""
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
    return endpoints


async def _setup_entry(hass, aioclient_mock):
    """Set up one config entry (mocked catalog); return entry + entities."""
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
    stt = [e for e in entities if isinstance(e, SpeechToTextEntity)]
    realtime = next(e for e in stt if e._mode == "realtime")
    async_ = next(e for e in stt if e._mode == "async")
    return entry, tts, realtime, async_


def _metadata() -> SpeechMetadata:
    return SpeechMetadata(
        language="en",
        format=AudioFormats.WAV,
        codec=AudioCodecs.PCM,
        bit_rate=AudioBitRates.BITRATE_16,
        sample_rate=AudioSampleRates.SAMPLERATE_16000,
        channel=AudioChannels.CHANNEL_MONO,
    )


async def _stream_chunks(*chunks: bytes):
    for chunk in chunks:
        yield chunk


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _tts_conns(factory) -> list:
    """The registered connections whose URL is the TTS websocket."""
    return [c for c in factory.connections if c.url.endswith("tts-websocket")]


async def _run_request(entity, *, options=None, message_gen=None):
    """Run one streaming request to completion; return the received chunks."""

    async def _default_gen():
        yield "hello"

    response = await entity.async_stream_tts_audio(
        TTSAudioRequest(
            language="en",
            options=options or {},
            message_gen=message_gen if message_gen is not None else _default_gen(),
        )
    )
    return [chunk async for chunk in response.data_gen]


async def _request_on_warm_conn(ws, entity, *, gate, want_configs, options=None):
    """Run a request that reuses the (gated) warm conn, then release its frames.

    The recv loop is held on ``gate``; we wait until the request's config
    (an ``api_key``-bearing frame) has landed on ``ws`` — proving the stream
    registered and no second connection was dialed — then set the gate so the
    scripted audio/terminated frames are demuxed to that stream.
    """
    task = asyncio.create_task(_run_request(entity, options=options))

    def _configs() -> int:
        return sum(1 for s in ws.sent if isinstance(s, dict) and "api_key" in s)

    for _ in range(500):
        if _configs() >= want_configs:
            break
        await asyncio.sleep(0.01)
    assert _configs() >= want_configs, ws.sent
    gate.set()
    return await task


async def _drive_borrowed(mock_soniox_ws, entity, incoming, *, url=None):
    """Run a TTS request that must dial its own (borrowed) connection."""
    ws = mock_soniox_ws(incoming, url=url)

    async def _send_json(data, **_: object):
        ws.sent.append(data)

    ws.send_json = _send_json
    chunks = await _run_request(entity)
    return ws, chunks


async def _warm_via_stt(realtime, mock_soniox_ws, *, stt_script, tts_script):
    """Warm the TTS pool through the realtime STT entity; return the warm conn."""
    endpoints = endpoints_for_region(REGION_US)
    mock_soniox_ws(stt_script, url=endpoints.stt_websocket_url)
    mock_soniox_ws(tts_script, url=endpoints.tts_websocket_url)
    result = await realtime.async_process_audio_stream(_metadata(), _stream_chunks(b"x"))
    assert result.result == SpeechResultState.SUCCESS
    await realtime.hass.async_block_till_done()
    return _tts_conns(mock_soniox_ws)[0]


async def _unload(hass, entry):
    """Tear the config entry down (pool shutdown) at the end of a pooled test."""
    if entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(entry.entry_id) is True


# -- seam A: STT start warms the TTS connection -------------------------


async def test_stt_start_warms_tts_conn_realtime(hass, aioclient_mock, mock_soniox_ws):
    """AC1 (realtime): TTS conn opens+authenticates with no TTS request present."""
    entry, tts, realtime, async_ = await _setup_entry(hass, aioclient_mock)
    endpoints = endpoints_for_region(REGION_US)
    mock_soniox_ws(_STT_RESULT, url=endpoints.stt_websocket_url)
    mock_soniox_ws([], url=endpoints.tts_websocket_url)

    result = await realtime.async_process_audio_stream(_metadata(), _stream_chunks(b"x"))

    # STT is never delayed or failed by warming.
    assert result.result == SpeechResultState.SUCCESS
    conns = _tts_conns(mock_soniox_ws)
    assert len(conns) == 1
    warm_config = conns[0].sent[0]
    assert warm_config["api_key"] == "test-api-key"
    assert warm_config["stream_id"]
    await _unload(hass, entry)


async def test_stt_start_warms_tts_conn_async_mode(
    hass, aioclient_mock, mock_soniox_ws
):
    """AC1 (async): the async file-upload STT path warms the pool the same way."""
    entry, tts, realtime, async_ = await _setup_entry(hass, aioclient_mock)
    endpoints = endpoints_for_region(REGION_US)
    aioclient_mock.post(endpoints.stt_files_url, json={"id": "file-1"})
    aioclient_mock.post(endpoints.stt_transcriptions_url, json={"id": "tr-1"})
    aioclient_mock.get(
        f"{endpoints.stt_transcriptions_url}/tr-1", json={"status": "completed"}
    )
    aioclient_mock.get(
        f"{endpoints.stt_transcriptions_url}/tr-1/transcript", json={"text": "hello"}
    )
    aioclient_mock.delete(f"{endpoints.stt_transcriptions_url}/tr-1")
    aioclient_mock.delete(f"{endpoints.stt_files_url}/file-1")
    mock_soniox_ws([], url=endpoints.tts_websocket_url)

    result = await async_.async_process_audio_stream(_metadata(), _stream_chunks(b"x"))

    assert result.result == SpeechResultState.SUCCESS
    # Warming is fire-and-forget: let the warm-up task reach its dial.
    await hass.async_block_till_done()
    conns = _tts_conns(mock_soniox_ws)
    assert len(conns) == 1
    warm_config = conns[0].sent[0]
    assert warm_config["api_key"] == "test-api-key"
    assert warm_config["stream_id"]
    await _unload(hass, entry)


# -- seam B: a request after warm reuses the one connection -------------


async def test_warm_conn_reused_for_tts_request(
    hass, aioclient_mock, mock_soniox_ws
):
    """AC2: one ws_connect across warm + request; distinct per-request config."""
    entry, tts, realtime, async_ = await _setup_entry(hass, aioclient_mock)
    gate = asyncio.Event()
    warm_ws = await _warm_via_stt(
        realtime,
        mock_soniox_ws,
        stt_script=_STT_RESULT,
        tts_script=[gate, {"audio": _b64(b"aa"), "terminated": True}],
    )
    assert len(warm_ws.sent) == 1  # warm-up config only, so far

    chunks = await _request_on_warm_conn(
        warm_ws,
        tts,
        gate=gate,
        want_configs=2,
        options={ATTR_PREFERRED_FORMAT: "flac"},
    )

    assert chunks == [b"aa"]
    # Still a single TTS connection — the request ran on the warm conn.
    assert len(_tts_conns(mock_soniox_ws)) == 1
    warm_config, request_config = warm_ws.sent[0], warm_ws.sent[1]
    # Warm-up config + request config, each with its own stream_id.
    assert warm_config["stream_id"] != request_config["stream_id"]
    # The request carries its own per-stream options (issue #8 AC2).
    assert request_config["audio_format"] == "flac"
    await _unload(hass, entry)


async def test_warm_conn_reused_across_consecutive_requests(
    hass, aioclient_mock, mock_soniox_ws
):
    """AC4: a second request with no STT in between still reuses the conn."""
    entry, tts, realtime, async_ = await _setup_entry(hass, aioclient_mock)
    gate1, gate2 = asyncio.Event(), asyncio.Event()
    frame = {"audio": _b64(b"aa"), "terminated": True}
    warm_ws = await _warm_via_stt(
        realtime,
        mock_soniox_ws,
        stt_script=_STT_RESULT,
        tts_script=[gate1, frame, gate2, frame],
    )

    chunks1 = await _request_on_warm_conn(warm_ws, tts, gate=gate1, want_configs=2)
    chunks2 = await _request_on_warm_conn(warm_ws, tts, gate=gate2, want_configs=3)

    assert chunks1 == [b"aa"]
    assert chunks2 == [b"aa"]
    assert len(_tts_conns(mock_soniox_ws)) == 1
    # Three per-stream configs on the one connection: warm-up + 2 requests.
    assert (
        sum(1 for s in warm_ws.sent if isinstance(s, dict) and "api_key" in s) == 3
    )
    await _unload(hass, entry)


# -- seam C: fallbacks to a fresh borrowed connection --------------------


async def test_request_during_warm_up_dials_borrowed_conn(
    hass, aioclient_mock, mock_soniox_ws
):
    """AC2b: a request while warm-up is in flight dials its own borrowed conn."""
    entry, tts, realtime, async_ = await _setup_entry(hass, aioclient_mock)
    endpoints = endpoints_for_region(REGION_US)
    dial_gate = asyncio.Event()
    # The warm dial stays in flight (gated open) — the request never awaits it.
    mock_soniox_ws(
        incoming=[], url=endpoints.tts_websocket_url, connect_gate=dial_gate
    )
    entry.runtime_data.tts_pool.async_warm()
    await asyncio.sleep(0.01)  # let the warm task reach the (gated) dial

    ws, chunks = await _drive_borrowed(
        mock_soniox_ws,
        tts,
        [{"audio": _b64(b"aa"), "terminated": True}],
        url=endpoints.tts_websocket_url,
    )

    assert chunks == [b"aa"]
    conns = _tts_conns(mock_soniox_ws)
    assert len(conns) == 2  # warm (still in flight) + borrowed
    assert ws is conns[1]  # the stream succeeded on the borrowed conn
    assert ws.closed  # borrowed conn closed after its stream
    dial_gate.set()  # let the in-flight warm settle so teardown is clean
    await hass.async_block_till_done()
    await _unload(hass, entry)


async def test_dropped_warm_falls_back_to_fresh_conn(
    hass, aioclient_mock, mock_soniox_ws
):
    """AC2b: a server-closed warm conn is forgotten; the next request cold-dials."""
    entry, tts, realtime, async_ = await _setup_entry(hass, aioclient_mock)
    closed = aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, b"", None)
    warm_ws = await _warm_via_stt(
        realtime,
        mock_soniox_ws,
        stt_script=_STT_RESULT,
        tts_script=[closed],
    )

    # Server-side close → the pool forgets the warm conn.
    for _ in range(200):
        if warm_ws.closed:
            break
        await asyncio.sleep(0.01)
    assert warm_ws.closed

    ws, chunks = await _drive_borrowed(
        mock_soniox_ws,
        tts,
        [{"audio": _b64(b"aa"), "terminated": True}],
        url=endpoints_for_region(REGION_US).tts_websocket_url,
    )
    assert chunks == [b"aa"]
    conns = _tts_conns(mock_soniox_ws)
    assert len(conns) == 2
    assert ws is not warm_ws  # a fresh connection, not the dropped one
    await _unload(hass, entry)


async def test_bare_tts_speak_stays_cold(hass, aioclient_mock, mock_soniox_ws):
    """Non-goal codified: bare tts.speak cold-connects and closes after its stream."""
    entry, tts, realtime, async_ = await _setup_entry(hass, aioclient_mock)
    ws, chunks = await _drive_borrowed(
        mock_soniox_ws,
        tts,
        [{"audio": _b64(b"aa"), "terminated": True}],
    )
    assert chunks == [b"aa"]
    assert len(_tts_conns(mock_soniox_ws)) == 1  # no warm conn was created
    assert ws.closed  # borrowed conn is closed after the stream
    await _unload(hass, entry)


# -- seam D: keepalive cadence + natural close ---------------------------


def test_keepalive_interval_within_documented_window():
    """AC3: keepalive cadence lies in Soniox's documented 20–30s window."""
    assert 20 <= soniox_pool.KEEPALIVE_INTERVAL <= 30


async def test_keepalives_start_after_warm_up_and_close_has_no_storm(
    hass, aioclient_mock, mock_soniox_ws, monkeypatch
):
    """AC3/AC4: keepalives fire only after the warm-up config, at cadence; the
    server's close is respected and no reconnect happens without STT."""
    monkeypatch.setattr(soniox_pool, "KEEPALIVE_INTERVAL", 0.05)
    entry, tts, realtime, async_ = await _setup_entry(hass, aioclient_mock)
    gate = asyncio.Event()
    closed = aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, b"", None)
    warm_ws = await _warm_via_stt(
        realtime,
        mock_soniox_ws,
        stt_script=_STT_RESULT,
        tts_script=[gate, closed],
    )

    # Before any tick elapses only the warm-up config was sent.
    assert "keep_alive" not in warm_ws.sent[0]

    def _keepalives() -> list:
        return [s for s in warm_ws.sent if s.get("keep_alive") is True]

    for _ in range(100):
        if _keepalives():
            break
        await asyncio.sleep(0.02)
    assert _keepalives(), warm_ws.sent

    warm_up_index = min(i for i, s in enumerate(warm_ws.sent) if "api_key" in s)
    first_keep_index = min(
        i for i, s in enumerate(warm_ws.sent) if s.get("keep_alive") is True
    )
    # Keepalives only count after the first stream (issue #8 AC3).
    assert warm_up_index == 0
    assert first_keep_index > warm_up_index

    # Server closes the idle conn → pool forgets it, no reconnect storm.
    gate.set()
    for _ in range(200):
        if warm_ws.closed:
            break
        await asyncio.sleep(0.01)
    assert warm_ws.closed
    await asyncio.sleep(0.1)  # a re-warm storm would dial a new conn here
    assert len(_tts_conns(mock_soniox_ws)) == 1
    await _unload(hass, entry)


# -- seam E: teardown on entry unload ------------------------------------


async def test_unload_shuts_pool_down_and_next_request_cold_dials(
    hass, aioclient_mock, mock_soniox_ws
):
    """AC6: unload tears the pool down; a later request cold-dials a NEW conn."""
    entry, tts, realtime, async_ = await _setup_entry(hass, aioclient_mock)
    # Keep the pool reference: HA deletes entry.runtime_data on unload and
    # production never streams after unload (entities are dropped with the
    # platform), so the "later request" is exercised at the pool seam.
    pool = entry.runtime_data.tts_pool
    gate = asyncio.Event()
    warm_ws = await _warm_via_stt(
        realtime,
        mock_soniox_ws,
        stt_script=_STT_RESULT,
        tts_script=[gate],
    )
    assert len(_tts_conns(mock_soniox_ws)) == 1

    assert await hass.config_entries.async_unload(entry.entry_id) is True
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert warm_ws.closed  # pool shutdown closed the warm socket
    # A post-shutdown warm is refused — no reconnect churn after unload.
    pool.async_warm()
    await hass.async_block_till_done()

    # A later request on the shut-down pool cold-dials a NEW connection.
    ws = mock_soniox_ws(
        [{"audio": _b64(b"aa"), "terminated": True}],
        url=endpoints_for_region(REGION_US).tts_websocket_url,
    )

    async def _send_json(data, **_: object):
        ws.sent.append(data)

    ws.send_json = _send_json
    response = pool.stream(
        options={"api_key": "test-api-key", "audio_format": "flac"},
        message_gen=_stream_chunks("hello"),
    )
    chunks = [chunk async for chunk in response]
    assert chunks == [b"aa"]
    conns = _tts_conns(mock_soniox_ws)
    assert len(conns) == 2  # warm (unloaded) + the fresh cold dial
    assert ws is not warm_ws  # fresh pool state
    await _unload(hass, entry)  # no-op (already unloaded)
