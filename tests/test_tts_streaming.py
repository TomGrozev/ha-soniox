"""Tests for Soniox TTS codec streaming (audio format + WS text frames).

Issue #6 acceptance criteria: the codec travels on ATTR_PREFERRED_FORMAT; no
outgoing message ever carries HA's internal ``audio_output`` option.

Issue #7 acceptance criteria: the streaming path requests the consumer's
preferred format whenever Soniox produces it natively (flac, mp3, aac, pcm,
wav); only formats Soniox cannot stream fall back to wav, so HA's tts
component sees ``extension == final_extension`` and skips its ffmpeg
transcode. ``pcm`` still normalizes to ``pcm_s16le`` (a real Soniox
format), keeping a flac-negotiating satellite (HA Voice PE) transcoding-free.

Issue #12 acceptance criteria: ``tts.speak`` and an Assist-style stream
produce their audio over the Soniox WebSocket, and neither sends an HTTP POST
to the Soniox REST API.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
)
from homeassistant.components.tts import (
    ATTR_AUDIO_OUTPUT,
    ATTR_PREFERRED_FORMAT,
    DATA_TTS_MANAGER,
    TTSAudioRequest,
    async_create_stream,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.entity_platform import DATA_ENTITY_PLATFORM
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockEntityPlatform,
)

from custom_components.soniox.const import (
    CONF_API_KEY,
    CONF_REGION,
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
    aioclient_mock.get(
        endpoints.stt_models_url,
        text=_load_fixture("stt_models_payload.json"),
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


class _RecordingMediaPlayer(MediaPlayerEntity):
    """Media player that resolves the TTS media source like a real player."""

    _attr_name = "Test speaker"
    _attr_supported_features = MediaPlayerEntityFeature.PLAY_MEDIA

    def __init__(self) -> None:
        self.media_id: str | None = None
        self.resolved = None

    async def async_play_media(self, media_type, media_id, **kwargs) -> None:
        self.media_id = media_id
        self.resolved = await media_source.async_resolve_media(
            self.hass, media_id, self.entity_id
        )


async def _setup_media_player(hass) -> _RecordingMediaPlayer:
    """Register a media player that resolves the TTS media source."""
    assert await async_setup_component(hass, "media_source", {}) is True
    assert await async_setup_component(hass, "media_player", {}) is True
    player = _RecordingMediaPlayer()
    platform = MockEntityPlatform(
        hass, domain="media_player", platform_name="test_media_player"
    )
    await platform.async_add_entities([player])
    return player


def _assert_no_http_post(aioclient_mock) -> None:
    """The mocked HTTP client fails unmocked calls; also assert no POST at all."""
    assert not [call for call in aioclient_mock.mock_calls if call[0] == "POST"]


async def _stream(entity, mock_soniox_ws, incoming, options=None, message_gen=None):
    """Run the WS TTS path and return ``(config, ws)``."""
    ws = mock_soniox_ws(incoming)

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


async def test_tts_speak_streams_audio_without_rest(
    hass, aioclient_mock, mock_soniox_ws
):
    """tts.speak produces its audio over the WebSocket, never the REST endpoint."""
    entity = await _load_entity(hass, aioclient_mock)
    hass.data[DATA_TTS_MANAGER].use_file_cache = False
    player = await _setup_media_player(hass)

    ws = mock_soniox_ws(
        [{"audio": base64.b64encode(b"aa").decode(), "terminated": True}]
    )

    await hass.services.async_call(
        "tts",
        "speak",
        {
            "entity_id": entity.entity_id,
            "media_player_entity_id": player.entity_id,
            "message": "hello",
            "cache": False,
            "options": {ATTR_PREFERRED_FORMAT: "mp3"},
        },
        blocking=True,
    )

    assert player.media_id is not None
    assert player.resolved is not None
    assert ws.sent, "expected a WebSocket config message"
    assert ws.sent[0]["audio_format"] == "mp3"
    _assert_no_http_post(aioclient_mock)

    (stream,) = hass.data[DATA_TTS_MANAGER].token_to_stream.values()
    audio = b"".join([chunk async for chunk in stream.async_stream_result()])
    assert audio == b"aa"
    assert stream.extension == "mp3"


async def test_assist_stream_produces_audio_without_rest(
    hass, aioclient_mock, mock_soniox_ws
):
    """The Assist pipeline's TTS stream call yields WebSocket audio and no POST."""
    entity = await _load_entity(hass, aioclient_mock)
    hass.data[DATA_TTS_MANAGER].use_file_cache = False

    ws = mock_soniox_ws(
        [{"audio": base64.b64encode(b"aa").decode(), "terminated": True}]
    )

    stream = async_create_stream(
        hass,
        engine=entity.entity_id,
        language="en",
        options={ATTR_PREFERRED_FORMAT: "mp3"},
    )
    stream.async_set_message("hello")

    audio = b"".join([chunk async for chunk in stream.async_stream_result()])
    assert audio == b"aa"
    assert ws.sent[0]["audio_format"] == "mp3"
    _assert_no_http_post(aioclient_mock)


