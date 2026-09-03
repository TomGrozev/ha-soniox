"""Smoke tests for the Soniox integration (mocked HTTP/WS; no live network)."""

from __future__ import annotations

from pathlib import Path

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soniox.const import (
    CONF_API_KEY,
    CONF_REGION,
    DOMAIN,
    REGION_US,
    SonioxEndpoints,
    endpoints_for_region,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


async def test_config_entry_setup_and_unload(hass, aioclient_mock):
    """Full round-trip: config flow (mocked HTTP) → setup → unload."""
    models_url = endpoints_for_region(REGION_US).tts_models_url
    aioclient_mock.get(
        models_url,
        text=_load_fixture("models_payload.json"),
        headers={"Content-Type": "application/json"},
    )

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

    assert isinstance(entry.runtime_data, SonioxEndpoints)

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

    # Exactly one HTTP call: the config-flow validation GET /v1/tts-models.
    assert len(aioclient_mock.mock_calls) == 1, aioclient_mock.mock_calls
    call = aioclient_mock.mock_calls[0]
    method, url = call[0], str(call[1])
    assert method == "GET"
    assert url == models_url

    # Unload.
    assert await hass.config_entries.async_unload(entry.entry_id) is True
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_makes_no_live_calls(hass, aioclient_mock):
    """A directly-added entry must not touch the network during setup."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_API_KEY: "test-api-key", CONF_REGION: REGION_US},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, SonioxEndpoints)
    assert aioclient_mock.mock_calls == []
