"""Options-flow tests for the Soniox integration (mocked HTTP; offline).

Verifies that the options flow surfaces the live voice catalog (instead of the
removed hardcoded TTS_VOICES), stays savable when the catalog fetch fails, and
round-trips saved options.
"""

from __future__ import annotations

import json
from pathlib import Path

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soniox.const import (
    CONF_API_KEY,
    CONF_ENDPOINT_LATENCY_ADJUSTMENT_LEVEL,
    CONF_MAX_ENDPOINT_DELAY_MS,
    CONF_REGION,
    CONF_STT_ASYNC_MODEL,
    CONF_STT_EXTRA_LANGUAGES,
    CONF_STT_HOME_CONTEXT,
    CONF_STT_LANGUAGE_IDENTIFICATION,
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


def _mock_stt_models(aioclient_mock):
    """Mock GET /v1/models so a loaded entry has per-model STT languages."""
    endpoints = endpoints_for_region(REGION_US)
    aioclient_mock.get(
        endpoints.stt_models_url,
        text=_load_fixture("stt_models_payload.json"),
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


async def _loaded_entry(hass, aioclient_mock, *, options: dict | None = None):
    """Set up an entry so runtime_data carries the API STT languages."""
    _mock_catalog(aioclient_mock)
    _mock_stt_models(aioclient_mock)
    entry = _entry(hass, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    return entry


def _selector(result: dict, key: str):
    """Return (field, selector) of an options-schema entry."""
    return next(
        (marker, sel)
        for marker, sel in result["data_schema"].schema.items()
        if marker.schema == key
    )


def _select_options(selector) -> dict[str, str]:
    """Return the {value: label} mapping of a select selector."""
    return {
        option["value"]: option["label"] for option in selector.config["options"]
    }


def _speed_selector(result: dict):
    """Return (field, selector) of the CONF_TTS_SPEED schema entry."""
    return next(
        (marker, sel)
        for marker, sel in result["data_schema"].schema.items()
        if marker.schema == CONF_TTS_SPEED
    )


def _number_selector(result: dict, key: str):
    """Return (field, selector) of a numeric options-schema entry."""
    return next(
        (marker, sel)
        for marker, sel in result["data_schema"].schema.items()
        if marker.schema == key
    )


def _boolean_selector(result: dict, key: str):
    """Return (field, selector) of a boolean options-schema entry."""
    return next(
        (marker, sel)
        for marker, sel in result["data_schema"].schema.items()
        if marker.schema == key
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
            CONF_STT_MODEL: "stt-rt-v6",
            CONF_STT_ASYNC_MODEL: "stt-async-v6",
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
            CONF_STT_MODEL: "stt-rt-v6",
            CONF_STT_ASYNC_MODEL: "stt-async-v6",
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
            CONF_STT_MODEL: "stt-rt-v6",
            CONF_STT_ASYNC_MODEL: "stt-async-v6",
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
            CONF_STT_MODEL: "stt-rt-v6",
            CONF_STT_ASYNC_MODEL: "stt-async-v6",
            CONF_TTS_MODEL: "tts-rt-v2",
            CONF_TTS_LANGUAGE: "en",
            CONF_TTS_AUDIO_FORMAT: "mp3",
            CONF_TTS_SAMPLE_RATE: "24000",
        },
    )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_TTS_SPEED] == 0.9


async def test_options_endpoint_fields_show_bounds_and_saved_values(
    hass, aioclient_mock
):
    """Both endpoint options render as bounded BOX numbers with saved values."""
    _mock_catalog(aioclient_mock)
    entry = _entry(
        hass,
        options={
            CONF_MAX_ENDPOINT_DELAY_MS: 1000,
            CONF_ENDPOINT_LATENCY_ADJUSTMENT_LEVEL: 2,
        },
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "form"
    assert result["errors"] == {}

    delay_field, delay_selector = _number_selector(
        result, CONF_MAX_ENDPOINT_DELAY_MS
    )
    assert delay_field.default() == 1000
    assert delay_selector.selector_type == "number"
    assert delay_selector.config["min"] == 500
    assert delay_selector.config["max"] == 3000
    assert delay_selector.config["step"] == 100
    assert delay_selector.config["mode"] == "box"

    level_field, level_selector = _number_selector(
        result, CONF_ENDPOINT_LATENCY_ADJUSTMENT_LEVEL
    )
    assert level_field.default() == 2
    assert level_selector.selector_type == "number"
    assert level_selector.config["min"] == 0
    assert level_selector.config["max"] == 3
    assert level_selector.config["step"] == 1
    assert level_selector.config["mode"] == "box"


async def test_options_endpoint_values_persist_as_ints(hass, aioclient_mock):
    """Saving non-default endpoint values stores them as ints."""
    _mock_catalog(aioclient_mock)
    entry = _entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_STT_MODEL: "stt-rt-v6",
            CONF_STT_ASYNC_MODEL: "stt-async-v6",
            CONF_TTS_MODEL: "tts-rt-v2",
            CONF_TTS_VOICE: "Maya",
            CONF_TTS_LANGUAGE: "en",
            CONF_TTS_AUDIO_FORMAT: "mp3",
            CONF_TTS_SAMPLE_RATE: "24000",
            CONF_MAX_ENDPOINT_DELAY_MS: "1000",
            CONF_ENDPOINT_LATENCY_ADJUSTMENT_LEVEL: "2",
        },
    )

    assert result["type"] == "create_entry"
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.options[CONF_MAX_ENDPOINT_DELAY_MS] == 1000
    assert updated.options[CONF_ENDPOINT_LATENCY_ADJUSTMENT_LEVEL] == 2
    assert isinstance(updated.options[CONF_MAX_ENDPOINT_DELAY_MS], int)
    assert isinstance(updated.options[CONF_ENDPOINT_LATENCY_ADJUSTMENT_LEVEL], int)