async def test_stream_config_has_no_audio_output(
    hass, aioclient_mock, mock_soniox_ws
):
    """The WS config message never carries audio_output."""
    entity = await _load_entity(hass, aioclient_mock)
    config, _ = await _stream(entity, mock_soniox_ws, [{"terminated": True}])
    assert "audio_output" not in config


async def test_stream_passes_consumer_flac_through(
    hass, aioclient_mock, mock_soniox_ws
):
    """'flac' (HA Voice PE) is requested natively; no transcode on HA's side."""
    entity = await _load_entity(hass, aioclient_mock)
    config, _ = await _stream(
        entity,
        mock_soniox_ws,
        [{"terminated": True}],
        options={ATTR_PREFERRED_FORMAT: "flac"},
    )
    assert config["audio_format"] == "flac"


async def test_stream_passes_consumer_mp3_through(
    hass, aioclient_mock, mock_soniox_ws
):
    """'mp3' (bare tts.speak default) is requested natively."""
    entity = await _load_entity(hass, aioclient_mock)
    config, _ = await _stream(
        entity,
        mock_soniox_ws,
        [{"terminated": True}],
        options={ATTR_PREFERRED_FORMAT: "mp3"},
    )
    assert config["audio_format"] == "mp3"


async def test_stream_normalizes_pcm_to_pcm_s16le(
    hass, aioclient_mock, mock_soniox_ws
):
    """ATTR_PREFERRED_FORMAT 'pcm' maps to Soniox's real 'pcm_s16le' name."""
    entity = await _load_entity(hass, aioclient_mock)
    config, _ = await _stream(
        entity,
        mock_soniox_ws,
        [{"terminated": True}],
        options={ATTR_PREFERRED_FORMAT: "pcm"},
    )
    assert config["audio_format"] == "pcm_s16le"


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


async def test_stream_response_extension_matches_received_format(
    hass, aioclient_mock, mock_soniox_ws
):
    """TTSAudioResponse.extension equals the requested format.

    HA tts converts only when final_extension != extension, so this equality
    is exactly the 'no ffmpeg conversion' clause for native formats.
    """
    entity = await _load_entity(hass, aioclient_mock)
    ws = mock_soniox_ws([{"terminated": True}])

    async def _gen():
        yield "hello"

    response = await entity.async_stream_tts_audio(
        TTSAudioRequest(
            language="en",
            options={ATTR_PREFERRED_FORMAT: "flac"},
            message_gen=_gen(),
        )
    )
    async for _ in response.data_gen:
        await asyncio.sleep(0)
    assert response.extension == "flac"


async def test_stream_unknown_format_falls_back_to_wav(
    hass, aioclient_mock, mock_soniox_ws
):
    """A format Soniox does not produce natively falls back to wav."""
    entity = await _load_entity(hass, aioclient_mock)
    config, _ = await _stream(
        entity,
        mock_soniox_ws,
        [{"terminated": True}],
        options={ATTR_PREFERRED_FORMAT: "ogg"},
    )
    assert config["audio_format"] == "wav"
    assert config["sample_rate"] == 24000


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
