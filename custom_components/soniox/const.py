"""Constants for the Soniox integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

DOMAIN: Final = "soniox"

# Config / options keys
CONF_API_KEY: Final = "api_key"
CONF_REGION: Final = "region"
CONF_STT_MODEL: Final = "stt_model"
CONF_STT_ASYNC_MODEL: Final = "stt_async_model"
CONF_TTS_MODEL: Final = "tts_model"
CONF_TTS_VOICE: Final = "tts_voice"
CONF_TTS_LANGUAGE: Final = "tts_language"
CONF_TTS_AUDIO_FORMAT: Final = "tts_audio_format"
CONF_TTS_SAMPLE_RATE: Final = "tts_sample_rate"
CONF_TTS_SPEED: Final = "tts_speed"
DEFAULT_TTS_SPEED: Final = 1.0
ATTR_SPEED: Final = "speed"  # per-request option key (not an HA constant)

# Regional deployments (https://soniox.com/docs/data-residency)
REGION_US: Final = "us"
REGION_EU: Final = "eu"
REGION_JP: Final = "jp"
DEFAULT_REGION: Final = REGION_US

# host family → (api, stt-rt, tts-rt)
_REGION_HOSTS: Final = {
    REGION_US: ("api.soniox.com", "stt-rt.soniox.com", "tts-rt.soniox.com"),
    REGION_EU: ("api.eu.soniox.com", "stt-rt.eu.soniox.com", "tts-rt.eu.soniox.com"),
    REGION_JP: ("api.jp.soniox.com", "stt-rt.jp.soniox.com", "tts-rt.jp.soniox.com"),
}

REGION_LABELS: Final = {
    REGION_US: "United States",
    REGION_EU: "European Union",
    REGION_JP: "Japan",
}


@dataclass(frozen=True)
class SonioxEndpoints:
    """Resolved REST and WebSocket URLs for a Soniox region."""

    region: str
    stt_files_url: str
    stt_transcriptions_url: str
    stt_websocket_url: str
    tts_rest_url: str
    tts_websocket_url: str
    tts_models_url: str
    voices_url: str


def endpoints_for_region(region: str) -> SonioxEndpoints:
    """Build service URLs for a regional Soniox deployment."""
    api, stt_rt, tts_rt = _REGION_HOSTS.get(region, _REGION_HOSTS[DEFAULT_REGION])
    return SonioxEndpoints(
        region=region if region in _REGION_HOSTS else DEFAULT_REGION,
        stt_files_url=f"https://{api}/v1/files",
        stt_transcriptions_url=f"https://{api}/v1/transcriptions",
        stt_websocket_url=f"wss://{stt_rt}/transcribe-websocket",
        tts_rest_url=f"https://{tts_rt}/tts",
        tts_websocket_url=f"wss://{tts_rt}/tts-websocket",
        tts_models_url=f"https://{api}/v1/tts-models",
        voices_url=f"https://{api}/v1/voices",
    )

# Models (https://soniox.com/docs/stt/models, https://soniox.com/docs/tts/models)
STT_REALTIME_MODELS: Final = ["stt-rt-v5"]
STT_ASYNC_MODELS: Final = ["stt-async-v5"]
TTS_MODELS: Final = ["tts-rt-v2", "tts-rt-v1"]

# Defaults
DEFAULT_STT_MODEL: Final = "stt-rt-v5"
DEFAULT_STT_ASYNC_MODEL: Final = "stt-async-v5"
DEFAULT_TTS_MODEL: Final = "tts-rt-v2"
DEFAULT_TTS_VOICE: Final = "Maya"
DEFAULT_TTS_LANGUAGE: Final = "en"
DEFAULT_TTS_AUDIO_FORMAT: Final = "mp3"
DEFAULT_TTS_SAMPLE_RATE: Final = 24000

# Curated subset of the 60+ languages Soniox advertises. Used as the
# advertised supported_languages list for both STT and TTS — the Soniox
# models actually handle far more codes, but Home Assistant prefers a
# concrete list it can match assist-pipeline languages against.
SUPPORTED_LANGUAGES: Final = [
    "af", "ar", "az", "be", "bg", "bn", "bs", "ca", "cs", "cy", "da", "de",
    "el", "en", "es", "et", "eu", "fa", "fi", "fr", "gl", "gu", "he", "hi",
    "hr", "hu", "hy", "id", "is", "it", "ja", "kk", "kn", "ko", "lt", "lv",
    "mi", "mk", "ml", "mn", "mr", "ms", "ne", "nl", "no", "pa", "pl", "ps",
    "pt", "ro", "ru", "sk", "sl", "sq", "sr", "sv", "sw", "ta", "te", "th",
    "tl", "tr", "uk", "ur", "uz", "vi", "zh",
]


def is_async_stt_model(model: str) -> bool:
    """Return True if the model uses the async (file) STT API."""
    return "async" in model