async def test_options_home_context_boolean_default_off(hass, aioclient_mock):
    """The home-context opt-in renders as a boolean that defaults to off."""
    _mock_catalog(aioclient_mock)
    entry = _entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "form"
    field, selector = _boolean_selector(result, CONF_STT_HOME_CONTEXT)
    assert field.default() is False
    assert selector.selector_type == "boolean"


async def test_options_home_context_persists_when_enabled(hass, aioclient_mock):
    """Saving the home-context opt-in as True stores True in the options."""
    _mock_catalog(aioclient_mock)
    entry = _entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_STT_MODEL: "stt-rt-v6",
            CONF_STT_ASYNC_MODEL: "stt-async-v6",
            CONF_STT_HOME_CONTEXT: True,
            CONF_TTS_MODEL: "tts-rt-v2",
            CONF_TTS_VOICE: "Maya",
            CONF_TTS_LANGUAGE: "en",
            CONF_TTS_AUDIO_FORMAT: "mp3",
            CONF_TTS_SAMPLE_RATE: "24000",
        },
    )

    assert result["type"] == "create_entry"
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.options[CONF_STT_HOME_CONTEXT] is True


async def test_options_home_context_field_without_catalog(hass, aioclient_mock):
    """The home-context opt-in is not catalog-dependent and survives a failure."""
    aioclient_mock.get(
        endpoints_for_region(REGION_US).tts_models_url,
        status=500,
        text="boom",
        headers={"Content-Type": "application/json"},
    )
    entry = _entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["errors"] == {"base": "cannot_connect"}
    field, selector = _boolean_selector(result, CONF_STT_HOME_CONTEXT)
    assert field.default() is False
    assert selector.selector_type == "boolean"


async def test_options_extra_languages_union_of_selected_models(
    hass, aioclient_mock
):
    """The multi-select offers exactly the two models' API language union."""
    entry = await _loaded_entry(hass, aioclient_mock)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "form"
    assert result["errors"] == {}

    field, selector = _selector(result, CONF_STT_EXTRA_LANGUAGES)
    assert field.default() == []
    assert selector.selector_type == "select"
    assert selector.config["multiple"] is True
    assert selector.config["mode"] == "dropdown"

    # stt-rt-v6 first, then stt-async-v6's new languages; "en" is shared.
    assert [option["value"] for option in selector.config["options"]] == [
        "en",
        "de",
        "ja",
        "es",
    ]
    assert _select_options(selector) == {
        "en": "English (en)",
        "de": "German (de)",
        "ja": "Japanese (ja)",
        "es": "Spanish (es)",
    }


