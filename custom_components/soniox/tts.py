"""Soniox text-to-speech platform."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import aiohttp
from homeassistant.components.tts import (
    ATTR_PREFERRED_FORMAT,
    ATTR_PREFERRED_SAMPLE_RATE,
    ATTR_VOICE,
    TextToSpeechEntity,
    TTSAudioRequest,
    TTSAudioResponse,
    TtsAudioType,
    Voice,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SonioxConfigEntry
from .catalog import DEFAULT_SPEED_MAX, DEFAULT_SPEED_MIN
from .const import (
    ATTR_SPEED,
    CONF_API_KEY,
    CONF_REGION,
    CONF_TTS_AUDIO_FORMAT,
    CONF_TTS_LANGUAGE,
    CONF_TTS_MODEL,
    CONF_TTS_SAMPLE_RATE,
    CONF_TTS_SPEED,
    CONF_TTS_VOICE,
    DEFAULT_REGION,
    DEFAULT_TTS_AUDIO_FORMAT,
    DEFAULT_TTS_LANGUAGE,
    DEFAULT_TTS_MODEL,
    DEFAULT_TTS_SAMPLE_RATE,
    DEFAULT_TTS_SPEED,
    DEFAULT_TTS_VOICE,
    DOMAIN,
    REGION_LABELS,
    SUPPORTED_LANGUAGES,
)

_LOGGER = logging.getLogger(__name__)

# Map Soniox audio_format → file extension HA expects back.
_EXTENSION_BY_FORMAT = {
    "mp3": "mp3",
    "wav": "wav",
    "pcm_s16le": "pcm",
    "flac": "flac",
    "aac": "aac",
}

# _STREAMABLE_FORMATS revisits #6's wav-preferring bias (issue #7): wav
# guarantees an instant first PCM chunk (progressive playback), but it also
# guarantees an HA-side ffmpeg transcode whenever the consumer prefers
# flac/mp3/aac — HA converts whenever its requested extension differs from
# the engine's (extension != final_extension in tts/__init__.py). For the
# common case the consumer downloads the finished file (stream_response
# false) progressive first-chunk playback never happens and the transcode
# is pure added latency (subprocess spawn + untuned -probesize in HA core).
# So pass the consumer's preferred format straight through when Soniox can
# produce it, and keep wav only as the fallback for formats Soniox cannot
# stream (e.g. ogg/opus containers). The wav tradeoff still exists for
# playback-while-downloading consumers on constrained devices. Soniox
# emits mp3, flac, aac, pcm (s16le) and wav natively over the WebSocket.
_STREAMABLE_FORMATS = {"mp3", "wav", "flac", "aac", "pcm_s16le"}
_STREAM_DEFAULT_FORMAT = "wav"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SonioxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Register the Soniox TTS entity for this config entry."""
    async_add_entities([SonioxTTSEntity(entry)])


