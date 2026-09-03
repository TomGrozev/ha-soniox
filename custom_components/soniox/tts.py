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
    ATTR_AUDIO_OUTPUT,
    ATTR_PREFERRED_FORMAT,
    ATTR_PREFERRED_SAMPLE_RATE,
    ATTR_VOICE,
    TextToSpeechEntity,
    TtsAudioType,
    TTSAudioRequest,
    TTSAudioResponse,
    Voice,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SonioxConfigEntry
from .const import (
    CONF_API_KEY,
    CONF_REGION,
    CONF_TTS_AUDIO_FORMAT,
    CONF_TTS_LANGUAGE,
    CONF_TTS_MODEL,
    CONF_TTS_SAMPLE_RATE,
    CONF_TTS_VOICE,
    DEFAULT_REGION,
    DEFAULT_TTS_AUDIO_FORMAT,
    DEFAULT_TTS_LANGUAGE,
    DEFAULT_TTS_MODEL,
    DEFAULT_TTS_SAMPLE_RATE,
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
}

# Assist satellites can start playback as soon as the first PCM/WAV chunk
# arrives. MP3 needs a complete frame sequence and is buffered as a file.
_STREAMABLE_FORMATS = {"wav", "pcm_s16le"}
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
            ATTR_AUDIO_OUTPUT,
            ATTR_PREFERRED_FORMAT,
            ATTR_PREFERRED_SAMPLE_RATE,
        ]

    @property
    def default_options(self) -> dict[str, Any]:
        return {
            ATTR_VOICE: self._entry.options.get(CONF_TTS_VOICE, DEFAULT_TTS_VOICE),
            ATTR_AUDIO_OUTPUT: self._entry.options.get(
                CONF_TTS_AUDIO_FORMAT, DEFAULT_TTS_AUDIO_FORMAT
            ),
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
        """Pick a chunk-friendly format so Assist can play audio immediately.

        The entity default is mp3 (good for tts.speak files). Assist merges
        that default into every request, which would force the WebSocket onto
        a buffered container. Prefer wav/pcm instead.
        """
        preferred = options.get(ATTR_PREFERRED_FORMAT)
        if preferred in ("wav", "pcm", "pcm_s16le"):
            return "wav" if preferred == "pcm" else preferred
        explicit = options.get(ATTR_AUDIO_OUTPUT)
        if explicit in _STREAMABLE_FORMATS:
            return explicit
        return _STREAM_DEFAULT_FORMAT

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

    def _build_request_body(
        self, message: str, language: str, options: dict[str, Any]
    ) -> dict[str, Any]:
        voice = options.get(
            ATTR_VOICE,
            self._entry.options.get(CONF_TTS_VOICE, DEFAULT_TTS_VOICE),
        )
        audio_format = options.get(
            ATTR_AUDIO_OUTPUT,
            self._entry.options.get(CONF_TTS_AUDIO_FORMAT, DEFAULT_TTS_AUDIO_FORMAT),
        )
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
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
        except aiohttp.ClientError as err:
            raise HomeAssistantError(
                f"Soniox TTS streaming connection failed: {err}"
            ) from err
