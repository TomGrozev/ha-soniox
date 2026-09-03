"""Tests for Soniox TTS codec streaming (audio format + WS text frames).

Issue #6 acceptance criteria: the codec travels on ATTR_PREFERRED_FORMAT
with CONF_TTS_AUDIO_FORMAT as the saved options-flow default; the streaming
path stays WAV-preferring; REST file output stays mp3; no outgoing message
ever carries HA's internal ``audio_output`` option.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from homeassistant.components.tts import (
    ATTR_AUDIO_OUTPUT,
    ATTR_PREFERRED_FORMAT,
    TTSAudioRequest,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.entity_platform import DATA_ENTITY_PLATFORM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soniox.const import (
    CONF_API_KEY,
    CONF_REGION,
    CONF_TTS_AUDIO_FORMAT,
    DOMAIN,
    REGION_US,
    endpoints_for_region,
)

FIXTURES = Path(__file__).parent / "fixtures"


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
    """Set up a config entry (mocked catalog) and return a TTS entity instance."""
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
    entities = [
        entity
        for platform in hass.data[DATA_ENTITY_PLATFORM][DOMAIN]
        for entity in platform.entities.values()
    ]
    return next(e for e in entities if e.entity_id == "tts.soniox_united_states_text_to_speech")


async def _post_body(hass, aioclient_mock, entity, *, options=None):
    """Run the REST TTS path and return the JSON body aioclient_mock captured."""
    aioclient_mock.post(
        endpoints_for_region(REGION_US).tts_rest_url, content=b"audio"
    )
    audio = await entity.async_get_tts_audio(
        "hello", "en", options or {}
    )
    assert audio is not None
    posts = [call for call in aioclient_mock.mock_calls if call[0] == "POST"]
    assert posts, "expected one REST POST in aioclient_mock.mock_calls"
    return posts[-1][2]


async def _stream(entity, mock_soniox_ws, incoming, options=None, message_gen=None):
    """Run the WS TTS path and return ``(config, ws)``.

    Overrides the shared (synchronous) fake ``send_json`` with an async one,
    because the integration ``await``s it (aiohttp's is a coroutine).
    """
    ws = mock_soniox_ws(incoming)

    async def _send_json(data, **_: object):
        ws.sent.append(data)

    ws.send_json = _send_json

    async def _default_gen():
        yield "hello"

    gen = message_gen if message_gen is not None else _default_gen()
    request = TTSAudioRequest(
        language="en", options=options or {}, message_gen=gen
    )
    response = await entity.async_stream_tts_audio(request)
    async for _ in response.data_gen:
        # The shared fake WS __anext__ has no real await, so give the event
        # loop a tick so the integration's background text pump task (which
        # must interleave with audio reads, as with real aiohttp I/O) runs.
        await asyncio.sleep(0)
    assert ws.sent, "expected a WebSocket config message"
    return ws.sent[0], ws


async def test_no_audio_output_in_supported_or_default_options(
    hass, aioclient_mock
):
    """HA's internal audio_output option is gone from both option surfaces."""
    entity = await _load_entity(hass, aioclient_mock)
    assert ATTR_AUDIO_OUTPUT not in entity.supported_options
    assert ATTR_AUDIO_OUTPUT not in entity.default_options


async def test_rest_body_has_no_audio_output(hass, aioclient_mock):
    """The REST request body never carries audio_output."""
    entity = await _load_entity(hass, aioclient_mock)
    body = await _post_body(hass, aioclient_mock, entity)
    assert "audio_output" not in body


async def test_rest_format_from_preferred_format(hass, aioclient_mock):
    """ATTR_PREFERRED_FORMAT drives the REST audio_format + wav sample rate."""
    entity = await _load_entity(hass, aioclient_mock)
    body = await _post_body(
        hass, aioclient_mock, entity, options={ATTR_PREFERRED_FORMAT: "wav"}
    )
    assert body["audio_format"] == "wav"
    assert body["sample_rate"] == 24000


async def test_rest_format_defaults_to_saved_option(hass, aioclient_mock):
    """REST format falls back to CONF_TTS_AUDIO_FORMAT, else mp3."""
    entity = await _load_entity(
        hass, aioclient_mock, options={CONF_TTS_AUDIO_FORMAT: "wav"}
    )
    body = await _post_body(hass, aioclient_mock, entity)
    assert body["audio_format"] == "wav"

    # A second config entry makes a deduplicated entity id; select the default
    # (no saved format) instance directly rather than by entity id.
    await _load_entity(hass, aioclient_mock)
    default_entity = next(
        e
        for platform in hass.data[DATA_ENTITY_PLATFORM][DOMAIN]
        for e in platform.entities.values()
        if e.entity_id.startswith("tts.")
        and CONF_TTS_AUDIO_FORMAT not in e._entry.options
    )
    default_body = await _post_body(hass, aioclient_mock, default_entity)
    assert default_body["audio_format"] == "mp3"


async def test_tts_speak_file_extension_mp3(hass, aioclient_mock):
    """tts.speak file output is mp3 via the REST path (no preferred format)."""
    entity = await _load_entity(hass, aioclient_mock)
    aioclient_mock.post(
        endpoints_for_region(REGION_US).tts_rest_url, content=b"audio"
    )
    result = await entity.async_get_tts_audio("hello", "en", {})
    assert result == ("mp3", b"audio")


async def test_stream_config_has_no_audio_output(
    hass, aioclient_mock, mock_soniox_ws
):
    """The WS config message never carries audio_output."""
    entity = await _load_entity(hass, aioclient_mock)
    config, _ = await _stream(entity, mock_soniox_ws, [{"terminated": True}])
    assert "audio_output" not in config


async def test_stream_prefers_wav_over_saved_mp3(
    hass, aioclient_mock, mock_soniox_ws
):
    """Streaming stays WAV even when the saved default is mp3."""
    entity = await _load_entity(
        hass, aioclient_mock, options={CONF_TTS_AUDIO_FORMAT: "mp3"}
    )
    config, _ = await _stream(entity, mock_soniox_ws, [{"terminated": True}])
    assert config["audio_format"] == "wav"
    assert config["sample_rate"] == 24000


async def test_stream_prefers_wav_normalizes_pcm(
    hass, aioclient_mock, mock_soniox_ws
):
    """ATTR_PREFERRED_FORMAT 'pcm' normalizes to 'wav' on the stream path."""
    entity = await _load_entity(hass, aioclient_mock)
    config, _ = await _stream(
        entity,
        mock_soniox_ws,
        [{"terminated": True}],
        options={ATTR_PREFERRED_FORMAT: "pcm"},
    )
    assert config["audio_format"] == "wav"


async def test_stream_keeps_pcm_s16le(hass, aioclient_mock, mock_soniox_ws):
    """ATTR_PREFERRED_FORMAT 'pcm_s16le' is kept on the stream path."""
    entity = await _load_entity(hass, aioclient_mock)
    config, _ = await _stream(
        entity,
        mock_soniox_ws,
        [{"terminated": True}],
        options={ATTR_PREFERRED_FORMAT: "pcm_s16le"},
    )
    assert config["audio_format"] == "pcm_s16le"


async def test_entity_declares_streaming_support(hass, aioclient_mock):
    """The entity advertises incremental (streaming input) TTS support."""
    entity = await _load_entity(hass, aioclient_mock)
    assert entity.async_supports_streaming_input() is True


async def test_multi_chunk_stream_emits_text_frames(
    hass, aioclient_mock, mock_soniox_ws
):
    """Each non-empty message_gen chunk produces a text frame + one final EOF."""
    entity = await _load_entity(hass, aioclient_mock)

    async def _gen():
        yield "Hello"
        yield " world"
        yield "!"

    config, ws = await _stream(
        entity,
        mock_soniox_ws,
        [{"audio": base64.b64encode(b"aa").decode(), "terminated": True}],
        message_gen=_gen(),
    )
    stream_id = config["stream_id"]
    assert ws.sent == [
        config,
        {"text": "Hello", "text_end": False, "stream_id": stream_id},
        {"text": " world", "text_end": False, "stream_id": stream_id},
        {"text": "!", "text_end": False, "stream_id": stream_id},
        {"text": "", "text_end": True, "stream_id": stream_id},
    ]


async def test_stream_yields_audio_before_generator_completes(
    hass, aioclient_mock, mock_soniox_ws
):
    """Audio is yielded while message_gen is still producing (incremental)."""
    entity = await _load_entity(hass, aioclient_mock)
    gate = asyncio.Event()

    async def _gen():
        yield "first"
        await gate.wait()
        yield "second"

    ws = mock_soniox_ws(
        [
            {"audio": base64.b64encode(b"chunk-0").decode()},
            {"terminated": True},
        ]
    )

    async def _send_json(data, **_: object):
        ws.sent.append(data)

    ws.send_json = _send_json

    request = TTSAudioRequest(language="en", options={}, message_gen=_gen())
    response = await entity.async_stream_tts_audio(request)

    first = await anext(response.data_gen)
    assert first == b"chunk-0"
    await asyncio.sleep(0)  # give the scheduler one tick
    assert not gate.is_set(), "producer finished before audio was consumed"

    gate.set()
    rest = [chunk async for chunk in response.data_gen]
    assert gate.is_set()
    assert rest == []
