"""Reauthentication flow tests for the Soniox integration (mocked HTTP)."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soniox.const import (
    CONF_API_KEY,
    CONF_REGION,
    DOMAIN,
    REGION_US,
    endpoints_for_region,
)

OLD_API_KEY = "soniox-old-api-key-00000000"
NEW_API_KEY = "soniox-new-api-key-ABCD1234"


def _add_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_API_KEY: OLD_API_KEY, CONF_REGION: REGION_US},
        unique_id=f"{REGION_US}:{OLD_API_KEY[-8:]}",
    )
    entry.add_to_hass(hass)
    return entry


async def _start_reauth(hass, entry: MockConfigEntry):
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
    )


async def test_reauth_starts_as_confirm_form(hass, aioclient_mock):
    """A reauth flow shows the API-key form before touching the network."""
    entry = _add_entry(hass)

    result = await _start_reauth(hass, entry)

    assert result["type"] == "form"
    assert result["step_id"] == "reauth_confirm"
    assert aioclient_mock.mock_calls == []


async def test_reauth_with_valid_key_reloads_entry(
    hass, aioclient_mock, mock_catalog
):
    """A key Soniox accepts replaces the stored key and reloads the entry."""
    mock_catalog()
    entry = _add_entry(hass)

    result = await _start_reauth(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: NEW_API_KEY}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()

    assert entry.data[CONF_API_KEY] == NEW_API_KEY
    assert entry.data[CONF_REGION] == REGION_US
    assert entry.unique_id == f"{REGION_US}:{NEW_API_KEY[-8:]}"
    assert entry.state is ConfigEntryState.LOADED


async def test_reauth_with_rejected_key_reports_invalid_auth(hass, aioclient_mock):
    """A 401 re-shows the form with invalid_auth and keeps the entry as-is."""
    endpoints = endpoints_for_region(REGION_US)
    aioclient_mock.get(
        endpoints.tts_models_url,
        status=401,
        text="unauthorized",
        headers={"Content-Type": "application/json"},
    )
    entry = _add_entry(hass)

    result = await _start_reauth(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: NEW_API_KEY}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_API_KEY] == OLD_API_KEY
