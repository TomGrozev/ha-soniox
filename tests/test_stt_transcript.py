"""Regression tests: Soniox STT transcripts must not contain control tokens.

Soniox's endpoint detection (https://soniox.com/docs/stt/rt/endpoint-detection)
emits a special final ``<end>`` token at every segment boundary. Assembly must
filter those markers (and the manual-finalize ``<fin>`` ack) instead of gluing
them onto the transcript we hand Home Assistant.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
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
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers.entity_platform import DATA_ENTITY_PLATFORM
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soniox import stt as stt_module
from custom_components.soniox.const import (
    CONF_API_KEY,
    CONF_ENDPOINT_LATENCY_ADJUSTMENT_LEVEL,
    CONF_MAX_ENDPOINT_DELAY_MS,
    CONF_REGION,
    CONF_STT_HOME_CONTEXT,
    DOMAIN,
    REGION_US,
    endpoints_for_region,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _mock_catalog(aioclient_mock):
    """Mock the GET /v1/tts-models and GET /v1/voices endpoints (see test_smoke)."""
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
    aioclient_mock.get(
        endpoints.stt_models_url,
        text=_load_fixture("stt_models_payload.json"),
        headers={"Content-Type": "application/json"},
    )
    return endpoints


async def _setup_entry(hass, aioclient_mock, options=None):
    """Set up one config entry with a mocked catalog."""
    _mock_catalog(aioclient_mock)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_API_KEY: "test-api-key", CONF_REGION: REGION_US},
        options=dict(options or {}),
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
    stt = [e for e in entities if isinstance(e, SpeechToTextEntity)]
    realtime = next(e for e in stt if e._mode == "realtime")
    async_ = next(e for e in stt if e._mode == "async")
    return realtime, async_


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


async def _stream_one_chunk_then_hold(chunk: bytes):
    """Send one chunk, then stay open like a VAD that has not ended the turn."""
    yield chunk
    await asyncio.Event().wait()


def _latency_log_lines(caplog) -> list[str]:
    """Return the end-of-speech latency debug lines the STT session logged."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "custom_components.soniox.stt"
        and "ms after the end of speech" in record.getMessage()
    ]


async def test_realtime_strips_end_marker(hass, aioclient_mock, mock_soniox_ws):
    """A final <end> token must not survive into the transcript (bug report)."""
    realtime, _ = await _setup_entry(hass, aioclient_mock)
    ws = mock_soniox_ws(
        [
            {
                "tokens": [
                    {"text": "Turn", "is_final": True},
                    {"text": " on the", "is_final": True},
                    {"text": " kitchen lights", "is_final": True},
                    {"text": "<end>", "is_final": True},
                ]
            },
            {"finished": True},
        ]
    )

    result = await realtime.async_process_audio_stream(
        _metadata(), _stream_chunks(b"pcmpcm")
    )

    assert result.result == SpeechResultState.SUCCESS
    assert result.text == "Turn on the kitchen lights"


async def test_realtime_returns_transcript_on_endpoint_before_stream_ends(
    hass, aioclient_mock, mock_soniox_ws
):
    """<end> must return the transcript while the audio stream is still open (#11)."""
    realtime, _ = await _setup_entry(hass, aioclient_mock)
    mock_soniox_ws(
        [
            {
                "tokens": [
                    {"text": "Turn", "is_final": True},
                    {"text": " on the", "is_final": True},
                    {"text": " kitchen lights", "is_final": True},
                    {"text": "<end>", "is_final": True},
                ]
            },
            # Never set: the turn must end on <end>, not on the socket
            # finishing or on this Event.
            asyncio.Event(),
        ]
    )

    result = await asyncio.wait_for(
        realtime.async_process_audio_stream(
            _metadata(), _stream_one_chunk_then_hold(b"pcmpcm")
        ),
        timeout=1,
    )

    assert result.result == SpeechResultState.SUCCESS
    assert result.text == "Turn on the kitchen lights"


async def test_realtime_end_marker_ends_the_turn(
    hass, aioclient_mock, mock_soniox_ws
):
    """Tokens after the first <end> belong to a later turn and stay out."""
    realtime, _ = await _setup_entry(hass, aioclient_mock)
    mock_soniox_ws(
        [
            {
                "tokens": [
                    {"text": "Turn", "is_final": True},
                    {"text": " on the", "is_final": True},
                    {"text": "<end>", "is_final": True},
                ]
            },
            {
                "tokens": [
                    {"text": " kitchen light", "is_final": True},
                    {"text": "<end>", "is_final": True},
                ]
            },
            {"finished": True},
        ]
    )

    result = await realtime.async_process_audio_stream(
        _metadata(), _stream_chunks(b"pcmpcm")
    )

    assert result.result == SpeechResultState.SUCCESS
    assert result.text == "Turn on the"


