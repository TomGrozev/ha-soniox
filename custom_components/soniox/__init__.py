"""The Soniox integration — speech-to-text and text-to-speech via Soniox AI."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .catalog import SonioxCatalog, async_fetch_catalog
from .const import (
    CONF_API_KEY,
    CONF_REGION,
    CONF_STT_ASYNC_MODEL,
    CONF_STT_MODEL,
    CONF_TTS_MODEL,
    CONF_TTS_SAMPLE_RATE,
    DEFAULT_REGION,
    DEFAULT_STT_ASYNC_MODEL,
    DEFAULT_STT_MODEL,
    DEFAULT_TTS_MODEL,
    DEFAULT_TTS_SAMPLE_RATE,
    DOMAIN,
    SonioxEndpoints,
    endpoints_for_region,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.STT, Platform.TTS]

_LEGACY_MODELS = {
    "stt-rt-v3": DEFAULT_STT_MODEL,
    "stt-rt-v4": DEFAULT_STT_MODEL,
    "stt-async-v3": DEFAULT_STT_ASYNC_MODEL,
    "stt-async-v4": DEFAULT_STT_ASYNC_MODEL,
    "tts-rt-v1": DEFAULT_TTS_MODEL,
    "tts-rt-v1-preview": DEFAULT_TTS_MODEL,
}

@dataclass(frozen=True)
class SonioxRuntimeData:
    """Per-config-entry runtime state.

    Holds the resolved regional endpoints and the single live catalog
    (models + custom voices) fetched once at config-entry setup.
    """

    endpoints: SonioxEndpoints
    catalog: SonioxCatalog


type SonioxConfigEntry = ConfigEntry[SonioxRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: SonioxConfigEntry) -> bool:
    """Set up Soniox from a config entry."""
    endpoints = endpoints_for_region(
        entry.data.get(CONF_REGION, DEFAULT_REGION)
    )
    catalog = await async_fetch_catalog(
        async_get_clientsession(hass),
        endpoints,
        entry.data[CONF_API_KEY],
    )
    entry.runtime_data = SonioxRuntimeData(endpoints=endpoints, catalog=catalog)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: SonioxConfigEntry) -> bool:
    """Migrate old config entries to the current version."""
    if entry.version > 3:
        return False

    data = dict(entry.data)
    options = dict(entry.options)

    if entry.version == 1:
        if (old := options.get(CONF_STT_MODEL)) in _LEGACY_MODELS:
            mapped = _LEGACY_MODELS[old]
            if "async" in old:
                options[CONF_STT_ASYNC_MODEL] = mapped
                options[CONF_STT_MODEL] = DEFAULT_STT_MODEL
            else:
                options[CONF_STT_MODEL] = mapped
        if (old := options.get(CONF_TTS_MODEL)) in _LEGACY_MODELS:
            options[CONF_TTS_MODEL] = _LEGACY_MODELS[old]
        if CONF_TTS_SAMPLE_RATE in options:
            try:
                options[CONF_TTS_SAMPLE_RATE] = int(options[CONF_TTS_SAMPLE_RATE])
            except (TypeError, ValueError):
                options[CONF_TTS_SAMPLE_RATE] = DEFAULT_TTS_SAMPLE_RATE

    if CONF_REGION not in data:
        data[CONF_REGION] = DEFAULT_REGION

    if entry.version < 3:
        hass.config_entries.async_update_entry(
            entry, data=data, options=options, version=3
        )
        _LOGGER.debug("Migrated Soniox config entry %s to version 3", entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SonioxConfigEntry) -> bool:
    """Unload a Soniox config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: SonioxConfigEntry) -> None:
    """Reload when options change so STT/TTS pick up new defaults."""
    await hass.config_entries.async_reload(entry.entry_id)