async def test_options_extra_languages_render_saved_values(hass, aioclient_mock):
    """A saved hint list and identification flag render as their saved values."""
    entry = await _loaded_entry(
        hass,
        aioclient_mock,
        options={
            CONF_STT_EXTRA_LANGUAGES: ["de", "ja"],
            CONF_STT_LANGUAGE_IDENTIFICATION: True,
        },
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)

    field, _ = _selector(result, CONF_STT_EXTRA_LANGUAGES)
    assert field.default() == ["de", "ja"]
    ident_field, ident_selector = _selector(
        result, CONF_STT_LANGUAGE_IDENTIFICATION
    )
    assert ident_field.default() is True
    assert ident_selector.selector_type == "boolean"


async def test_options_extra_languages_saved_code_missing_from_api(
    hass, aioclient_mock
):
    """A saved code the live list dropped stays a selectable, savable option."""
    entry = await _loaded_entry(
        hass, aioclient_mock, options={CONF_STT_EXTRA_LANGUAGES: ["xx"]}
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)

    field, selector = _selector(result, CONF_STT_EXTRA_LANGUAGES)
    assert field.default() == ["xx"]
    assert _select_options(selector)["xx"] == "xx"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_STT_MODEL: "stt-rt-v6",
            CONF_STT_ASYNC_MODEL: "stt-async-v6",
            CONF_STT_EXTRA_LANGUAGES: ["xx"],
            CONF_STT_LANGUAGE_IDENTIFICATION: False,
            CONF_TTS_MODEL: "tts-rt-v2",
            CONF_TTS_VOICE: "Maya",
            CONF_TTS_LANGUAGE: "en",
            CONF_TTS_AUDIO_FORMAT: "mp3",
            CONF_TTS_SAMPLE_RATE: "24000",
        },
    )

    assert result["type"] == "create_entry"
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.options[CONF_STT_EXTRA_LANGUAGES] == ["xx"]
    await hass.async_block_till_done()


async def test_options_extra_languages_save_dedupes_and_keeps_order(
    hass, aioclient_mock
):
    """Saving stores a deduped, order-preserving list and a real bool."""
    entry = await _loaded_entry(hass, aioclient_mock)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_STT_MODEL: "stt-rt-v6",
            CONF_STT_ASYNC_MODEL: "stt-async-v6",
            CONF_STT_EXTRA_LANGUAGES: ["de", "de", "ja", "de"],
            CONF_STT_LANGUAGE_IDENTIFICATION: True,
            CONF_TTS_MODEL: "tts-rt-v2",
            CONF_TTS_VOICE: "Maya",
            CONF_TTS_LANGUAGE: "en",
            CONF_TTS_AUDIO_FORMAT: "mp3",
            CONF_TTS_SAMPLE_RATE: "24000",
        },
    )

    assert result["type"] == "create_entry"
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.options[CONF_STT_EXTRA_LANGUAGES] == ["de", "ja"]
    assert updated.options[CONF_STT_LANGUAGE_IDENTIFICATION] is True
    await hass.async_block_till_done()


async def test_options_identification_default_off(hass, aioclient_mock):
    """Language identification is an opt-in boolean that defaults to off."""
    _mock_catalog(aioclient_mock)
    entry = _entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "form"
    field, selector = _selector(result, CONF_STT_LANGUAGE_IDENTIFICATION)
    assert field.default() is False
    assert selector.selector_type == "boolean"


async def test_options_identification_persists_when_enabled(
    hass, aioclient_mock
):
    """Saving identification as on stores True in the options."""
    _mock_catalog(aioclient_mock)
    entry = _entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_STT_MODEL: "stt-rt-v6",
            CONF_STT_ASYNC_MODEL: "stt-async-v6",
            CONF_STT_EXTRA_LANGUAGES: [],
            CONF_STT_LANGUAGE_IDENTIFICATION: True,
            CONF_TTS_MODEL: "tts-rt-v2",
            CONF_TTS_VOICE: "Maya",
            CONF_TTS_LANGUAGE: "en",
            CONF_TTS_AUDIO_FORMAT: "mp3",
            CONF_TTS_SAMPLE_RATE: "24000",
        },
    )

    assert result["type"] == "create_entry"
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.options[CONF_STT_LANGUAGE_IDENTIFICATION] is True


