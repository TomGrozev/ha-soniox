"""Tests for satellite-safe TTS audio options (issue #13).

HA runs ffmpeg on TTS output when a client asks for a sample channel count,
sample size or bitrate that the engine does not list in ``supported_options``.
Voice satellites such as ESPHome Voice PE always ask for 1 channel and 2-byte
samples, so the entity must declare those options, plus ``preferred_bitrate``,
to make HA skip the conversion. The declared options are the signal, because
HA 2025.12 (the test harness) still converts even when an engine lists them;
the skip rule itself landed upstream later.

Soniox emits mono 16-bit audio, so a stereo request still gets mono; Soniox
has no request fields for channels or sample size, hence only ``sample_rate``
and ``bitrate`` travel on the WebSocket config.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from homeassistant.components.tts import (
    ATTR_PREFERRED_FORMAT,
    ATTR_PREFERRED_SAMPLE_BYTES,
    ATTR_PREFERRED_SAMPLE_CHANNELS,
    ATTR_PREFERRED_SAMPLE_RATE,
    TTSAudioRequest,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.entity_platform import DATA_ENTITY_PLATFORM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soniox.const import (
    ATTR_PREFERRED_BITRATE,
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
    aioclient_mock.get(
        endpoints.stt_models_url,
        text=_load_fixture("stt_models_payload.json"),
        headers={"Content-Type": "application/json"},
    )
    return endpoints


async def _load_entity(hass, aioclient_mock, *, options=None):
    """Set up a config entry (mocked catalog) and return a TTS entity instance."""
    _mock_catalog(aioclient_mock)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_API_KEY: "test-api-key", CONF_REGION: REGION_US},
        options=options or {},
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
    return next(
        e for e in entities if e.entity_id == "tts.soniox_united_states_text_to_speech"
    )


async def _stream(entity, mock_soniox_ws, options=None):
    """Run the WS TTS path and return the config message sent to Soniox."""
    ws = mock_soniox_ws([{"terminated": True}])

    async def _gen():
        yield "hello"

    request = TTSAudioRequest(language="en", options=options or {}, message_gen=_gen())
    response = await entity.async_stream_tts_audio(request)
    async for _ in response.data_gen:
        # The fake WS has no real I/O, so tick the loop for the text pump.
        await asyncio.sleep(0)
    assert ws.sent, "expected a WebSocket config message"
    return ws.sent[0]


def _without_stream_id(config: dict) -> dict:
    """Drop the per-stream id so the rest of the config can be compared literally."""
    return {key: value for key, value in config.items() if key != "stream_id"}


async def test_entity_declares_satellite_audio_options(hass, aioclient_mock):
    """The three extra options are HA's exact skip-conversion signal.

    HA skips its ffmpeg conversion only when every preferred audio option the
    client asks for appears in ``supported_options``. Declaring channel count,
    sample size and bitrate therefore removes the satellite transcode.
    """
    entity = await _load_entity(hass, aioclient_mock)
    assert ATTR_PREFERRED_SAMPLE_CHANNELS in entity.supported_options
    assert ATTR_PREFERRED_SAMPLE_BYTES in entity.supported_options
    assert ATTR_PREFERRED_BITRATE in entity.supported_options
    # Existing format and rate declarations must survive.
    assert ATTR_PREFERRED_FORMAT in entity.supported_options
    assert ATTR_PREFERRED_SAMPLE_RATE in entity.supported_options


async def test_flac_satellite_request_sends_sample_rate_only(
    hass, aioclient_mock, mock_soniox_ws
):
    """A Voice PE style flac request carries the rate but no channel fields."""
    entity = await _load_entity(hass, aioclient_mock)
    config = await _stream(
        entity,
        mock_soniox_ws,
        options={
            ATTR_PREFERRED_FORMAT: "flac",
            ATTR_PREFERRED_SAMPLE_RATE: 48000,
            ATTR_PREFERRED_SAMPLE_CHANNELS: 1,
            ATTR_PREFERRED_SAMPLE_BYTES: 2,
        },
    )
    assert config["audio_format"] == "flac"
    assert config["sample_rate"] == 48000
    # Soniox has no request fields for these; HA's skip rule depends on the
    # declared options, not on forwarding them.
    assert "channels" not in config
    assert "sample_bytes" not in config
    assert "bitrate" not in config


async def test_mp3_bitrate_is_forwarded_in_bps(
    hass, aioclient_mock, mock_soniox_ws
):
    """HA's kbit/s bitrate becomes Soniox's bps bitrate for mp3."""
    entity = await _load_entity(hass, aioclient_mock)
    config = await _stream(
        entity,
        mock_soniox_ws,
        options={ATTR_PREFERRED_FORMAT: "mp3", ATTR_PREFERRED_BITRATE: 64},
    )
    assert config["audio_format"] == "mp3"
    assert config["bitrate"] == 64000


async def test_aac_bitrate_is_forwarded_in_bps(
    hass, aioclient_mock, mock_soniox_ws
):
    """AAC also accepts a bitrate, converted from kbit/s to bps."""
    entity = await _load_entity(hass, aioclient_mock)
    config = await _stream(
        entity,
        mock_soniox_ws,
        options={ATTR_PREFERRED_FORMAT: "aac", ATTR_PREFERRED_BITRATE: 96},
    )
    assert config["audio_format"] == "aac"
    assert config["bitrate"] == 96000


async def test_flac_never_sends_bitrate(hass, aioclient_mock, mock_soniox_ws):
    """Formats other than mp3/aac get no bitrate field."""
    entity = await _load_entity(hass, aioclient_mock)
    config = await _stream(
        entity,
        mock_soniox_ws,
        options={ATTR_PREFERRED_FORMAT: "flac", ATTR_PREFERRED_BITRATE: 64},
    )
    assert config["audio_format"] == "flac"
    assert "bitrate" not in config


async def test_invalid_bitrate_is_ignored(hass, aioclient_mock, mock_soniox_ws):
    """An unparseable bitrate sends no field and never raises."""
    entity = await _load_entity(hass, aioclient_mock)
    config = await _stream(
        entity,
        mock_soniox_ws,
        options={ATTR_PREFERRED_FORMAT: "mp3", ATTR_PREFERRED_BITRATE: "fast"},
    )
    assert config["audio_format"] == "mp3"
    assert "bitrate" not in config


async def test_request_without_options_sends_entry_defaults(
    hass, aioclient_mock, mock_soniox_ws
):
    """A bare request sends only the entry defaults and base stream fields."""
    entity = await _load_entity(hass, aioclient_mock)
    config = await _stream(entity, mock_soniox_ws)
    assert _without_stream_id(config) == {
        "api_key": "test-api-key",
        "model": "tts-rt-v2",
        "language": "en",
        "voice": "Maya",
        "audio_format": "wav",
        "sample_rate": 24000,
        "speed": 1.0,
    }