async def test_realtime_config_carries_endpoint_options(
    hass, aioclient_mock, mock_soniox_ws
):
    """Saved endpoint options reach the Soniox realtime config message."""
    realtime, _ = await _setup_entry(
        hass,
        aioclient_mock,
        options={
            CONF_MAX_ENDPOINT_DELAY_MS: 1000,
            CONF_ENDPOINT_LATENCY_ADJUSTMENT_LEVEL: 2,
        },
    )
    ws = mock_soniox_ws(
        [
            {"tokens": [{"text": "Hi", "is_final": True}]},
            {"finished": True},
        ]
    )

    await realtime.async_process_audio_stream(
        _metadata(), _stream_chunks(b"pcmpcm")
    )

    assert ws.sent[0][CONF_MAX_ENDPOINT_DELAY_MS] == 1000
    assert ws.sent[0][CONF_ENDPOINT_LATENCY_ADJUSTMENT_LEVEL] == 2
    assert isinstance(ws.sent[0][CONF_MAX_ENDPOINT_DELAY_MS], int)
    assert isinstance(ws.sent[0][CONF_ENDPOINT_LATENCY_ADJUSTMENT_LEVEL], int)


async def test_realtime_config_uses_endpoint_defaults(
    hass, aioclient_mock, mock_soniox_ws
):
    """An entry without endpoint options sends Soniox's current defaults."""
    realtime, _ = await _setup_entry(hass, aioclient_mock)
    ws = mock_soniox_ws(
        [
            {"tokens": [{"text": "Hi", "is_final": True}]},
            {"finished": True},
        ]
    )

    await realtime.async_process_audio_stream(
        _metadata(), _stream_chunks(b"pcmpcm")
    )

    assert ws.sent[0][CONF_MAX_ENDPOINT_DELAY_MS] == 2000
    assert ws.sent[0][CONF_ENDPOINT_LATENCY_ADJUSTMENT_LEVEL] == 0


async def test_realtime_config_omits_home_context_by_default(
    hass, aioclient_mock, mock_soniox_ws
):
    """Home context is opt-in, so the default config carries no home data (#14)."""
    realtime, _ = await _setup_entry(hass, aioclient_mock)
    ws = mock_soniox_ws(
        [
            {"tokens": [{"text": "Hi", "is_final": True}]},
            {"finished": True},
        ]
    )

    await realtime.async_process_audio_stream(
        _metadata(), _stream_chunks(b"pcmpcm")
    )

    assert "context" not in ws.sent[0]


async def test_realtime_config_includes_exposed_home_terms(
    hass, aioclient_mock, mock_soniox_ws
):
    """Opting in sends exposed names and aliases plus area and floor names (#14)."""
    assert await async_setup_component(hass, "homeassistant", {})

    floor = fr.async_get(hass).async_create("Ground floor")
    ar.async_get(hass).async_create("Kitchen", floor_id=floor.floor_id)

    registry = er.async_get(hass)
    exposed = registry.async_get_or_create(
        "light", "demo", "kitchen", suggested_object_id="kitchen"
    )
    registry.async_update_entity(exposed.entity_id, aliases={"Ceiling lamp"})
    hass.states.async_set(
        exposed.entity_id, "on", {"friendly_name": "Kitchen light"}
    )

    hidden = registry.async_get_or_create(
        "light", "demo", "hidden", suggested_object_id="hidden"
    )
    registry.async_update_entity(hidden.entity_id, aliases={"Secret lamp"})
    hass.states.async_set(hidden.entity_id, "on", {"friendly_name": "Secret light"})
    async_expose_entity(hass, "conversation", hidden.entity_id, False)

    explicitly_exposed = registry.async_get_or_create(
        "input_text", "demo", "note", suggested_object_id="note"
    )
    hass.states.async_set(
        explicitly_exposed.entity_id, "hi", {"friendly_name": "Doorbell note"}
    )
    async_expose_entity(hass, "conversation", explicitly_exposed.entity_id, True)

    realtime, _ = await _setup_entry(
        hass, aioclient_mock, options={CONF_STT_HOME_CONTEXT: True}
    )
    ws = mock_soniox_ws(
        [
            {"tokens": [{"text": "Hi", "is_final": True}]},
            {"finished": True},
        ]
    )

    await realtime.async_process_audio_stream(
        _metadata(), _stream_chunks(b"pcmpcm")
    )

    terms = ws.sent[0]["context"]["terms"]
    assert "Kitchen light" in terms
    assert "Ceiling lamp" in terms
    assert "Doorbell note" in terms
    assert "Kitchen" in terms
    assert "Ground floor" in terms
    assert "Secret light" not in terms
    assert "Secret lamp" not in terms


