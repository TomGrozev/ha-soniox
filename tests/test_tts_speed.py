"""Tests for Soniox per-request TTS speed (REST body + WS config + options)."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.tts import TTSAudioRequest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.entity_platform import DATA_ENTITY_PLATFORM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soniox.const import (
    ATTR_SPEED,
    CONF_API_KEY,
    CONF_REGION,
    CONF_TTS_MODEL,
    CONF_TTS_SPEED,
    DEFAULT_TTS_SPEED,
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
    return next(e for e in entities if e.entity_id == "tts.soniox_united_states_text_to_speech")


async def _post_body(hass, aioclient_mock, entity, *, options=None):
    """Run the REST TTS path and return the JSON body aioclient_mock captured."""
    aioclient_mock.post(
        endpoints_for_region(REGION_US).tts_rest_url, content=b"audio"
    )
    audio = await entity.async_get_tts_audio(
        "hello", "en", options or {}
    )
    assert audio is not None
    posts = [call for call in aioclient_mock.mock_calls if call[0] == "POST"]
    assert posts, "expected one REST POST in aioclient_mock.mock_calls"
    return posts[-1][2]


async def _stream_config(entity, mock_soniox_ws, *, options=None):
    """Run the WS TTS path and return the first (config) message the WS got."""
    ws = mock_soniox_ws([{"terminated": True}])

    # The integration awaits ``ws.send_json`` (aiohttp's is async); the shared
    # conftest fake is synchronous, so override the instance with a coroutine.
    async def _send_json(data, **_: object):
        ws.sent.append(data)

    ws.send_json = _send_json

    async def _gen():
        yield "hello"

    request = TTSAudioRequest(
        language="en", options=options or {}, message_gen=_gen()
    )
    response = await entity.async_stream_tts_audio(request)
    async for _ in response.data_gen:
        pass
    assert ws.sent, "expected a WebSocket config message"
    return ws.sent[0]


async def test_rest_speed_per_request_overrides_option(hass, aioclient_mock):
    """A per-request ATTR_SPEED beats the CONF_TTS_SPEED option default."""
    entity = await _load_entity(
        hass, aioclient_mock, options={CONF_TTS_SPEED: 0.8}
    )
    body = await _post_body(hass, aioclient_mock, entity, options={ATTR_SPEED: 1.2})
    assert body["speed"] == 1.2


async def test_rest_speed_clamped_to_model_bounds(hass, aioclient_mock):
    """Per-request speed is clamped to the model's speed_min/speed_max."""
    entity = await _load_entity(hass, aioclient_mock)
    high = await _post_body(hass, aioclient_mock, entity, options={ATTR_SPEED: 5.0})
    assert high["speed"] == 1.3
    low = await _post_body(hass, aioclient_mock, entity, options={ATTR_SPEED: 0.1})
    assert low["speed"] == 0.7


async def test_rest_speed_option_default_when_no_per_request(hass, aioclient_mock):
    """Without a per-request speed, the option default is sent."""
    entity = await _load_entity(
        hass, aioclient_mock, options={CONF_TTS_SPEED: 0.9}
    )
    body = await _post_body(hass, aioclient_mock, entity)
    assert body["speed"] == 0.9


async def test_rest_speed_omitted_for_unsupported_model(hass, aioclient_mock):
    """tts-rt-v1 has supports_speed_adjustment false => no speed is sent."""
    entity = await _load_entity(
        hass, aioclient_mock, options={CONF_TTS_MODEL: "tts-rt-v1"}
    )
    body = await _post_body(hass, aioclient_mock, entity, options={ATTR_SPEED: 1.2})
    assert "speed" not in body


async def test_stream_ws_config_has_speed(hass, aioclient_mock, mock_soniox_ws):
    """The WS config message carries the resolved speed."""
    entity = await _load_entity(
        hass, aioclient_mock, options={CONF_TTS_SPEED: 0.9}
    )
    config = await _stream_config(entity, mock_soniox_ws)
    assert config["speed"] == 0.9


async def test_stream_ws_config_omits_speed_for_unsupported_model(
    hass, aioclient_mock, mock_soniox_ws
):
    """An unsupported model omits speed from the WS config message."""
    entity = await _load_entity(
        hass, aioclient_mock, options={CONF_TTS_MODEL: "tts-rt-v1"}
    )
    config = await _stream_config(entity, mock_soniox_ws)
    assert "speed" not in config


async def test_supported_options_include_speed(hass, aioclient_mock):
    """ATTR_SPEED must be in supported_options for per-request use."""
    entity = await _load_entity(hass, aioclient_mock)
    assert ATTR_SPEED in entity.supported_options


async def test_default_options_speed_from_option(hass, aioclient_mock):
    """default_options advertises the naive (unclamped) option default."""
    entity = await _load_entity(
        hass, aioclient_mock, options={CONF_TTS_SPEED: 0.9}
    )
    assert entity.default_options[ATTR_SPEED] == 0.9


async def test_default_options_speed_default_when_unset(hass, aioclient_mock):
    """default_options falls back to DEFAULT_TTS_SPEED when unset."""
    entity = await _load_entity(hass, aioclient_mock)
    assert entity.default_options[ATTR_SPEED] == DEFAULT_TTS_SPEED