class SonioxTTSEntity(TextToSpeechEntity):
    """Soniox TTS — full-message REST path, streaming WebSocket path."""

    _attr_has_entity_name = True
    _attr_name = "Text-to-Speech"

    def __init__(self, entry: SonioxConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}-tts"
        region = entry.data.get(CONF_REGION, DEFAULT_REGION)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Soniox ({REGION_LABELS.get(region, REGION_LABELS[DEFAULT_REGION])})",
            manufacturer="Soniox",
            model="Speech AI",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def default_language(self) -> str:
        return self._entry.options.get(CONF_TTS_LANGUAGE, DEFAULT_TTS_LANGUAGE)

    @property
    def supported_languages(self) -> list[str]:
        return SUPPORTED_LANGUAGES

    @property
    def supported_options(self) -> list[str]:
        return [
            ATTR_VOICE,
            ATTR_SPEED,
            ATTR_PREFERRED_FORMAT,
            ATTR_PREFERRED_SAMPLE_RATE,
        ]

    @property
    def default_options(self) -> dict[str, Any]:
        return {
            ATTR_VOICE: self._entry.options.get(CONF_TTS_VOICE, DEFAULT_TTS_VOICE),
            ATTR_SPEED: self._entry.options.get(CONF_TTS_SPEED, DEFAULT_TTS_SPEED),
        }

    @callback
    def async_get_supported_voices(self, language: str) -> list[Voice]:
        # Soniox voices speak every supported language, so the same list applies.
        # Pull from the pre-parsed live catalog; a miss on the selected model
        # degrades to an empty picker rather than a stale hardcoded list.
        model_id = self._entry.options.get(CONF_TTS_MODEL, DEFAULT_TTS_MODEL)
        model = self._entry.runtime_data.catalog.models.get(model_id)
        voices = model.voices + model.custom_voices if model else ()
        return [Voice(voice_id=v.voice_id, name=v.label) for v in voices]

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict[str, Any]
    ) -> TtsAudioType:
        """One-shot synthesis via the REST endpoint."""
        _LOGGER.debug("Soniox TTS path: REST (async_get_tts_audio)")
        body = self._build_request_body(message, language, options)
        session = async_get_clientsession(self.hass)
        api_key: str = self._entry.data[CONF_API_KEY]

        try:
            async with session.post(
                self._entry.runtime_data.endpoints.tts_rest_url,
                json=body,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    _LOGGER.error("Soniox TTS HTTP %s: %s", resp.status, err_text)
                    raise HomeAssistantError(f"Soniox TTS error: {resp.status}")
                audio = await resp.read()
        except aiohttp.ClientError as err:
            raise HomeAssistantError(f"Soniox TTS connection failed: {err}") from err

        extension = _EXTENSION_BY_FORMAT.get(body["audio_format"], "mp3")
        return extension, audio

    async def async_stream_tts_audio(
        self, request: TTSAudioRequest
    ) -> TTSAudioResponse:
        """Streaming synthesis via the Soniox TTS WebSocket.

        Lets HA pipe text chunks (e.g. from an LLM) and get audio back with
        sub-sentence latency.
        """
        audio_format = self._resolve_stream_format(request.options)
        _LOGGER.debug(
            "Soniox TTS path: WebSocket stream (format=%s)", audio_format
        )
        extension = _EXTENSION_BY_FORMAT.get(audio_format, "wav")
        data_gen = self._stream_audio(request, audio_format)
        return TTSAudioResponse(extension=extension, data_gen=data_gen)

    def _resolve_stream_format(self, options: dict[str, Any]) -> str:
        """Request the consumer's preferred format when Soniox makes it natively.

        HA's tts manager skips its ffmpeg conversion exactly when the format we
        stream back matches the consumer's requested extension, so a native
        match (flac satellite, mp3 tts.speak) avoids the transcode entirely.
        Only formats Soniox cannot produce over the WebSocket fall back to wav
        (see _STREAMABLE_FORMATS for the progressive-playback tradeoff).
        """
        preferred = options.get(ATTR_PREFERRED_FORMAT)
        if preferred == "pcm":
            preferred = "pcm_s16le"
        if isinstance(preferred, str) and preferred in _STREAMABLE_FORMATS:
            return preferred
        return _STREAM_DEFAULT_FORMAT

    def _resolve_request_format(self, options: dict[str, Any]) -> str:
        """Resolve the codec for a one-shot REST request.

        Callers request the output format on ATTR_PREFERRED_FORMAT (Assist
        pipelines put tts_audio_output there); the saved options-flow default
        is the fallback, keeping tts.speak file output on the saved format.
        """
        preferred = options.get(ATTR_PREFERRED_FORMAT)
        if isinstance(preferred, str) and preferred:
            return preferred
        return self._entry.options.get(CONF_TTS_AUDIO_FORMAT, DEFAULT_TTS_AUDIO_FORMAT)

    def _resolve_sample_rate(self, options: dict[str, Any]) -> int:
        preferred = options.get(ATTR_PREFERRED_SAMPLE_RATE)
        if preferred:
            try:
                return int(preferred)
            except (TypeError, ValueError):
                pass
        return int(
            self._entry.options.get(CONF_TTS_SAMPLE_RATE, DEFAULT_TTS_SAMPLE_RATE)
        )

    def _resolve_speed(self, options: dict[str, Any]) -> float | None:
        """Resolve the effective Soniox speed.

        Returns None when the selected model lacks speed adjustment (speed not
        sent). Clamps the per-request ATTR_SPEED value, else the CONF_TTS_SPEED
        option default, to the model's speed_min/speed_max. Missing catalog model
        entry ⇒ fall back to the payload default bounds (catalog defaults
        0.7/1.3 surface through CatalogModel when the payload omits bounds).
        """
        model_id = self._entry.options.get(CONF_TTS_MODEL, DEFAULT_TTS_MODEL)
        model = self._entry.runtime_data.catalog.models.get(model_id)
        if model is not None and not model.supports_speed_adjustment:
            return None

        try:
            speed = float(
                options.get(
                    ATTR_SPEED,
                    self._entry.options.get(CONF_TTS_SPEED, DEFAULT_TTS_SPEED),
                )
            )
        except (TypeError, ValueError):
            _LOGGER.debug(
                "Soniox TTS invalid speed value: %r", options.get(ATTR_SPEED)
            )
            return None

        speed_min = model.speed_min if model is not None else DEFAULT_SPEED_MIN
        speed_max = model.speed_max if model is not None else DEFAULT_SPEED_MAX
        return max(speed_min, min(speed_max, speed))

    def _build_request_body(
        self, message: str, language: str, options: dict[str, Any]
    ) -> dict[str, Any]:
        voice = options.get(
            ATTR_VOICE,
            self._entry.options.get(CONF_TTS_VOICE, DEFAULT_TTS_VOICE),
        )
        audio_format = self._resolve_request_format(options)
        body: dict[str, Any] = {
            "model": self._entry.options.get(CONF_TTS_MODEL, DEFAULT_TTS_MODEL),
            "language": (language or self.default_language).split("-", 1)[0].lower(),
            "voice": voice,
            "audio_format": audio_format,
            "text": message,
        }
        if audio_format.startswith("pcm") or audio_format == "wav":
            body["sample_rate"] = int(
                self._entry.options.get(CONF_TTS_SAMPLE_RATE, DEFAULT_TTS_SAMPLE_RATE)
            )
        if (speed := self._resolve_speed(options)) is not None:
            body["speed"] = speed
        return body

    async def _stream_audio(
        self, request: TTSAudioRequest, audio_format: str
    ) -> AsyncGenerator[bytes]:
        """Drive the Soniox TTS WebSocket and yield decoded audio chunks."""
        session = async_get_clientsession(self.hass)
        api_key: str = self._entry.data[CONF_API_KEY]
        model = self._entry.options.get(CONF_TTS_MODEL, DEFAULT_TTS_MODEL)
        voice = request.options.get(
            ATTR_VOICE,
            self._entry.options.get(CONF_TTS_VOICE, DEFAULT_TTS_VOICE),
        )
        language = (request.language or self.default_language).split("-", 1)[0].lower()
        sample_rate = self._resolve_sample_rate(request.options)
        stream_id = uuid.uuid4().hex

        try:
            async with session.ws_connect(
                self._entry.runtime_data.endpoints.tts_websocket_url,
                heartbeat=30,
                max_msg_size=0,
            ) as ws:
                config: dict[str, Any] = {
                    "api_key": api_key,
                    "model": model,
                    "language": language,
                    "voice": voice,
                    "audio_format": audio_format,
                    "stream_id": stream_id,
                }
                if audio_format.startswith("pcm") or audio_format == "wav":
                    config["sample_rate"] = sample_rate
                if (speed := self._resolve_speed(request.options)) is not None:
                    config["speed"] = speed
                await ws.send_json(config)

                async def pump_text() -> None:
                    async for chunk in request.message_gen:
                        if chunk:
                            await ws.send_json(
                                {
                                    "text": chunk,
                                    "text_end": False,
                                    "stream_id": stream_id,
                                }
                            )
                    await ws.send_json(
                        {"text": "", "text_end": True, "stream_id": stream_id}
                    )

                pump_task = asyncio.create_task(pump_text())
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            payload = json.loads(msg.data)
                            if err := payload.get("error_code"):
                                _LOGGER.error(
                                    "Soniox TTS error %s: %s",
                                    err,
                                    payload.get("error_message"),
                                )
                                break
                            if payload.get("stream_id") not in (None, stream_id):
                                continue
                            if audio_b64 := payload.get("audio"):
                                yield base64.b64decode(audio_b64)
                            if payload.get("terminated") or payload.get("audio_end"):
                                break
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
                finally:
                    pump_task.cancel()
                    try:
                        await pump_task
                    except (asyncio.CancelledError, Exception) as err:  # noqa: BLE001
                        _LOGGER.debug("Soniox TTS stream pump cancelled: %s", err)
        except aiohttp.ClientError as err:
            raise HomeAssistantError(
                f"Soniox TTS streaming connection failed: {err}"
            ) from err
