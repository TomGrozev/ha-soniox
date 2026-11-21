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

from homeassistant.components.assist_pipeline.pipeline import (
    KEY_ASSIST_PIPELINE,
    async_get_pipelines,
)
from homeassistant.components.homeassistant.exposed_entities import (
    async_should_expose,
)
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
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SonioxConfigEntry
from .catalog import stt_languages_for_model
from .const import (
    CONF_API_KEY,
    CONF_ENDPOINT_LATENCY_ADJUSTMENT_LEVEL,
    CONF_MAX_ENDPOINT_DELAY_MS,
    CONF_REGION,
    CONF_STT_ASYNC_MODEL,
    CONF_STT_EXTRA_LANGUAGES,
    CONF_STT_HOME_CONTEXT,
    CONF_STT_LANGUAGE_IDENTIFICATION,
    CONF_STT_MODEL,
    DEFAULT_ENDPOINT_LATENCY_ADJUSTMENT_LEVEL,
    DEFAULT_MAX_ENDPOINT_DELAY_MS,
    DEFAULT_REGION,
    DEFAULT_STT_ASYNC_MODEL,
    DEFAULT_STT_EXTRA_LANGUAGES,
    DEFAULT_STT_HOME_CONTEXT,
    DEFAULT_STT_LANGUAGE_IDENTIFICATION,
    DEFAULT_STT_MODEL,
    DOMAIN,
    REGION_LABELS,
    is_async_stt_model,
    tts_unique_id,
)

_LOGGER = logging.getLogger(__name__)

# Soniox's preferred raw encoding for low-latency streaming.
_AUDIO_FORMAT = "pcm_s16le"
_ASYNC_POLL_INTERVAL = 0.5
_ASYNC_TIMEOUT = 90
# Soniox finalizes a realtime session within a couple of seconds of the audio
# ending. Ten seconds tolerates a slow link while still ending a stalled
# Assist turn instead of waiting on a socket that never sends `finished`.
_REALTIME_FINALIZE_TIMEOUT = 10.0
# Soniox rejects a context over 8,000 tokens (~10,000 characters)
# (https://docs.soniox.com/stt/concepts/context).
_CONTEXT_TERMS_MAX_CHARS = 10_000
_CONTROL_TOKENS = frozenset({"<end>", "<fin>"})
# Soniox control markers, not speech: <end> marks an endpoint-detected
# segment boundary (https://soniox.com/docs/stt/rt/endpoint-detection) and
# ends the realtime session; <fin> acknowledges a manual finalize and is
# only filtered. Both arrive as ordinary final tokens, so transcript
# assembly must skip them or they end up glued onto the text.


def _transcript_latency_ms(
    elapsed_ms: float, speech_end_ms: float | None, last_audio_ms: float
) -> int:
    """Return ms from the end of speech to the transcript, never negative.

    Token timestamps are audio offsets; `elapsed_ms` and `last_audio_ms` are
    wall-clock offsets from the same session start. Without a token timestamp,
    the last audio chunk is the closest end-of-speech signal.
    """
    if speech_end_ms is None:
        return max(round(elapsed_ms - last_audio_ms), 0)
    return max(round(elapsed_ms - speech_end_ms), 0)


def _home_context_terms(hass: HomeAssistant) -> list[str]:
    """Return home names to send as Soniox context terms.

    The caller sends them only when the user opted in, because the terms
    carry home data to Soniox.
    """
    entity_registry = er.async_get(hass)
    # Areas and floors go first: a large home can spend the whole character
    # budget on entity names, and the short area/floor names help every command.
    candidates: list[object] = [
        area.name for area in ar.async_get(hass).async_list_areas()
    ]
    candidates.extend(floor.name for floor in fr.async_get(hass).async_list_floors())
    for state in hass.states.async_all():
        if not async_should_expose(hass, "conversation", state.entity_id):
            continue
        candidates.append(state.name)
        if entry := entity_registry.async_get(state.entity_id):
            candidates.extend(entry.aliases)

    terms: list[str] = []
    seen: set[str] = set()
    total_chars = 0
    for candidate in candidates:
        # Current Home Assistant stores a non-string sentinel in aliases to
        # mark the computed entity name; there is no text to send for it.
        if not isinstance(candidate, str):
            continue
        name = candidate.strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        # +2 is a conservative allowance for the punctuation between terms.
        added_chars = len(name) + 2 if terms else len(name)
        if total_chars + added_chars > _CONTEXT_TERMS_MAX_CHARS:
            continue
        seen.add(key)
        terms.append(name)
        total_chars += added_chars
    return terms


