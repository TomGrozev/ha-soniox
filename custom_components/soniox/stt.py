"""Soniox speech-to-text platform — realtime WebSocket and async file APIs."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import wave
from collections.abc import AsyncIterable
from typing import Literal

import aiohttp

from homeassistant.components.stt import (
    AudioBitRates,
    AudioChannels,
    AudioCodecs,
    AudioFormats,
    AudioSampleRates,
    SpeechMetadata,
    SpeechResult,
    SpeechResultState,
    SpeechToTextEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SonioxConfigEntry
from .const import (
    CONF_API_KEY,
    CONF_REGION,
    CONF_STT_ASYNC_MODEL,
    CONF_STT_MODEL,
    DEFAULT_REGION,
    DEFAULT_STT_ASYNC_MODEL,
    DEFAULT_STT_MODEL,
    DOMAIN,
    REGION_LABELS,
    SUPPORTED_LANGUAGES,
    is_async_stt_model,
)

_LOGGER = logging.getLogger(__name__)

# Soniox's preferred raw encoding for low-latency streaming.
_AUDIO_FORMAT = "pcm_s16le"
_ASYNC_POLL_INTERVAL = 0.5
_ASYNC_TIMEOUT = 90


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SonioxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Register realtime and async Soniox STT entities for this config entry."""
    async_add_entities(
        [
            SonioxSTTEntity(entry, mode="realtime"),
            SonioxSTTEntity(entry, mode="async"),
        ]
    )


