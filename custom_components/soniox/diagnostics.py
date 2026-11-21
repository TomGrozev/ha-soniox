"""Diagnostics support for the Soniox integration.

The download is meant to be attached to a bug report, so it carries only what
helps reproduce a problem: the entry's options, the region and a summary of the
fetched TTS catalog. Bug reports are public, so the API key must not appear.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from . import SonioxConfigEntry
from .const import CONF_API_KEY, CONF_REGION, DEFAULT_REGION


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SonioxConfigEntry
) -> dict[str, Any]:
    """Return the options, region and catalog counts for a bug report."""
    catalog = entry.runtime_data.catalog
    return {
        # The API key lives in entry.data and is not part of this summary. The
        # redaction guard keeps a future secret option out of public reports.
        "options": async_redact_data(dict(entry.options), {CONF_API_KEY}),
        "region": entry.data.get(CONF_REGION, DEFAULT_REGION),
        "catalog": {
            "models": len(catalog.models),
            "voices": sum(
                len(model.voices) + len(model.custom_voices)
                for model in catalog.models.values()
            ),
        },
    }