def _pipeline_pairs_entry_tts(
    hass: HomeAssistant, stt_entity_id: str | None, entry_id: str
) -> bool:
    """Return True when an Assist pipeline pairs this STT with this entry's TTS.

    Warming only pays off when the reply is synthesized by the Soniox TTS entity
    of the same config entry; any other engine leaves the socket unused
    (issue #17). An STT engine is not told which pipeline run called it, so the
    stored pipelines are the only signal available at STT start: a home that
    runs the same Soniox STT in two pipelines with different TTS engines warms
    for both, and the unused socket closes on Soniox's no-audio timeout
    (keepalives are not billed).
    """
    if stt_entity_id is None or hass.data.get(KEY_ASSIST_PIPELINE) is None:
        return False
    tts_entity_id = er.async_get(hass).async_get_entity_id(
        Platform.TTS, DOMAIN, tts_unique_id(entry_id)
    )
    if tts_entity_id is None:
        return False
    return any(
        pipeline.stt_engine == stt_entity_id
        and pipeline.tts_engine == tts_entity_id
        for pipeline in async_get_pipelines(hass)
    )


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
        return [
            language.code
            for language in stt_languages_for_model(
                self._entry.runtime_data.stt_languages, self._selected_model()
            )
        ]

    def _apply_language_options(
        self, request: dict[str, object], model: str, language: str
    ) -> None:
        """Send the pipeline language first, then the saved extra hints.

        Extras are restricted to the languages the selected model reports and
        deduped, so a stale saved option cannot send a hint Soniox rejects.
        Absence of the identification flag means off for Soniox, so only
        opt-in users send it.
        """
        allowed = {
            entry.code
            for entry in stt_languages_for_model(
                self._entry.runtime_data.stt_languages, model
            )
        }
        hints = [language]
        for extra in self._entry.options.get(
            CONF_STT_EXTRA_LANGUAGES, DEFAULT_STT_EXTRA_LANGUAGES
        ):
            if extra not in allowed or extra in hints:
                continue
            hints.append(extra)
        request["language_hints"] = hints
        if self._entry.options.get(
            CONF_STT_LANGUAGE_IDENTIFICATION,
            DEFAULT_STT_LANGUAGE_IDENTIFICATION,
        ):
            request["enable_language_identification"] = True

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
        # Issue #8: STT start is a strong signal a TTS reply is coming, so the
        # TTS websocket is opened speculatively and the response stream reuses
        # it. Issue #17 narrows that to a pipeline that replies through this
        # entry's TTS. Warming must never delay or fail STT.
        try:
            if _pipeline_pairs_entry_tts(
                self.hass, self.entity_id, self._entry.entry_id
            ):
                self._entry.runtime_data.tts_pool.async_warm()
        except Exception as err:  # noqa: BLE001 — warming must not break STT
            _LOGGER.debug("Soniox TTS warm-up could not be started: %s", err)
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
            "enable_endpoint_detection": True,
            "max_endpoint_delay_ms": int(
                self._entry.options.get(
                    CONF_MAX_ENDPOINT_DELAY_MS, DEFAULT_MAX_ENDPOINT_DELAY_MS
                )
            ),
            "endpoint_latency_adjustment_level": int(
                self._entry.options.get(
                    CONF_ENDPOINT_LATENCY_ADJUSTMENT_LEVEL,
                    DEFAULT_ENDPOINT_LATENCY_ADJUSTMENT_LEVEL,
                )
            ),
        }
        self._apply_language_options(config_msg, model, language)
        if self._entry.options.get(
            CONF_STT_HOME_CONTEXT, DEFAULT_STT_HOME_CONTEXT
        ):
            terms = _home_context_terms(self.hass)
            if terms:
                config_msg["context"] = {"terms": terms}

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
        session_started_at = time.monotonic()
        last_audio_at = session_started_at
        speech_end_ms: float | None = None
        ended_on_endpoint = False

        async def send_audio() -> None:
            nonlocal send_error, last_audio_at
            try:
                async for chunk in stream:
                    if not chunk:
                        continue
                    await ws.send_bytes(chunk)
                    last_audio_at = time.monotonic()
                # Empty string signals end-of-audio to Soniox.
                await ws.send_str("")
            except Exception as err:  # noqa: BLE001 (surface after receive)
                send_error = err

        async def receive() -> bool:
            """Collect final tokens; return False when Soniox reports an error."""
            nonlocal ended_on_endpoint, speech_end_ms
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    payload = json.loads(msg.data)
                    if err_code := payload.get("error_code"):
                        _LOGGER.error(
                            "Soniox STT error %s: %s",
                            err_code,
                            payload.get("error_message"),
                        )
                        return False
                    endpoint_reached = False
                    for token in payload.get("tokens", []):
                        text = token.get("text")
                        if not (token.get("is_final") and text):
                            continue
                        marker = text.strip()
                        if marker in _CONTROL_TOKENS:
                            if marker == "<end>":
                                endpoint_reached = True
                            continue
                        final_tokens.append(text)
                        end_ms = token.get("end_ms")
                        if isinstance(end_ms, (int, float)):
                            speech_end_ms = max(speech_end_ms or 0.0, end_ms)
                    # Append the tokens of this frame first: final speech and
                    # <end> can arrive in the same message.
                    if endpoint_reached:
                        ended_on_endpoint = True
                        return True
                    if payload.get("finished"):
                        return True
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    return True
            return True

        send_task = asyncio.create_task(send_audio())
        receive_task = asyncio.create_task(receive())

        error: str | None = None
        try:
            done, _ = await asyncio.wait(
                {send_task, receive_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if receive_task in done:
                # Re-raise a receive failure (bad frame, socket error) as the
                # old inline loop did, instead of returning a partial transcript.
                if not receive_task.result():
                    error = "Soniox error"
            elif send_error is None:
                # Audio is fully sent: Soniox owes us a final reply, but it
                # may keep the socket open and never send it.
                try:
                    if not await asyncio.wait_for(
                        receive_task, _REALTIME_FINALIZE_TIMEOUT
                    ):
                        error = "Soniox error"
                except TimeoutError:
                    _LOGGER.error(
                        "Soniox STT session did not finish within %ss of the "
                        "audio ending; ending the voice turn",
                        _REALTIME_FINALIZE_TIMEOUT,
                    )
                    error = "no final reply"
        finally:
            send_task.cancel()
            receive_task.cancel()
            await asyncio.gather(
                send_task, receive_task, return_exceptions=True
            )

        if error is None and send_error is not None:
            _LOGGER.error("Soniox STT audio upload failed: %s", send_error)
            error = "upload failed"

        text = None if error else ("".join(final_tokens).strip() or None)
        reason = error or (
            "endpoint detected" if ended_on_endpoint else "audio stream ended"
        )
        if error is None and text is None:
            reason = "empty transcript"
        _LOGGER.debug(
            "Soniox STT transcript %d ms after the end of speech (%s)",
            _transcript_latency_ms(
                (time.monotonic() - session_started_at) * 1000,
                speech_end_ms,
                (last_audio_at - session_started_at) * 1000,
            ),
            reason,
        )
        return text

    async def _process_async(
        self,
        metadata: SpeechMetadata,
        stream: AsyncIterable[bytes],
        model: str,
    ) -> SpeechResult:
        """Buffer the utterance, upload it, and poll the async model for a transcript."""
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
        body: dict[str, object] = {
            "model": model,
            "file_id": file_id,
            "client_reference_id": "home-assistant",
        }
        self._apply_language_options(body, model, language)
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
            stripped = text.rstrip()
            for marker in _CONTROL_TOKENS:
                stripped = stripped.removesuffix(marker)
            return stripped.strip() or None
        tokens = payload.get("tokens") or []
        joined = "".join(
            token["text"]
            for token in tokens
            if isinstance(token, dict)
            and isinstance(token.get("text"), str)
            and token["text"].strip() not in _CONTROL_TOKENS
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
