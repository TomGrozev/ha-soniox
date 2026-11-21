"""Tests for issue #15: per-model STT languages, extra hints, identification.

Covers the three acceptance criteria for both STT entities, plus the
unknown-model fallback and the filtering of duplicate or unsupported extras.
"""

from __future__ import annotations

import json
from pathlib import Path

from homeassistant.components.stt import (
    AudioBitRates,
    AudioChannels,
    AudioCodecs,
    AudioFormats,
    AudioSampleRates,
    SpeechMetadata,
    SpeechResultState,
    SpeechToTextEntity,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.entity_platform import DATA_ENTITY_PLATFORM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soniox.catalog import parse_stt_languages
from custom_components.soniox.const import (
    CONF_API_KEY,
    CONF_REGION,
    CONF_STT_EXTRA_LANGUAGES,
    CONF_STT_LANGUAGE_IDENTIFICATION,
    CONF_STT_MODEL,
    DOMAIN,
    REGION_US,
    SUPPORTED_LANGUAGES,
    endpoints_for_region,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _mock_tts_catalog(aioclient_mock):
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
    endpoints = endpoints_for_region(REGION_US)
    aioclient_mock.get(
        endpoints.stt_models_url,
        text=_load_fixture("stt_models_payload.json"),
        headers={"Content-Type": "application/json"},
    )
    return endpoints


def _mock_catalog(aioclient_mock):
    endpoints = _mock_tts_catalog(aioclient_mock)
    _mock_stt_models(aioclient_mock)
    return endpoints


async def _setup_entry(hass, aioclient_mock, options=None):
    """Set up one config entry with the TTS catalog and STT models mocked."""
    _mock_catalog(aioclient_mock)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_API_KEY: "test-api-key", CONF_REGION: REGION_US},
        options=dict(options or {}),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    entities = [
        entity
        for platform in hass.data[DATA_ENTITY_PLATFORM][DOMAIN]
        for entity in platform.entities.values()
    ]
    stt = [e for e in entities if isinstance(e, SpeechToTextEntity)]
    realtime = next(e for e in stt if e._mode == "realtime")
    async_ = next(e for e in stt if e._mode == "async")
    return realtime, async_


def _metadata(language: str = "en") -> SpeechMetadata:
    return SpeechMetadata(
        language=language,
        format=AudioFormats.WAV,
        codec=AudioCodecs.PCM,
        bit_rate=AudioBitRates.BITRATE_16,
        sample_rate=AudioSampleRates.SAMPLERATE_16000,
        channel=AudioChannels.CHANNEL_MONO,
    )


async def _stream_chunks(*chunks: bytes):
    for chunk in chunks:
        yield chunk


def _mock_async_pipeline(aioclient_mock):
    endpoints = endpoints_for_region(REGION_US)
    aioclient_mock.post(endpoints.stt_files_url, json={"id": "file-1"})
    aioclient_mock.post(endpoints.stt_transcriptions_url, json={"id": "tr-1"})
    aioclient_mock.get(
        f"{endpoints.stt_transcriptions_url}/tr-1",
        json={"status": "completed"},
    )
    aioclient_mock.get(
        f"{endpoints.stt_transcriptions_url}/tr-1/transcript",
        json={"text": "hi"},
    )
    aioclient_mock.delete(f"{endpoints.stt_transcriptions_url}/tr-1")
    aioclient_mock.delete(f"{endpoints.stt_files_url}/file-1")
    return endpoints


def _transcription_body(aioclient_mock, url: str) -> dict:
    call = next(
        call
        for call in aioclient_mock.mock_calls
        if call[0].lower() == "post" and str(call[1]) == url
    )
    return call[2]


async def test_supported_languages_match_the_api_for_each_entity(
    hass, aioclient_mock
):
    """Each STT entity advertises the languages the API reports for its model."""
    realtime, async_ = await _setup_entry(hass, aioclient_mock)

    assert realtime.supported_languages == ["en", "de", "ja"]
    assert async_.supported_languages == ["en", "es"]


async def test_realtime_extra_hints_follow_the_pipeline_language(
    hass, aioclient_mock, mock_soniox_ws
):
    """Saved extra hints reach the realtime config after the pipeline language."""
    realtime, _ = await _setup_entry(
        hass,
        aioclient_mock,
        {CONF_STT_EXTRA_LANGUAGES: ["de", "ja"]},
    )
    mock_soniox_ws(
        [
            {"tokens": [{"text": "hi", "is_final": True}]},
            {"finished": True},
        ]
    )
    ws = mock_soniox_ws.connections[-1]

    await realtime.async_process_audio_stream(
        _metadata("en"), _stream_chunks(b"pcmpcm")
    )

    config = ws.sent[0]
    assert config["language_hints"] == ["en", "de", "ja"]


async def test_async_extra_hints_follow_the_pipeline_language(
    hass, aioclient_mock
):
    """Saved extra hints reach the async transcription request after the pipeline language."""
    _, async_ = await _setup_entry(
        hass,
        aioclient_mock,
        {CONF_STT_EXTRA_LANGUAGES: ["es"]},
    )
    endpoints = _mock_async_pipeline(aioclient_mock)

    result = await async_.async_process_audio_stream(
        _metadata("en"), _stream_chunks(b"pcmpcm")
    )

    assert result.result == SpeechResultState.SUCCESS
    body = _transcription_body(aioclient_mock, endpoints.stt_transcriptions_url)
    assert body["language_hints"] == ["en", "es"]


async def test_realtime_identification_flag_when_on(
    hass, aioclient_mock, mock_soniox_ws
):
    """Language identification reaches the realtime config when enabled."""
    realtime, _ = await _setup_entry(
        hass,
        aioclient_mock,
        {CONF_STT_LANGUAGE_IDENTIFICATION: True},
    )
    mock_soniox_ws(
        [
            {"tokens": [{"text": "hi", "is_final": True}]},
            {"finished": True},
        ]
    )
    ws = mock_soniox_ws.connections[-1]

    await realtime.async_process_audio_stream(
        _metadata("en"), _stream_chunks(b"pcmpcm")
    )

    config = ws.sent[0]
    assert config["enable_language_identification"] is True
    # Hints coexist with identification: the pipeline language is still sent.
    assert config["language_hints"] == ["en"]


async def test_async_identification_flag_when_on(hass, aioclient_mock):
    """Language identification reaches the async request when enabled."""
    _, async_ = await _setup_entry(
        hass,
        aioclient_mock,
        {CONF_STT_LANGUAGE_IDENTIFICATION: True},
    )
    endpoints = _mock_async_pipeline(aioclient_mock)

    await async_.async_process_audio_stream(
        _metadata("en"), _stream_chunks(b"pcmpcm")
    )

    body = _transcription_body(aioclient_mock, endpoints.stt_transcriptions_url)
    assert body["enable_language_identification"] is True
    assert body["language_hints"] == ["en"]


async def test_identification_omitted_when_off(
    hass, aioclient_mock, mock_soniox_ws
):
    """Off is the default, so neither entity sends the identification flag."""
    realtime, async_ = await _setup_entry(hass, aioclient_mock)
    mock_soniox_ws(
        [
            {"tokens": [{"text": "hi", "is_final": True}]},
            {"finished": True},
        ]
    )
    ws = mock_soniox_ws.connections[-1]
    await realtime.async_process_audio_stream(
        _metadata("en"), _stream_chunks(b"pcmpcm")
    )
    config = ws.sent[0]
    assert "enable_language_identification" not in config

    endpoints = _mock_async_pipeline(aioclient_mock)
    await async_.async_process_audio_stream(
        _metadata("en"), _stream_chunks(b"pcmpcm")
    )
    body = _transcription_body(aioclient_mock, endpoints.stt_transcriptions_url)
    assert "enable_language_identification" not in body


async def test_duplicate_and_unsupported_extras_are_filtered(
    hass, aioclient_mock, mock_soniox_ws
):
    """Only the model's languages survive, deduped, pipeline language first."""
    realtime, _ = await _setup_entry(
        hass,
        aioclient_mock,
        {CONF_STT_EXTRA_LANGUAGES: ["de", "zz", "de", "ja", "en"]},
    )
    mock_soniox_ws(
        [
            {"tokens": [{"text": "hi", "is_final": True}]},
            {"finished": True},
        ]
    )
    ws = mock_soniox_ws.connections[-1]

    await realtime.async_process_audio_stream(
        _metadata("en"), _stream_chunks(b"pcmpcm")
    )

    config = ws.sent[0]
    assert config["language_hints"] == ["en", "de", "ja"]


async def test_unknown_model_falls_back_to_curated_languages(
    hass, aioclient_mock
):
    """A custom model id absent from /v1/models keeps the curated language list."""
    realtime, _ = await _setup_entry(
        hass,
        aioclient_mock,
        {CONF_STT_MODEL: "stt-custom-v1"},
    )

    assert realtime.supported_languages == SUPPORTED_LANGUAGES


async def test_setup_fails_when_stt_models_fetch_fails(hass, aioclient_mock):
    """An unreachable /v1/models fails setup instead of leaving stale languages."""
    endpoints = _mock_tts_catalog(aioclient_mock)
    aioclient_mock.get(
        endpoints.stt_models_url,
        status=500,
        text="boom",
        headers={"Content-Type": "application/json"},
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_API_KEY: "test-api-key", CONF_REGION: REGION_US},
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is not ConfigEntryState.LOADED


def test_parse_stt_languages_skips_malformed_and_dedupes():
    """Parsing degrades on malformed entries instead of raising."""
    parsed = parse_stt_languages(
        {
            "models": [
                {
                    "id": "m1",
                    "languages": [
                        {"code": "en", "name": "English"},
                        {"code": "en", "name": "English again"},
                        {"name": "No code"},
                        "not a dict",
                    ],
                },
                {"id": "m2"},
                "not a dict",
            ]
        }
    )

    assert [language.code for language in parsed["m1"]] == ["en"]
    assert parsed["m1"][0].name == "English"
    assert "m2" not in parsed


def test_stt_models_fixture_has_distinct_language_sets():
    """Guard the fixture contract the per-entity assertions rely on."""
    payload = json.loads(_load_fixture("stt_models_payload.json"))
    codes = {
        model["id"]: [language["code"] for language in model["languages"]]
        for model in payload["models"]
    }
    assert codes["stt-rt-v6"] == ["en", "de", "ja"]
    assert codes["stt-async-v6"] == ["en", "es"]
