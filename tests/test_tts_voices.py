"""Tests for SonioxTTSEntity.async_get_supported_voices (catalog-backed)."""

from __future__ import annotations

from pathlib import Path

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soniox.const import (
    CONF_API_KEY,
    CONF_REGION,
    CONF_TTS_MODEL,
    DOMAIN,
    REGION_US,
    endpoints_for_region,
)
from custom_components.soniox.tts import SonioxTTSEntity

FIXTURES = Path(__file__).parent / "fixtures"

EM_DASH = "\u2014"
CUSTOM_VOICE_ID = "497f6eca-6276-4993-bfeb-53cbbbba6f08"


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
    """Set up a config entry (mocked catalog) and return the TTS entity."""
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
    return SonioxTTSEntity(entry)


async def test_supported_voices_from_catalog(hass, aioclient_mock):
    """Built-in + custom voices from the parsed catalog, with em-dash labels."""
    entity = await _load_entity(hass, aioclient_mock)

    voices = entity.async_get_supported_voices("en")
    assert [v.voice_id for v in voices] == [
        "Maya",
        "Daniel",
        "Nina",
        "Owen",
        CUSTOM_VOICE_ID,
    ]
    by_id = {v.voice_id: v for v in voices}

    assert by_id["Maya"].name == f"Maya {EM_DASH} female"
    assert by_id["Daniel"].name == f"Daniel {EM_DASH} male"
    assert by_id["Nina"].name == f"Nina {EM_DASH} female"
    assert by_id["Owen"].name == f"Owen {EM_DASH} male"
    assert by_id[CUSTOM_VOICE_ID].name == f"My Cloned Voice {EM_DASH} custom"


async def test_missing_model_returns_no_voices(hass, aioclient_mock):
    """Catalog miss on the selected model renders an empty picker, not stale."""
    entity = await _load_entity(
        hass, aioclient_mock, options={CONF_TTS_MODEL: "nonexistent-model"}
    )
    assert entity.async_get_supported_voices("en") == []


async def test_empty_options_defaults_to_tts_rt_v2(hass, aioclient_mock):
    """No stored model option degrades to DEFAULT_TTS_MODEL (tts-rt-v2) voices."""
    entity = await _load_entity(hass, aioclient_mock)
    voices = entity.async_get_supported_voices("en")
    # tts-rt-v2 keeps all four built-ins plus the ready custom voice, in
    # parser tuple order (built-ins first, then custom).
    assert [v.voice_id for v in voices] == [
        "Maya",
        "Daniel",
        "Nina",
        "Owen",
        CUSTOM_VOICE_ID,
    ]