class SonioxSTTEntity(SpeechToTextEntity):
    """Transcribes Home Assistant audio with Soniox realtime or async STT."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: SonioxConfigEntry,
        mode: Literal["realtime", "async"],
    ) -> None:
        self._entry = entry
        self._mode = mode
        if mode == "async":
            self._attr_name = "Speech-to-Text (Async)"
            self._attr_unique_id = f"{entry.entry_id}-stt-async"
        else:
            self._attr_name = "Speech-to-Text"
            self._attr_unique_id = f"{entry.entry_id}-stt"
        region = entry.data.get(CONF_REGION, DEFAULT_REGION)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Soniox ({REGION_LABELS.get(region, REGION_LABELS[DEFAULT_REGION])})",
            manufacturer="Soniox",
            model="Speech AI",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def supported_languages(self) -> list[str]:
        return SUPPORTED_LANGUAGES

    @property
    def supported_formats(self) -> list[AudioFormats]:
        # Assist streams raw PCM labeled as WAV (no container header).
        return [AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[AudioCodecs]:
        return [AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[AudioBitRates]:
        return [AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[AudioSampleRates]:
        return [
            AudioSampleRates.SAMPLERATE_8000,
            AudioSampleRates.SAMPLERATE_16000,
            AudioSampleRates.SAMPLERATE_44100,
            AudioSampleRates.SAMPLERATE_48000,
        ]

    @property
    def supported_channels(self) -> list[AudioChannels]:
        return [AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(
        self, metadata: SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> SpeechResult:
        """Dispatch to the realtime WebSocket or the async file API."""
        model = self._selected_model()
        if self._mode == "async" or is_async_stt_model(model):
            return await self._process_async(metadata, stream, model)
        return await self._process_realtime(metadata, stream, model)

    def _selected_model(self) -> str:
        if self._mode == "async":
            return self._entry.options.get(
                CONF_STT_ASYNC_MODEL, DEFAULT_STT_ASYNC_MODEL
            )
        return self._entry.options.get(CONF_STT_MODEL, DEFAULT_STT_MODEL)

    async def _process_realtime(
        self,
        metadata: SpeechMetadata,
        stream: AsyncIterable[bytes],
        model: str,
    ) -> SpeechResult:
        """Open a Soniox realtime session, push audio, collect the transcript."""
        api_key: str = self._entry.data[CONF_API_KEY]
        language = (metadata.language or "en").split("-", 1)[0].lower()

        config_msg = {
            "api_key": api_key,
            "model": model,
            "audio_format": _AUDIO_FORMAT,
            "sample_rate": int(metadata.sample_rate),
            "num_channels": int(metadata.channel),
            "language_hints": [language],
            "enable_endpoint_detection": True,
        }

        session = async_get_clientsession(self.hass)
        try:
            async with session.ws_connect(
                self._entry.runtime_data.endpoints.stt_websocket_url,
                heartbeat=30,
                max_msg_size=0,
            ) as ws:
                await ws.send_json(config_msg)
                text = await self._run_session(ws, stream)
        except aiohttp.ClientError as err:
            _LOGGER.error("Soniox STT connection failed: %s", err)
            return SpeechResult(None, SpeechResultState.ERROR)
        except TimeoutError:
            _LOGGER.error("Soniox STT timed out")
            return SpeechResult(None, SpeechResultState.ERROR)

        if not text:
            return SpeechResult("", SpeechResultState.ERROR)
        return SpeechResult(text, SpeechResultState.SUCCESS)

    async def _run_session(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        stream: AsyncIterable[bytes],
    ) -> str | None:
        """Pump audio in and collect final tokens out, concurrently."""
        final_tokens: list[str] = []
        send_error: BaseException | None = None

        async def send_audio() -> None:
            nonlocal send_error
            try:
                async for chunk in stream:
                    if not chunk:
                        continue
                    await ws.send_bytes(chunk)
                # Empty string signals end-of-audio to Soniox.
                await ws.send_str("")
            except Exception as err:  # noqa: BLE001 — surface in receive loop
                send_error = err

        send_task = asyncio.create_task(send_audio())

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    payload = json.loads(msg.data)
                    if err_code := payload.get("error_code"):
                        _LOGGER.error(
                            "Soniox STT error %s: %s",
                            err_code,
                            payload.get("error_message"),
                        )
                        return None
                    for token in payload.get("tokens", []):
                        if token.get("is_final") and (text := token.get("text")):
                            final_tokens.append(text)
                    if payload.get("finished"):
                        break
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        finally:
            send_task.cancel()
            try:
                await send_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        if send_error is not None:
            _LOGGER.error("Soniox STT audio upload failed: %s", send_error)
            return None

        return "".join(final_tokens).strip() or None

    async def _process_async(
        self,
        metadata: SpeechMetadata,
        stream: AsyncIterable[bytes],
        model: str,
    ) -> SpeechResult:
        """Buffer the utterance, upload it, and poll stt-async-v5 for a transcript."""
        pcm = bytearray()
        async for chunk in stream:
            if chunk:
                pcm.extend(chunk)
        if not pcm:
            _LOGGER.error("Soniox async STT received no audio")
            return SpeechResult(None, SpeechResultState.ERROR)

        wav_bytes = _as_wav(
            bytes(pcm),
            sample_rate=int(metadata.sample_rate),
            channels=int(metadata.channel),
            sample_width=max(int(metadata.bit_rate) // 8, 1),
        )
        language = (metadata.language or "en").split("-", 1)[0].lower()
        session = async_get_clientsession(self.hass)
        api_key: str = self._entry.data[CONF_API_KEY]
        headers = {"Authorization": f"Bearer {api_key}"}
        file_id: str | None = None
        transcription_id: str | None = None

        try:
            file_id = await self._upload_file(session, headers, wav_bytes)
            transcription_id = await self._create_transcription(
                session, headers, model, file_id, language
            )
            if not await self._wait_for_transcription(
                session, headers, transcription_id
            ):
                return SpeechResult(None, SpeechResultState.ERROR)
            text = await self._get_transcript(session, headers, transcription_id)
        except aiohttp.ClientError as err:
            _LOGGER.error("Soniox async STT request failed: %s", err)
            return SpeechResult(None, SpeechResultState.ERROR)
        except TimeoutError:
            _LOGGER.error("Soniox async STT timed out")
            return SpeechResult(None, SpeechResultState.ERROR)
        finally:
            await self._cleanup_async_job(session, headers, file_id, transcription_id)

        if not text:
            return SpeechResult("", SpeechResultState.ERROR)
        return SpeechResult(text, SpeechResultState.SUCCESS)

    async def _upload_file(
        self,
        session: aiohttp.ClientSession,
        headers: dict[str, str],
        wav_bytes: bytes,
    ) -> str:
        form = aiohttp.FormData()
        form.add_field(
            "file",
            wav_bytes,
            filename="speech.wav",
            content_type="audio/wav",
        )
        async with session.post(
            self._entry.runtime_data.endpoints.stt_files_url,
            data=form,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status not in (200, 201):
                err_text = await resp.text()
                _LOGGER.error("Soniox file upload HTTP %s: %s", resp.status, err_text)
                raise aiohttp.ClientError(f"file upload failed: {resp.status}")
            payload = await resp.json()
        file_id = payload.get("id")
        if not file_id:
            raise aiohttp.ClientError("file upload returned no id")
        return file_id

    async def _create_transcription(
        self,
        session: aiohttp.ClientSession,
        headers: dict[str, str],
        model: str,
        file_id: str,
        language: str,
    ) -> str:
        body = {
            "model": model,
            "file_id": file_id,
            "language_hints": [language],
            "client_reference_id": "home-assistant",
        }
        async with session.post(
            self._entry.runtime_data.endpoints.stt_transcriptions_url,
            json=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status not in (200, 201):
                err_text = await resp.text()
                _LOGGER.error(
                    "Soniox create transcription HTTP %s: %s", resp.status, err_text
                )
                raise aiohttp.ClientError(
                    f"create transcription failed: {resp.status}"
                )
            payload = await resp.json()
        transcription_id = payload.get("id")
        if not transcription_id:
            raise aiohttp.ClientError("create transcription returned no id")
        return transcription_id

    async def _wait_for_transcription(
        self,
        session: aiohttp.ClientSession,
        headers: dict[str, str],
        transcription_id: str,
    ) -> bool:
        deadline = time.monotonic() + _ASYNC_TIMEOUT
        url = f"{self._entry.runtime_data.endpoints.stt_transcriptions_url}/{transcription_id}"
        while True:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    _LOGGER.error(
                        "Soniox poll transcription HTTP %s: %s",
                        resp.status,
                        err_text,
                    )
                    return False
                payload = await resp.json()

            status = payload.get("status")
            if status == "completed":
                return True
            if status in ("error", "failed"):
                _LOGGER.error(
                    "Soniox async STT %s: %s",
                    payload.get("error_type") or status,
                    payload.get("error_message"),
                )
                return False
            if time.monotonic() >= deadline:
                _LOGGER.error("Soniox async STT timed out waiting for %s", status)
                return False
            await asyncio.sleep(_ASYNC_POLL_INTERVAL)

    async def _get_transcript(
        self,
        session: aiohttp.ClientSession,
        headers: dict[str, str],
        transcription_id: str,
    ) -> str | None:
        url = f"{self._entry.runtime_data.endpoints.stt_transcriptions_url}/{transcription_id}/transcript"
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                err_text = await resp.text()
                _LOGGER.error(
                    "Soniox get transcript HTTP %s: %s", resp.status, err_text
                )
                return None
            payload = await resp.json()
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        tokens = payload.get("tokens") or []
        joined = "".join(
            token.get("text", "") for token in tokens if isinstance(token, dict)
        ).strip()
        return joined or None

    async def _cleanup_async_job(
        self,
        session: aiohttp.ClientSession,
        headers: dict[str, str],
        file_id: str | None,
        transcription_id: str | None,
    ) -> None:
        """Best-effort delete so completed jobs do not pile up against account limits."""
        if transcription_id:
            try:
                async with session.delete(
                    f"{self._entry.runtime_data.endpoints.stt_transcriptions_url}/{transcription_id}",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status >= 400 and resp.status != 404:
                        _LOGGER.debug(
                            "Could not delete Soniox transcription %s: HTTP %s",
                            transcription_id,
                            resp.status,
                        )
            except aiohttp.ClientError as err:
                _LOGGER.debug("Could not delete Soniox transcription: %s", err)
        if file_id:
            try:
                async with session.delete(
                    f"{self._entry.runtime_data.endpoints.stt_files_url}/{file_id}",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status >= 400 and resp.status != 404:
                        _LOGGER.debug(
                            "Could not delete Soniox file %s: HTTP %s",
                            file_id,
                            resp.status,
                        )
            except aiohttp.ClientError as err:
                _LOGGER.debug("Could not delete Soniox file: %s", err)


def _as_wav(
    audio: bytes, sample_rate: int, channels: int, sample_width: int
) -> bytes:
    """Wrap raw PCM in a WAV container; leave existing RIFF data unchanged."""
    if audio.startswith(b"RIFF"):
        return audio
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio)
    return buffer.getvalue()