async def test_realtime_without_end_returns_transcript_at_stream_end(
    hass, aioclient_mock, mock_soniox_ws
):
    """A session that never sees <end> still returns its transcript."""
    realtime, _ = await _setup_entry(hass, aioclient_mock)
    mock_soniox_ws(
        [
            {
                "tokens": [
                    {"text": "Turn on the", "is_final": True},
                    {"text": " kitchen lights", "is_final": True},
                    {"text": "<fin>", "is_final": True},
                ]
            },
            {"finished": True},
        ]
    )

    result = await realtime.async_process_audio_stream(
        _metadata(), _stream_chunks(b"pcmpcm")
    )

    assert result.result == SpeechResultState.SUCCESS
    assert result.text == "Turn on the kitchen lights"


async def test_realtime_logs_end_of_speech_to_transcript_latency(
    hass, aioclient_mock, mock_soniox_ws, caplog
):
    """An endpoint-detected turn logs one measured latency line (#11)."""
    realtime, _ = await _setup_entry(hass, aioclient_mock)
    mock_soniox_ws(
        [
            {
                "tokens": [
                    {"text": "Turn on", "is_final": True, "end_ms": 600},
                    {"text": " the", "is_final": True, "end_ms": 760},
                    {"text": " lights", "is_final": True, "end_ms": 1500},
                    {"text": "<end>", "is_final": True},
                ]
            },
            asyncio.Event(),
        ]
    )
    caplog.set_level(logging.DEBUG, logger="custom_components.soniox.stt")

    await asyncio.wait_for(
        realtime.async_process_audio_stream(
            _metadata(), _stream_one_chunk_then_hold(b"pcmpcm")
        ),
        timeout=1,
    )

    lines = _latency_log_lines(caplog)
    assert len(lines) == 1
    match = re.fullmatch(
        r"Soniox STT transcript (\d+) ms after the end of speech "
        r"\(endpoint detected\)",
        lines[0],
    )
    assert match is not None
    assert int(match.group(1)) >= 0


async def test_realtime_logs_stream_end_latency(
    hass, aioclient_mock, mock_soniox_ws, caplog
):
    """A session without <end> logs the stream-end latency instead."""
    realtime, _ = await _setup_entry(hass, aioclient_mock)
    mock_soniox_ws(
        [
            {"tokens": [{"text": "Turn on the lights", "is_final": True}]},
            {"finished": True},
        ]
    )
    caplog.set_level(logging.DEBUG, logger="custom_components.soniox.stt")

    await realtime.async_process_audio_stream(
        _metadata(), _stream_chunks(b"pcmpcm")
    )

    lines = _latency_log_lines(caplog)
    assert len(lines) == 1
    assert re.fullmatch(
        r"Soniox STT transcript \d+ ms after the end of speech "
        r"\(audio stream ended\)",
        lines[0],
    )


def test_transcript_latency_prefers_token_end_off_speech_end():
    assert stt_module._transcript_latency_ms(4200, 1500, 0) == 2700
    assert stt_module._transcript_latency_ms(4200, None, 4000) == 200
    assert stt_module._transcript_latency_ms(1000, 1500, 0) == 0


async def test_realtime_empty_transcript_logs_one_latency_line(
    hass, aioclient_mock, mock_soniox_ws, caplog
):
    """A session that hears no speech still logs exactly one latency line."""
    realtime, _ = await _setup_entry(hass, aioclient_mock)
    mock_soniox_ws([{"tokens": [], "finished": True}])
    caplog.set_level(logging.DEBUG, logger="custom_components.soniox.stt")

    result = await realtime.async_process_audio_stream(
        _metadata(), _stream_chunks(b"pcmpcm")
    )

    assert result.result == SpeechResultState.ERROR
    lines = _latency_log_lines(caplog)
    assert len(lines) == 1
    assert re.fullmatch(
        r"Soniox STT transcript \d+ ms after the end of speech "
        r"\(empty transcript\)",
        lines[0],
    )


