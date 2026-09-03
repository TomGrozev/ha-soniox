"""Smoke tests for the Soniox integration (mocked HTTP/WS; no live network)."""

from __future__ import annotations

from pathlib import Path

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soniox import SonioxRuntimeData
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


def _call_urls(aioclient_mock):
    return [(call[0], str(call[1])) for call in aioclient_mock.mock_calls]


async def test_config_entry_setup_and_unload(hass, aioclient_mock):
    """Full round-trip: config flow (mocked HTTP) → setup → unload."""
    endpoints = _mock_catalog(aioclient_mock)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_API_KEY: "test-api-key", CONF_REGION: REGION_US},
    )
    assert result["type"] == "create_entry", result
    entry = hass.config_entries.async_get_entry(result["result"].entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    assert isinstance(entry.runtime_data, SonioxRuntimeData)
    assert entry.runtime_data.endpoints.region == "us"
    catalog = entry.runtime_data.catalog
    assert "tts-rt-v2" in catalog.models
    # The custom voice from voices_payload.json is merged across models.
    assert any(voice.custom for voice in catalog.models["tts-rt-v2"].custom_voices)

    # Both platforms loaded: one TTS entity and both STT entities.
    assert (
        hass.states.get("tts.soniox_united_states_text_to_speech") is not None
    )
    assert (
        hass.states.get("stt.soniox_united_states_speech_to_text") is not None
    )
    assert (
        hass.states.get("stt.soniox_united_states_speech_to_text_async")
        is not None
    )

    # Flow-validation GET /v1/tts-models, plus setup's catalog fetch which
    # hits /v1/tts-models then /v1/voices.
    assert len(aioclient_mock.mock_calls) == 3, aioclient_mock.mock_calls
    assert _call_urls(aioclient_mock) == [
        ("GET", endpoints.tts_models_url),
        ("GET", endpoints.tts_models_url),
        ("GET", endpoints.voices_url),
    ]

    # Unload.
    assert await hass.config_entries.async_unload(entry.entry_id) is True
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_fetches_catalog(hass, aioclient_mock):
    """A directly-added entry fetches the live catalog once during setup."""
    endpoints = _mock_catalog(aioclient_mock)

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_API_KEY: "test-api-key", CONF_REGION: REGION_US},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    assert isinstance(entry.runtime_data, SonioxRuntimeData)
    assert entry.runtime_data.endpoints.region == REGION_US
    assert "tts-rt-v2" in entry.runtime_data.catalog.models

    # Setup fetch = exactly the models GET then the voices GET.
    assert len(aioclient_mock.mock_calls) == 2, aioclient_mock.mock_calls
    assert _call_urls(aioclient_mock) == [
        ("GET", endpoints.tts_models_url),
        ("GET", endpoints.voices_url),
    ]


async def test_setup_fetch_failure_surfaces(hass, aioclient_mock):
    """A failed catalog fetch must surface and leave the entry unloaded."""
    aioclient_mock.get(
        endpoints_for_region(REGION_US).tts_models_url,
        status=500,
        text="boom",
        headers={"Content-Type": "application/json"},
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_API_KEY: "test-api-key", CONF_REGION: REGION_US},
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # The failed fetch is not swallowed: setup errors and the platform never
    # loads with an empty catalog.
    assert entry.state is not ConfigEntryState.LOADED
