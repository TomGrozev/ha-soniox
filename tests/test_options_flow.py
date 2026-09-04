"""Options-flow tests for the Soniox integration (mocked HTTP; offline).

Verifies that the options flow surfaces the live voice catalog (instead of the
removed hardcoded TTS_VOICES), stays savable when the catalog fetch fails, and
round-trips saved options.
"""

from __future__ import annotations

from pathlib import Path

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soniox.const import (
    CONF_API_KEY,
    CONF_REGION,
    CONF_STT_ASYNC_MODEL,
    CONF_STT_MODEL,
    CONF_TTS_AUDIO_FORMAT,
    CONF_TTS_LANGUAGE,
    CONF_TTS_MODEL,
    CONF_TTS_SAMPLE_RATE,
    CONF_TTS_SPEED,
    CONF_TTS_VOICE,
    DOMAIN,
    REGION_US,
    endpoints_for_region,
)

FIXTURES = Path(__file__).parent / "fixtures"

CUSTOM_VOICE_ID = "497f6eca-6276-4993-bfeb-53cbbbba6f08"
EM_DASH = "\u2014"


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


def _voice_options(result: dict):
    """Return the {value: label} mapping of the TTS voice selector."""
    _, selector = next(
        (marker, sel)
        for marker, sel in result["data_schema"].schema.items()
        if marker.schema == CONF_TTS_VOICE
    )
    return {option["value"]: option["label"] for option in selector.config["options"]}


def _entry(hass, *, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_API_KEY: "test-api-key", CONF_REGION: REGION_US},
        options=dict(options or {}),
    )
    entry.add_to_hass(hass)
    return entry


def _speed_selector(result: dict):
    """Return (field, selector) of the CONF_TTS_SPEED schema entry."""
    return next(
        (marker, sel)
        for marker, sel in result["data_schema"].schema.items()
        if marker.schema == CONF_TTS_SPEED
    )


async def test_options_init_builds_voice_dropdown_from_catalog(hass, aioclient_mock):
    """Form open surfaces the live catalog voices for the selected model."""
    _mock_catalog(aioclient_mock)
    entry = _entry(hass, options={CONF_TTS_MODEL: "tts-rt-v2"})

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "form"
    assert result["step_id"] == "init"
    assert result["errors"] == {}

    by_value = _voice_options(result)

    # tts-rt-v2's built-ins (with prebuilt labels) plus the ready custom voice.
    assert set(by_value) == {
        "Maya",
        "Daniel",
        "Nina",
        "Owen",
        CUSTOM_VOICE_ID,
    }
    assert by_value["Maya"] == f"Maya {EM_DASH} female"
    assert by_value["Owen"] == f"Owen {EM_DASH} male"
    assert by_value[CUSTOM_VOICE_ID] == f"My Cloned Voice {EM_DASH} custom"
    # A hardcoded-era voice that the tts-rt-v2 catalog does not include is gone.
    assert "Emma" not in by_value


async def test_options_fetch_failure_shows_cannot_connect(hass, aioclient_mock):
    """Catalog fetch failure re-shows the form with a cannot_connect error."""
    aioclient_mock.get(
        endpoints_for_region(REGION_US).tts_models_url,
        status=500,
        text="boom",
        headers={"Content-Type": "application/json"},
    )
    entry = _entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "form"
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_options_fetch_failure_fresh_entry_still_savable(hass, aioclient_mock):
    """A fresh entry (empty options) with a failing fetch can still be saved."""
    aioclient_mock.get(
        endpoints_for_region(REGION_US).tts_models_url,
        status=500,
        text="boom",
        headers={"Content-Type": "application/json"},
    )
    entry = _entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["errors"] == {"base": "cannot_connect"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_STT_MODEL: "stt-rt-v5",
            CONF_STT_ASYNC_MODEL: "stt-async-v5",
            CONF_TTS_MODEL: "tts-rt-v2",
            CONF_TTS_VOICE: "Maya",
            CONF_TTS_LANGUAGE: "en",
            CONF_TTS_AUDIO_FORMAT: "mp3",
            CONF_TTS_SAMPLE_RATE: "24000",
        },
    )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_TTS_VOICE] == "Maya"
    assert "preserved" not in result["data"]
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.options[CONF_TTS_VOICE] == "Maya"


