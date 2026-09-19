"""End-to-end proof that the entry-level default TTS speed survives HA's real
assist seam (SpeechManager.process_options) and reaches the Soniox WS config.

The existing entity-level tests call the TTS entity directly and would not
catch a process_options-level regression (e.g. someone dropping ATTR_SPEED from
supported_options/default_options). These tests drive synthesis through HA's
PUBLIC tts API the way the Assist pipeline does, so the merged options go
through `async_create_result_stream` -> `process_options` exactly as in prod.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from homeassistant.components.tts import (
    ATTR_PREFERRED_FORMAT,
    ATTR_VOICE,
    DATA_TTS_MANAGER,
    async_create_stream,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.entity_platform import DATA_ENTITY_PLATFORM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soniox.const import (
    ATTR_SPEED,
    CONF_API_KEY,
    CONF_REGION,
    CONF_TTS_MODEL,
    CONF_TTS_SPEED,
    CONF_TTS_VOICE,
    DEFAULT_TTS_MODEL,
    DOMAIN,
    REGION_US,
    endpoints_for_region,
)

FIXTURES = Path(__file__).parent / "fixtures"

ENTITY_ID = "tts.soniox_united_states_text_to_speech"


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


async def _load_entry(hass, aioclient_mock):
    """Set up a config entry (mocked catalog) like the entity-level tests."""
    _mock_catalog(aioclient_mock)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_API_KEY: "test-api-key", CONF_REGION: REGION_US},
        options={
            CONF_TTS_SPEED: 0.8,
            CONF_TTS_VOICE: "Maya",
            CONF_TTS_MODEL: DEFAULT_TTS_MODEL,
        },
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
    entity = next(e for e in entities if e.entity_id == ENTITY_ID)
    # Bypass the persistent on-disk TTS file cache so the entity's WS stream
    # always runs. The disk cache lives in a shared dir that survives pytest
    # runs; a hit there would short-circuit generation and we'd never open the
    # Soniox WebSocket. The in-memory cache is fresh per test (new manager).
    hass.data[DATA_TTS_MANAGER].use_file_cache = False
    return entry, entity


async def _drive_assist(hass, mock_soniox_ws, *, options):
    """Synthesize through HA's public tts API and return the WS config msg.

    Mirrors how the Assist pipeline and media_source drive TTS:
    async_create_stream -> async_set_message -> async_stream_result. The
    merged options flow through SpeechManager.process_options before reaching
    the Soniox WebSocket config message.
    """
    ws = mock_soniox_ws([{"terminated": True}])

    # The integration awaits ``ws.send_json`` (aiohttp's is async); the shared
    # conftest fake is synchronous, so override with a coroutine that records
    # the outgoing message (same pattern as the entity-level streaming tests).
    async def _send_json(data, **_: object):
        ws.sent.append(data)

    ws.send_json = _send_json

    stream = async_create_stream(
        hass,
        engine=ENTITY_ID,
        language="en",
        options=options,
    )
    stream.async_set_message("hello")

    # The SpeechManager generates audio in a background task; let it run so
    # the WS is opened and the config message is sent. Poll for the config
    # with a bounded deadline rather than assuming one tick drains the task.
    for _ in range(200):
        if ws.sent:
            break
        await asyncio.sleep(0.01)
    await hass.async_block_till_done()

    assert ws.sent, "expected a WebSocket config message"
    return ws.sent[0]


async def test_entry_default_speed_survives_assist_merge(
    hass, aioclient_mock, mock_soniox_ws
):
    """Entry-level CONF_TTS_SPEED reaches the WS config via assist (no per-request speed)."""
    await _load_entry(hass, aioclient_mock)

    # Assist sends the voice plus a preferred format (its tts_audio_output);
    # the merged options must carry the entry default speed through the real
    # process_options merge. A preferred format also avoids HA's ffmpeg audio
    # conversion (streaming path is wav), which isn't available in tests.
    config = await _drive_assist(
        hass,
        mock_soniox_ws,
        options={ATTR_VOICE: "Maya", ATTR_PREFERRED_FORMAT: "wav"},
    )

    assert config["speed"] == 0.8


async def test_flac_satellite_gets_native_flac_no_transcode(
    hass, aioclient_mock, mock_soniox_ws
):
    """A flac-negotiating consumer (HA Voice PE) gets Soniox-native flac.

    Driven through HA's public tts API: the WS config must request flac and
    the stream token HA emits must be flac too. Equal extension on both sides
    is HA's exact no-ffmpeg condition (extension == final_extension).
    """
    await _load_entry(hass, aioclient_mock)

    config = await _drive_assist(
        hass,
        mock_soniox_ws,
        options={ATTR_VOICE: "Maya", ATTR_PREFERRED_FORMAT: "flac"},
    )
    assert config["audio_format"] == "flac"

    token = hass.data[DATA_TTS_MANAGER].token_to_stream
    (stream_token, result_stream), = token.items()
    assert result_stream.extension == "flac"


async def test_per_request_speed_overrides_default_via_assist(
    hass, aioclient_mock, mock_soniox_ws
):
    """A per-request speed in assist options wins over the entry default."""
    await _load_entry(hass, aioclient_mock)

    config = await _drive_assist(
        hass,
        mock_soniox_ws,
        options={
            ATTR_VOICE: "Maya",
            ATTR_SPEED: 1.1,
            ATTR_PREFERRED_FORMAT: "wav",
        },
    )

    assert config["speed"] == 1.1