async def test_realtime_stalled_final_reply_ends_turn_with_error(
    hass, aioclient_mock, mock_soniox_ws, monkeypatch, caplog
):
    """A socket that never sends `finished` must not hang the voice turn (#10)."""
    realtime, _ = await _setup_entry(hass, aioclient_mock)
    monkeypatch.setattr(stt_module, "_REALTIME_FINALIZE_TIMEOUT", 0.05)
    # An Event holds the receive loop open with no server frames, like a live
    # socket that stays connected after the audio has been sent.
    mock_soniox_ws([asyncio.Event()])
    caplog.set_level(logging.ERROR, logger="custom_components.soniox.stt")

    result = await asyncio.wait_for(
        realtime.async_process_audio_stream(
            _metadata(), _stream_chunks(b"pcmpcm")
        ),
        timeout=1,
    )

    assert result.result == SpeechResultState.ERROR
    errors = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR
        and record.name == "custom_components.soniox.stt"
    ]
    assert len(errors) == 1
    assert "Soniox STT session" in errors[0].getMessage()


async def test_async_tokens_transcript_strips_end_marker(
    hass, aioclient_mock
):
    """The async token-fallback transcript must also exclude <end>."""
    realtime, async_ = await _setup_entry(hass, aioclient_mock)
    endpoints = endpoints_for_region(REGION_US)
    aioclient_mock.post(
        endpoints.stt_files_url,
        json={"id": "file-1"},
    )
    aioclient_mock.post(
        endpoints.stt_transcriptions_url,
        json={"id": "tr-1"},
    )
    aioclient_mock.get(
        f"{endpoints.stt_transcriptions_url}/tr-1",
        json={"status": "completed"},
    )
    aioclient_mock.get(
        f"{endpoints.stt_transcriptions_url}/tr-1/transcript",
        json={
            "tokens": [
                {"text": "Turn", "is_final": True},
                {"text": " on the kitchen light", "is_final": True},
                {"text": "<end>", "is_final": True},
            ]
        },
    )
    aioclient_mock.delete(f"{endpoints.stt_transcriptions_url}/tr-1")
    aioclient_mock.delete(f"{endpoints.stt_files_url}/file-1")

    result = await async_.async_process_audio_stream(
        _metadata(), _stream_chunks(b"pcmpcm")
    )

    assert result.result == SpeechResultState.SUCCESS
    assert result.text == "Turn on the kitchen light"


async def test_async_text_transcript_drops_trailing_end_marker(
    hass, aioclient_mock
):
    """A plain-text transcript that ends with the marker gets it stripped."""
    _, async_ = await _setup_entry(hass, aioclient_mock)
    endpoints = endpoints_for_region(REGION_US)
    aioclient_mock.post(endpoints.stt_files_url, json={"id": "file-1"})
    aioclient_mock.post(endpoints.stt_transcriptions_url, json={"id": "tr-1"})
    aioclient_mock.get(
        f"{endpoints.stt_transcriptions_url}/tr-1",
        json={"status": "completed"},
    )
    aioclient_mock.get(
        f"{endpoints.stt_transcriptions_url}/tr-1/transcript",
        json={"text": "Turn on the kitchen light<end>"},
    )
    aioclient_mock.delete(f"{endpoints.stt_transcriptions_url}/tr-1")
    aioclient_mock.delete(f"{endpoints.stt_files_url}/file-1")

    result = await async_.async_process_audio_stream(
        _metadata(), _stream_chunks(b"pcmpcm")
    )

    assert result.result == SpeechResultState.SUCCESS
    assert result.text == "Turn on the kitchen light"


async def test_home_context_terms_stay_within_soniox_size_limit(hass):
    """Long names must not push the context past Soniox's character limit (#14)."""
    assert await async_setup_component(hass, "homeassistant", {})
    area_registry = ar.async_get(hass)
    for index in range(30):
        area_registry.async_create(f"Area {index} " + "x" * 500)

    terms = stt_module._home_context_terms(hass)

    assert terms
    assert len(", ".join(terms)) <= stt_module._CONTEXT_TERMS_MAX_CHARS
    assert len(terms) < 30
