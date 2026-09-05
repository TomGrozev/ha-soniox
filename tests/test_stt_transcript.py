"""Regression tests: Soniox STT transcripts must not contain control tokens.

Soniox's endpoint detection (https://soniox.com/docs/stt/rt/endpoint-detection)
emits a special final ``<end>`` token at every segment boundary. Assembly must
filter those markers (and the manual-finalize ``<fin>`` ack) instead of gluing
them onto the transcript we hand Home Assistant.
"""

from __future__ import annotations

from pathlib import Path

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
    return endpoints


async def _setup_entry(hass, aioclient_mock):
    """Set up one config entry with a mocked catalog."""
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


async def test_realtime_drops_midstream_markers_between_segments(
    hass, aioclient_mock, mock_soniox_ws
):
    """An <end> between two finalized segments must not glue/split the text."""
    realtime, _ = await _setup_entry(hass, aioclient_mock)
    ws = mock_soniox_ws(
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
    assert result.text == "Turn on the kitchen light"


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