async def test_options_missing_stt_language_fields_keep_saved_values(
    hass, aioclient_mock
):
    """A form payload without the STT language fields keeps the saved values."""
    _mock_catalog(aioclient_mock)
    entry = _entry(
        hass,
        options={
            CONF_STT_EXTRA_LANGUAGES: ["es", "de"],
            CONF_STT_LANGUAGE_IDENTIFICATION: True,
        },
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_STT_MODEL: "stt-rt-v6",
            CONF_STT_ASYNC_MODEL: "stt-async-v6",
            CONF_TTS_MODEL: "tts-rt-v2",
            CONF_TTS_VOICE: "Maya",
            CONF_TTS_LANGUAGE: "en",
            CONF_TTS_AUDIO_FORMAT: "mp3",
            CONF_TTS_SAMPLE_RATE: "24000",
        },
    )

    assert result["type"] == "create_entry"
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.options[CONF_STT_EXTRA_LANGUAGES] == ["es", "de"]
    assert updated.options[CONF_STT_LANGUAGE_IDENTIFICATION] is True


async def test_options_extra_languages_fall_back_without_runtime_data(
    hass, aioclient_mock
):
    """Before setup (no runtime_data) the curated list keeps the form usable."""
    _mock_catalog(aioclient_mock)
    entry = _entry(hass, options={CONF_STT_EXTRA_LANGUAGES: ["de"]})

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "form"
    field, selector = _selector(result, CONF_STT_EXTRA_LANGUAGES)
    assert field.default() == ["de"]
    assert "de" in _select_options(selector)
    assert "unknown-code" not in _select_options(selector)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_STT_MODEL: "stt-rt-v6",
            CONF_STT_ASYNC_MODEL: "stt-async-v6",
            CONF_STT_EXTRA_LANGUAGES: ["de"],
            CONF_STT_LANGUAGE_IDENTIFICATION: False,
            CONF_TTS_MODEL: "tts-rt-v2",
            CONF_TTS_VOICE: "Maya",
            CONF_TTS_LANGUAGE: "en",
            CONF_TTS_AUDIO_FORMAT: "mp3",
            CONF_TTS_SAMPLE_RATE: "24000",
        },
    )

    assert result["type"] == "create_entry"


async def test_options_stt_fields_group_in_order(hass, aioclient_mock):
    """STT fields form one group in model → languages → endpoint → home order."""
    _mock_catalog(aioclient_mock)
    entry = _entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    keys = [marker.schema for marker in result["data_schema"].schema]
    assert keys[:7] == [
        CONF_STT_MODEL,
        CONF_STT_ASYNC_MODEL,
        CONF_STT_EXTRA_LANGUAGES,
        CONF_STT_LANGUAGE_IDENTIFICATION,
        CONF_MAX_ENDPOINT_DELAY_MS,
        CONF_ENDPOINT_LATENCY_ADJUSTMENT_LEVEL,
        CONF_STT_HOME_CONTEXT,
    ]
    assert keys[7] == CONF_TTS_MODEL


def test_options_strings_have_stt_language_labels():
    """Both the source strings and the runtime translations label the options."""
    component = Path(__file__).parent.parent / "custom_components" / "soniox"
    for name in ("strings.json", "translations/en.json"):
        strings = json.loads((component / name).read_text(encoding="utf-8"))
        step = strings["options"]["step"]["init"]
        assert step["data"][CONF_STT_EXTRA_LANGUAGES]
        assert step["data"][CONF_STT_LANGUAGE_IDENTIFICATION]
        assert step["data_description"][CONF_STT_EXTRA_LANGUAGES]
        assert step["data_description"][CONF_STT_LANGUAGE_IDENTIFICATION]

