"""Smoke tests for the Soniox integration (mocked HTTP/WS; no live network)."""

from __future__ import annotations

from datetime import timedelta

import aiohttp
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.soniox import SonioxRuntimeData
from custom_components.soniox.const import (
    CONF_API_KEY,
    CONF_REGION,
    DOMAIN,
    REGION_US,
    endpoints_for_region,
)


def _call_urls(aioclient_mock):
    return [(call[0], str(call[1])) for call in aioclient_mock.mock_calls]


async def test_config_entry_setup_and_unload(hass, aioclient_mock, mock_catalog):
    """Full round-trip: config flow (mocked HTTP) → setup → unload."""
    endpoints = mock_catalog()

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

    # Flow-validation GET /v1/tts-models, plus setup's catalog fetch (models
    # and voices are fetched concurrently) and the STT-language fetch.
    assert len(aioclient_mock.mock_calls) == 4, aioclient_mock.mock_calls
    assert sorted(_call_urls(aioclient_mock)) == sorted([
        ("GET", endpoints.tts_models_url),
        ("GET", endpoints.tts_models_url),
        ("GET", endpoints.voices_url),
        ("GET", endpoints.stt_models_url),
    ])

    # Unload.
    assert await hass.config_entries.async_unload(entry.entry_id) is True
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_fetches_catalog(hass, aioclient_mock, mock_catalog):
    """A directly-added entry fetches the live catalog once during setup."""
    endpoints = mock_catalog()

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
    assert "stt-rt-v6" in entry.runtime_data.stt_languages

    # Setup fetch = TTS models and voices (concurrently), then the STT models.
    assert len(aioclient_mock.mock_calls) == 3, aioclient_mock.mock_calls
    assert sorted(_call_urls(aioclient_mock)) == sorted([
        ("GET", endpoints.tts_models_url),
        ("GET", endpoints.voices_url),
        ("GET", endpoints.stt_models_url),
    ])


def _mock_endpoints_failing(aioclient_mock, status: int = 500, exc=None):
    """Mock every setup GET endpoint with the same failure."""
    endpoints = endpoints_for_region(REGION_US)
    for url in (
        endpoints.tts_models_url,
        endpoints.voices_url,
        endpoints.stt_models_url,
    ):
        if exc is not None:
            aioclient_mock.get(url, exc=exc)
        else:
            aioclient_mock.get(
                url,
                status=status,
                text="boom",
                headers={"Content-Type": "application/json"},
            )
    return endpoints


def _add_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_API_KEY: "test-api-key", CONF_REGION: REGION_US},
    )
    entry.add_to_hass(hass)
    return entry


async def test_setup_fetch_failure_schedules_retry(hass, aioclient_mock):
    """A failed catalog fetch makes setup retry instead of giving up."""
    _mock_endpoints_failing(aioclient_mock, status=500)
    entry = _add_entry(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # The failed fetch is not swallowed: the entry asks Home Assistant to
    # try again rather than staying unavailable until a manual reload.
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not hasattr(entry, "runtime_data")


async def test_setup_connection_error_schedules_retry(hass, aioclient_mock):
    """A network that is not up yet leaves the entry retrying, not erroring."""
    _mock_endpoints_failing(
        aioclient_mock, exc=aiohttp.ClientConnectionError("network down")
    )
    entry = _add_entry(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_timeout_schedules_retry(hass, aioclient_mock):
    """A hung request must retry as well; it is not a rejected key."""
    _mock_endpoints_failing(aioclient_mock, exc=TimeoutError("slow"))
    entry = _add_entry(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_retry_loads_once_soniox_is_reachable(
    hass, aioclient_mock, mock_catalog
):
    """The scheduled retry loads the entry after Soniox recovers."""
    _mock_endpoints_failing(aioclient_mock, status=500)
    entry = _add_entry(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY

    # Soniox becomes reachable before the retry fires.
    aioclient_mock.clear_requests()
    endpoints = mock_catalog()

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
    # The retry runs as a background task, so wait for those too.
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, SonioxRuntimeData)
    assert entry.runtime_data.endpoints.region == REGION_US
    assert "tts-rt-v2" in entry.runtime_data.catalog.models
    assert len(aioclient_mock.mock_calls) == 3, aioclient_mock.mock_calls
    assert sorted(_call_urls(aioclient_mock)) == sorted([
        ("GET", endpoints.tts_models_url),
        ("GET", endpoints.voices_url),
        ("GET", endpoints.stt_models_url),
    ])


async def test_setup_with_rejected_key_starts_reauth(hass, aioclient_mock):
    """A 401 during setup starts a reauthentication flow for a new key."""
    _mock_endpoints_failing(aioclient_mock, status=401)
    entry = _add_entry(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert not hasattr(entry, "runtime_data")
    reauth_flows = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"]["source"] == "reauth"
        and flow["context"]["entry_id"] == entry.entry_id
    ]
    assert len(reauth_flows) == 1
    assert reauth_flows[0]["handler"] == DOMAIN