async def test_options_fetch_failure_preserves_saved_voice(hass, aioclient_mock):
    """A previously saved voice survives a failed catalog fetch on re-submit."""
    aioclient_mock.get(
        endpoints_for_region(REGION_US).tts_models_url,
        status=500,
        text="boom",
        headers={"Content-Type": "application/json"},
    )
    entry = _entry(hass, options={CONF_TTS_VOICE: "Daniel"})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["errors"] == {"base": "cannot_connect"}

    # The saved voice remains a valid dropdown choice after a failed fetch.
    by_value = _voice_options(result)
    assert "Daniel" in by_value

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_STT_MODEL: "stt-rt-v5",
            CONF_STT_ASYNC_MODEL: "stt-async-v5",
            CONF_TTS_MODEL: "tts-rt-v2",
            CONF_TTS_VOICE: "Daniel",
            CONF_TTS_LANGUAGE: "en",
            CONF_TTS_AUDIO_FORMAT: "mp3",
            CONF_TTS_SAMPLE_RATE: "24000",
        },
    )
    assert result["type"] == "create_entry"
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.options[CONF_TTS_VOICE] == "Daniel"


async def test_options_save_roundtrip(hass, aioclient_mock):
    """Submitting the form keeps the previously saved voice/model unchanged."""
    _mock_catalog(aioclient_mock)
    saved = {
        CONF_TTS_MODEL: "tts-rt-v2",
        CONF_TTS_VOICE: "Maya",
        CONF_TTS_LANGUAGE: "en",
        CONF_TTS_SAMPLE_RATE: 24000,
    }
    entry = _entry(hass, options=saved)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_STT_MODEL: "stt-rt-v5",
            CONF_STT_ASYNC_MODEL: "stt-async-v5",
            CONF_TTS_MODEL: "tts-rt-v2",
            CONF_TTS_VOICE: "Maya",
            CONF_TTS_LANGUAGE: "en",
            CONF_TTS_AUDIO_FORMAT: "mp3",
            CONF_TTS_SAMPLE_RATE: "24000",
        },
    )

    assert result["type"] == "create_entry"
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.options[CONF_TTS_VOICE] == "Maya"
    assert updated.options[CONF_TTS_MODEL] == "tts-rt-v2"
    assert "preserved" not in updated.options


async def test_options_speed_slider_for_speed_supporting_model(
    hass, aioclient_mock
):
    """A speed-supporting model surfaces a bounded CONF_TTS_SPEED slider."""
    _mock_catalog(aioclient_mock)
    entry = _entry(hass, options={CONF_TTS_MODEL: "tts-rt-v2"})

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "form"
    assert result["errors"] == {}

    field, selector = _speed_selector(result)
    assert field.default() == 1.0
    assert selector.selector_type == "number"
    # Bounds/step come from the mocked catalog's tts-rt-v2 entry.
    assert selector.config["min"] == 0.7
    assert selector.config["max"] == 1.3
    assert selector.config["step"] == 0.05
    assert selector.config["mode"] == "slider"


async def test_options_no_speed_for_model_without_speed_adjustment(
    hass, aioclient_mock
):
    """A model lacking speed adjustment gets no CONF_TTS_SPEED field."""
    _mock_catalog(aioclient_mock)
    entry = _entry(hass, options={CONF_TTS_MODEL: "tts-rt-v1"})

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "form"
    assert result["errors"] == {}
    assert all(
        marker.schema != CONF_TTS_SPEED
        for marker in result["data_schema"].schema
    )


async def test_options_fetch_failure_preserves_saved_speed(hass, aioclient_mock):
    """A failed catalog fetch re-keeps the saved speed on submit."""
    aioclient_mock.get(
        endpoints_for_region(REGION_US).tts_models_url,
        status=500,
        text="boom",
        headers={"Content-Type": "application/json"},
    )
    entry = _entry(
        hass,
        options={
            CONF_TTS_MODEL: "tts-rt-v2",
            CONF_TTS_SPEED: 0.9,
        },
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["errors"] == {"base": "cannot_connect"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_STT_MODEL: "stt-rt-v5",
            CONF_STT_ASYNC_MODEL: "stt-async-v5",
            CONF_TTS_MODEL: "tts-rt-v2",
            CONF_TTS_LANGUAGE: "en",
            CONF_TTS_AUDIO_FORMAT: "mp3",
            CONF_TTS_SAMPLE_RATE: "24000",
        },
    )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_TTS_SPEED] == 0.9
