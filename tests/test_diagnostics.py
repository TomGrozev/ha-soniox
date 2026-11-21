"""Tests for the Soniox config-entry diagnostics download."""

from __future__ import annotations

import json

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soniox.const import (
    CONF_API_KEY,
    CONF_REGION,
    CONF_TTS_SPEED,
    CONF_TTS_VOICE,
    DOMAIN,
    REGION_US,
)
from custom_components.soniox.diagnostics import async_get_config_entry_diagnostics

API_KEY = "soniox-diag-key-1234"


async def test_diagnostics_report_options_region_catalog_without_api_key(
    hass, mock_catalog
):
    """The download carries options, region and catalog counts, never the key."""
    mock_catalog()
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_API_KEY: API_KEY, CONF_REGION: REGION_US},
        options={CONF_TTS_VOICE: "Nina", CONF_TTS_SPEED: 1.1},
        unique_id=f"{REGION_US}:{API_KEY[-8:]}",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["region"] == REGION_US
    assert diag["options"] == {
        CONF_TTS_VOICE: "Nina",
        CONF_TTS_SPEED: 1.1,
    }
    assert diag["catalog"] == {"models": 2, "voices": 7}
    assert API_KEY not in json.dumps(diag)
