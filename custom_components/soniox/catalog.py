"""Live Soniox TTS capability catalog (models and voices).

Fetches the platform's published TTS models and custom voices so the
integration can adapt its advertised options without hardcoding them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Final

import aiohttp

from homeassistant.exceptions import HomeAssistantError

from .const import SonioxEndpoints

DEFAULT_SPEED_MIN: Final = 0.7
DEFAULT_SPEED_MAX: Final = 1.3

# U+2014 em dash used to join voice label parts.
_EM_DASH: Final = "\u2014"
_CUSTOM_LABEL: Final = f" {_EM_DASH} custom"


@dataclass(frozen=True)
class CatalogVoice:
    """A single TTS voice (built-in or custom)."""

    voice_id: str
    label: str  # "Name — gender" | bare name
    gender: str = ""
    accent: str = ""
    custom: bool = False


@dataclass(frozen=True)
class CatalogModel:
    """A single TTS model and its supported voices."""

    model_id: str
    supports_speed_adjustment: bool = False
    speed_min: float = DEFAULT_SPEED_MIN
    speed_max: float = DEFAULT_SPEED_MAX
    voices: tuple[CatalogVoice, ...] = ()
    custom_voices: tuple[CatalogVoice, ...] = ()


@dataclass(frozen=True)
class SonioxCatalog:
    """The full TTS capability catalog keyed by model id."""

    models: dict[str, CatalogModel]


class CatalogFetchError(HomeAssistantError):
    """Raised when the live catalog cannot be fetched."""


def _is_number(value: Any) -> bool:
    """Return True for int/float values, treating bools as non-numeric."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _string_field(entry: dict[str, Any], key: str) -> str | None:
    """Return entry[key] when it is a non-empty string, else None."""
    value = entry.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _voice_label(name: str, gender: str) -> str:
    """Build a display label from the name and optional gender part."""
    if gender:
        return f"{name} {_EM_DASH} {gender}"
    return name


def _parse_voice(entry: Any) -> CatalogVoice | None:
    """Parse a single built-in voice entry, or None to skip it."""
    if not isinstance(entry, dict):
        return None
    voice_id = _string_field(entry, "id")
    if voice_id is None:
        return None
    gender = entry.get("gender", "")
    if not isinstance(gender, str):
        gender = ""
    accent = entry.get("description", "")
    if not isinstance(accent, str):
        accent = ""
    return CatalogVoice(
        voice_id=voice_id,
        label=_voice_label(voice_id, gender),
        gender=gender,
        accent=accent,
    )


def _parse_model(entry: Any) -> CatalogModel | None:
    """Parse a single model entry, or None to skip it."""
    if not isinstance(entry, dict):
        return None
    model_id = _string_field(entry, "id")
    if model_id is None:
        return None

    supports_speed = entry.get("supports_speed_adjustment", False)
    if not isinstance(supports_speed, bool):
        supports_speed = False

    speed_min = DEFAULT_SPEED_MIN
    speed_max = DEFAULT_SPEED_MAX
    if _is_number(entry.get("speed_min")):
        speed_min = float(entry["speed_min"])
    if _is_number(entry.get("speed_max")):
        speed_max = float(entry["speed_max"])
    if speed_min > speed_max:
        speed_min, speed_max = speed_max, speed_min

    voices: tuple[CatalogVoice, ...] = ()
    raw_voices = entry.get("voices")
    if isinstance(raw_voices, list):
        voices = tuple(
            v for v in (_parse_voice(v) for v in raw_voices) if v is not None
        )

    return CatalogModel(
        model_id=model_id,
        supports_speed_adjustment=supports_speed,
        speed_min=speed_min,
        speed_max=speed_max,
        voices=voices,
    )


def parse_catalog(models_payload: Any, voices_payload: Any) -> SonioxCatalog:
    """Parse the /tts-models and /voices JSON payloads into a SonioxCatalog.

    Never raises: malformed input degrades to sensible defaults.
    """
    models: dict[str, CatalogModel] = {}

    if isinstance(models_payload, dict):
        raw_models = models_payload.get("models")
        if isinstance(raw_models, list):
            for entry in raw_models:
                model = _parse_model(entry)
                if model is not None:
                    models[model.model_id] = model

    # Collect custom voices ready per model, keyed by model id.
    custom_by_model: dict[str, list[CatalogVoice]] = {}

    if isinstance(voices_payload, dict):
        raw_voices = voices_payload.get("voices")
        if isinstance(raw_voices, list):
            for entry in raw_voices:
                if not isinstance(entry, dict):
                    continue
                if (voice_id := _string_field(entry, "id")) is None:
                    continue
                name = entry.get("name", "")
                if not isinstance(name, str) or not name:
                    name = voice_id
                label = f"{name}{_CUSTOM_LABEL}"
                voice = CatalogVoice(
                    voice_id=voice_id, label=label, custom=True
                )
                raw_ready = entry.get("models")
                if not isinstance(raw_ready, list):
                    continue  # not ready for any model; kept out of all tuples
                seen: set[str] = set()
                for ready in raw_ready:
                    if not isinstance(ready, dict):
                        continue
                    model = ready.get("model")
                    status = ready.get("status")
                    if not isinstance(model, str) or not model:
                        continue
                    if status != "ready":
                        continue
                    if model in seen:
                        continue
                    seen.add(model)
                    custom_by_model.setdefault(model, []).append(voice)

    for model_id, custom_voices in custom_by_model.items():
        if model_id in models:
            model = models[model_id]
            models[model_id] = replace(
                model, custom_voices=tuple(custom_voices)
            )

    return SonioxCatalog(models=models)


async def async_fetch_catalog(
    session: aiohttp.ClientSession,
    endpoints: SonioxEndpoints,
    api_key: str,
    *,
    timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(total=30),
) -> SonioxCatalog:
    """Fetch the live TTS catalog (models + custom voices) from Soniox."""
    headers = {"Authorization": f"Bearer {api_key}"}

    async def _get_json(url: str) -> Any:
        try:
            resp = await session.get(url, headers=headers, timeout=timeout)
        except aiohttp.ClientError as err:
            raise CatalogFetchError(
                f"Soniox catalog request failed: {err}"
            ) from err
        if resp.status >= 400 or resp.status < 200:
            raise CatalogFetchError(
                f"Soniox catalog returned HTTP {resp.status}"
            )
        try:
            return await resp.json()
        except (ValueError, aiohttp.ClientError) as err:
            raise CatalogFetchError(
                "Soniox catalog returned invalid JSON"
            ) from err

    models_payload = await _get_json(endpoints.tts_models_url)
    if not isinstance(models_payload, dict):
        raise CatalogFetchError(
            "Soniox catalog models payload was not a JSON object"
        )
    voices_payload = await _get_json(endpoints.voices_url)
    if not isinstance(voices_payload, dict):
        raise CatalogFetchError(
            "Soniox catalog voices payload was not a JSON object"
        )
    return parse_catalog(models_payload, voices_payload)
