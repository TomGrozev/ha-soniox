"""RED-phase unit tests for the Soniox TTS catalog parser.

Tests the frozen catalog contract (local://catalog-contract.md) against
`custom_components.soniox.catalog`. catalog.py does not exist yet — these tests
are expected to fail on import (the RED state of this TDD slice).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.soniox.catalog import (
    DEFAULT_SPEED_MAX,
    DEFAULT_SPEED_MIN,
    CatalogFetchError,
    CatalogModel,
    CatalogVoice,
    SonioxCatalog,
    parse_catalog,
)
from custom_components.soniox.const import (
    SonioxEndpoints,
    endpoints_for_region,
)

FIXTURES = Path(__file__).parent / "fixtures"

EM_DASH = "\u2014"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# 1. Built-in voices parsed with correct gender/accent labels
# ---------------------------------------------------------------------------


def test_builtin_voices_parse_with_gender_accent_labels():
    catalog = parse_catalog(_load("models_payload.json"), _load("voices_payload.json"))
    v2 = catalog.models["tts-rt-v2"]
    by_id = {v.voice_id: v for v in v2.voices}

    assert set(by_id) == {"Maya", "Daniel", "Nina", "Owen"}
    maya = by_id["Maya"]
    assert maya.label == f"Maya {EM_DASH} female, A steady, clear voice"
    assert maya.gender == "female"
    assert maya.accent == "A steady, clear voice"
    assert maya.custom is False

    assert by_id["Owen"].label == f"Owen {EM_DASH} male, Deep and calm"
    assert by_id["Nina"].gender == "female"


def test_gender_only_label():
    catalog = parse_catalog(
        {
            "models": [
                {
                    "id": "m1",
                    "voices": [{"id": "V1", "gender": "female"}],
                }
            ]
        },
        None,
    )
    voice = catalog.models["m1"].voices[0]
    assert voice.label == f"V1 {EM_DASH} female"
    assert voice.accent == ""


def test_bare_name_label_when_gender_and_accent_missing():
    catalog = parse_catalog(
        {"models": [{"id": "m1", "voices": [{"id": "V1"}]}]},
        None,
    )
    voice = catalog.models["m1"].voices[0]
    assert voice.label == "V1"
    assert voice.gender == ""
    assert voice.accent == ""


def test_accent_only_is_bare_name():
    catalog = parse_catalog(
        {"models": [{"id": "m1", "voices": [{"id": "V1", "description": "Deep"}]}]},
        None,
    )
    voice = catalog.models["m1"].voices[0]
    assert voice.label == "V1"


# ---------------------------------------------------------------------------
# 2. Custom voices parsed with custom=True and per-model readiness
# ---------------------------------------------------------------------------


def test_custom_voices_appear_only_under_ready_model():
    catalog = parse_catalog(_load("models_payload.json"), _load("voices_payload.json"))

    v2 = catalog.models["tts-rt-v2"]
    v1 = catalog.models["tts-rt-v1"]

    assert [c.voice_id for c in v2.custom_voices] == [
        "497f6eca-6276-4993-bfeb-53cbbbba6f08"
    ]
    custom = v2.custom_voices[0]
    assert custom.custom is True
    assert custom.label == f"My Cloned Voice {EM_DASH} custom"
    assert custom.gender == ""
    assert custom.accent == ""

    # tts-rt-v1 readiness status is "failed" -> no custom voices there.
    assert v1.custom_voices == ()


def test_custom_voice_name_falls_back_to_id():
    catalog = parse_catalog(
        {
            "models": [
                {
                    "id": "m1",
                    "voices": [],
                    "supports_speed_adjustment": True,
                    "speed_min": 0.5,
                    "speed_max": 1.5,
                }
            ]
        },
        {
            "voices": [
                {"id": "c1", "models": [{"model": "m1", "status": "ready"}]}
            ]
        },
    )
    custom = catalog.models["m1"].custom_voices[0]
    assert custom.voice_id == "c1"
    assert custom.label == f"c1 {EM_DASH} custom"


def test_custom_voice_ready_for_unknown_model_dropped():
    catalog = parse_catalog(
        {"models": [{"id": "m1", "voices": []}]},
        {"voices": [{"id": "c1", "models": [{"model": "ghost", "status": "ready"}]}]},
    )
    assert catalog.models["m1"].custom_voices == ()


def test_non_ready_status_excluded():
    catalog = parse_catalog(
        {"models": [{"id": "m1", "voices": []}]},
        {
            "voices": [
                {
                    "id": "c1",
                    "models": [
                        {"model": "m1", "status": "processing"},
                        {"model": "m1", "status": "ready"},
                    ],
                }
            ]
        },
    )
    # Duplicate ready entry for the same model appears once.
    assert [c.voice_id for c in catalog.models["m1"].custom_voices] == ["c1"]


def test_readiness_entry_missing_model_ignored():
    catalog = parse_catalog(
        {"models": [{"id": "m1", "voices": []}]},
        {
            "voices": [
                {
                    "id": "c1",
                    "models": [
                        {"status": "ready"},
                        {"model": "m1", "status": "ready"},
                    ],
                }
            ]
        },
    )
    assert [c.voice_id for c in catalog.models["m1"].custom_voices] == ["c1"]


def test_model_with_no_matching_custom_voices_has_empty_tuple():
    catalog = parse_catalog(
        {"models": [{"id": "m1", "voices": []}, {"id": "m2", "voices": []}]},
        {"voices": [{"id": "c1", "models": [{"model": "m1", "status": "ready"}]}]},
    )
    assert catalog.models["m2"].custom_voices == ()


# ---------------------------------------------------------------------------
# 3. Per-model speed bounds and supports_speed_adjustment
# ---------------------------------------------------------------------------


def test_speed_bounds_and_support_for_both_recorded_models():
    catalog = parse_catalog(_load("models_payload.json"), _load("voices_payload.json"))

    v2 = catalog.models["tts-rt-v2"]
    assert v2.supports_speed_adjustment is True
    assert v2.speed_min == 0.7
    assert v2.speed_max == 1.3

    v1 = catalog.models["tts-rt-v1"]
    assert v1.supports_speed_adjustment is False
    # null speed bounds degrade to module defaults.
    assert v1.speed_min == DEFAULT_SPEED_MIN == 0.7
    assert v1.speed_max == DEFAULT_SPEED_MAX == 1.3


def test_missing_speed_fields_degrade_to_defaults():
    catalog = parse_catalog({"models": [{"id": "m1", "voices": []}]}, None)
    model = catalog.models["m1"]
    assert model.speed_min == 0.7
    assert model.speed_max == 1.3
    assert model.supports_speed_adjustment is False


def test_inverted_speed_bounds_are_swapped():
    catalog = parse_catalog(
        {"models": [{"id": "m1", "speed_min": 1.5, "speed_max": 0.5, "voices": []}]},
        None,
    )
    model = catalog.models["m1"]
    assert model.speed_min == 0.5
    assert model.speed_max == 1.5


# ---------------------------------------------------------------------------
# 4. Malformed / absent fields degrade to defaults
# ---------------------------------------------------------------------------


def test_non_bool_supports_speed_adjustment_is_false():
    catalog = parse_catalog(
        {"models": [{"id": "m1", "supports_speed_adjustment": "yes", "voices": []}]},
        None,
    )
    assert catalog.models["m1"].supports_speed_adjustment is False


def test_missing_models_key_yields_empty_dict():
    catalog = parse_catalog({}, None)
    assert catalog.models == {}


def test_top_level_non_dict_yields_empty_dict():
    assert parse_catalog("not a dict", None).models == {}
    assert parse_catalog([1, 2, 3], None).models == {}


def test_none_payloads_yield_empty_catalog():
    catalog = parse_catalog(None, None)
    assert catalog.models == {}
    assert isinstance(catalog, SonioxCatalog)


def test_non_dict_model_entry_skipped():
    catalog = parse_catalog(
        {"models": [{"id": "m1", "voices": []}, "junk", 42, None]}, None
    )
    assert set(catalog.models) == {"m1"}


def test_model_without_id_or_empty_id_skipped():
    catalog = parse_catalog(
        {
            "models": [
                {"voices": []},  # no id
                {"id": "", "voices": []},  # empty id
                {"id": "m1", "voices": []},  # valid
            ]
        },
        None,
    )
    assert set(catalog.models) == {"m1"}


def test_missing_or_non_list_voices_tuple_empty():
    catalog = parse_catalog(
        {"models": [{"id": "m1"}, {"id": "m2", "voices": "oops"}]}, None
    )
    assert catalog.models["m1"].voices == ()
    assert catalog.models["m2"].voices == ()


def test_non_dict_voice_entry_skipped():
    catalog = parse_catalog(
        {
            "models": [
                {
                    "id": "m1",
                    "voices": [
                        {"id": "V1"},
                        "junk",
                        5,
                        None,
                    ],
                }
            ]
        },
        None,
    )
    assert [v.voice_id for v in catalog.models["m1"].voices] == ["V1"]


def test_voice_without_id_or_empty_id_skipped():
    catalog = parse_catalog(
        {
            "models": [
                {
                    "id": "m1",
                    "voices": [
                        {"gender": "female"},  # no id
                        {"id": ""},  # empty id
                        {"id": "V1"},
                    ],
                }
            ]
        },
        None,
    )
    assert [v.voice_id for v in catalog.models["m1"].voices] == ["V1"]


def test_non_str_gender_and_accent_coerce_to_empty():
    catalog = parse_catalog(
        {
            "models": [
                {"id": "m1", "voices": [{"id": "V1", "gender": 3, "description": 7}]}
            ]
        },
        None,
    )
    voice = catalog.models["m1"].voices[0]
    # Both non-str -> bare name label.
    assert voice.label == "V1"
    assert voice.gender == ""
    assert voice.accent == ""


def test_non_list_custom_voices_payload_yields_none():
    catalog = parse_catalog(
        {"models": [{"id": "m1", "voices": []}]},
        {"voices": "oops"},
    )
    assert catalog.models["m1"].custom_voices == ()



# ---------------------------------------------------------------------------
# 5. SonioxEndpoints.voices_url and endpoints_for_region
# ---------------------------------------------------------------------------


def test_endpoints_voices_url_for_each_region():
    expected = {
        "us": "https://api.soniox.com/v1/voices",
        "eu": "https://api.eu.soniox.com/v1/voices",
        "jp": "https://api.jp.soniox.com/v1/voices",
    }
    for region, url in expected.items():
        endpoints = endpoints_for_region(region)
        assert endpoints.voices_url == url
        assert endpoints.region == region


def test_endpoints_voices_url_field_exists():
    endpoints = endpoints_for_region("us")
    assert isinstance(endpoints, SonioxEndpoints)
    assert hasattr(endpoints, "voices_url")
    assert isinstance(endpoints.voices_url, str)
    assert endpoints.voices_url.endswith("/v1/voices")


def test_unknown_region_falls_back_to_default():
    endpoints = endpoints_for_region("xx")
    assert endpoints.region == "us"
    assert endpoints.voices_url == "https://api.soniox.com/v1/voices"


# ---------------------------------------------------------------------------
# Error contract
# ---------------------------------------------------------------------------

def test_catalog_fetch_error_is_homeassistant_error():
    assert issubclass(CatalogFetchError, HomeAssistantError)
