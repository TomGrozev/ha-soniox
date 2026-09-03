"""Config flow for the Soniox integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .catalog import CatalogFetchError, async_fetch_catalog
from .const import (
    CONF_API_KEY,
    CONF_REGION,
    CONF_STT_ASYNC_MODEL,
    CONF_STT_MODEL,
    CONF_TTS_AUDIO_FORMAT,
    CONF_TTS_LANGUAGE,
    CONF_TTS_MODEL,
    CONF_TTS_SAMPLE_RATE,
    CONF_TTS_VOICE,
    DEFAULT_REGION,
    DEFAULT_STT_ASYNC_MODEL,
    DEFAULT_STT_MODEL,
    DEFAULT_TTS_AUDIO_FORMAT,
    DEFAULT_TTS_LANGUAGE,
    DEFAULT_TTS_MODEL,
    DEFAULT_TTS_SAMPLE_RATE,
    DEFAULT_TTS_VOICE,
    DOMAIN,
    REGION_EU,
    REGION_JP,
    REGION_LABELS,
    REGION_US,
    STT_ASYNC_MODELS,
    STT_REALTIME_MODELS,
    SUPPORTED_LANGUAGES,
    TTS_MODELS,
    endpoints_for_region,
)

_LOGGER = logging.getLogger(__name__)

TTS_AUDIO_FORMATS = ["mp3", "wav", "pcm_s16le"]
TTS_SAMPLE_RATES = [8000, 16000, 24000, 44100, 48000]


def _model_options(
    choices: list[str], current: str | None
) -> list[SelectOptionDict]:
    """Keep a user-saved custom model visible in the dropdown."""
    values = list(choices)
    if current and current not in values:
        values.append(current)
    return [SelectOptionDict(value=value, label=value) for value in values]


def _region_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(
                    value=REGION_US, label="United States — api.soniox.com"
                ),
                SelectOptionDict(
                    value=REGION_EU, label="European Union — api.eu.soniox.com"
                ),
                SelectOptionDict(
                    value=REGION_JP, label="Japan — api.jp.soniox.com"
                ),
            ],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _credentials_schema(
    *, api_key: str | None = None, region: str = DEFAULT_REGION
) -> vol.Schema:
    api_key_field = (
        vol.Required(CONF_API_KEY, default=api_key)
        if api_key
        else vol.Required(CONF_API_KEY)
    )
    return vol.Schema(
        {
            api_key_field: str,
            vol.Required(CONF_REGION, default=region): _region_selector(),
        }
    )


def _entry_title(region: str) -> str:
    label = REGION_LABELS.get(region, REGION_LABELS[DEFAULT_REGION])
    return f"Soniox ({label})"


async def _validate_api_key(
    session: aiohttp.ClientSession, api_key: str, region: str
) -> str | None:
    """Return an error key, or None if the key works on the chosen region."""
    endpoints = endpoints_for_region(region)
    try:
        async with session.get(
            endpoints.tts_models_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 401:
                return "invalid_auth"
            if resp.status >= 400:
                return "cannot_connect"
    except aiohttp.ClientError:
        return "cannot_connect"
    except TimeoutError:
        return "cannot_connect"
    return None


class SonioxConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Soniox."""

    VERSION = 3

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prompt for the API key and regional endpoint."""
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            error = await _validate_api_key(
                session, user_input[CONF_API_KEY], user_input[CONF_REGION]
            )
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(
                    f"{user_input[CONF_REGION]}:{user_input[CONF_API_KEY][-8:]}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=_entry_title(user_input[CONF_REGION]), data=user_input
                )

        defaults = user_input or {}
        return self.async_show_form(
            step_id="user",
            data_schema=_credentials_schema(
                api_key=defaults.get(CONF_API_KEY),
                region=defaults.get(CONF_REGION, DEFAULT_REGION),
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Allow updating the API key and regional endpoint."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            error = await _validate_api_key(
                session, user_input[CONF_API_KEY], user_input[CONF_REGION]
            )
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data={**entry.data, **user_input},
                    title=_entry_title(user_input[CONF_REGION]),
                    unique_id=(
                        f"{user_input[CONF_REGION]}:{user_input[CONF_API_KEY][-8:]}"
                    ),
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_credentials_schema(
                region=entry.data.get(CONF_REGION, DEFAULT_REGION)
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return SonioxOptionsFlow()


class SonioxOptionsFlow(OptionsFlow):
    """Per-engine defaults that the user can change without re-adding the entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show / save the options form."""
        if user_input is not None:
            try:
                user_input[CONF_TTS_SAMPLE_RATE] = int(
                    user_input[CONF_TTS_SAMPLE_RATE]
                )
            except (KeyError, TypeError, ValueError):
                user_input[CONF_TTS_SAMPLE_RATE] = DEFAULT_TTS_SAMPLE_RATE
            return self.async_create_entry(title="", data=user_input)

        opts = self.config_entry.options
        voice_options: list[SelectOptionDict] = []
        errors: dict[str, str] = {}

        try:
            catalog = await async_fetch_catalog(
                async_get_clientsession(self.hass),
                endpoints_for_region(
                    self.config_entry.data.get(CONF_REGION, DEFAULT_REGION)
                ),
                self.config_entry.data[CONF_API_KEY],
            )
        except CatalogFetchError:
            errors["base"] = "cannot_connect"
        else:
            model = catalog.models.get(
                opts.get(CONF_TTS_MODEL, DEFAULT_TTS_MODEL)
            )
            if model is not None:
                voice_options = [
                    SelectOptionDict(value=voice.voice_id, label=voice.label)
                    for voice in (*model.voices, *model.custom_voices)
                ]

        # Guarantee the default (or saved) voice is a valid choice so the form
        # stays savable even when the catalog is unavailable / lists no voices.
        fallback_voice = opts.get(CONF_TTS_VOICE, DEFAULT_TTS_VOICE)
        if fallback_voice not in {
            option["value"] for option in voice_options
        }:
            voice_options.append(
                SelectOptionDict(value=fallback_voice, label=fallback_voice)
            )

        return self.async_show_form(
            step_id="init",
            data_schema=self._options_schema(opts, voice_options),
            errors=errors,
        )

    def _options_schema(
        self,
        opts: Mapping[str, Any],
        voice_options: list[SelectOptionDict],
    ) -> vol.Schema:
        """Build the options form schema."""
        lang_options = [
            SelectOptionDict(value=code, label=code) for code in SUPPORTED_LANGUAGES
        ]
        format_options = [
            SelectOptionDict(value=f, label=f) for f in TTS_AUDIO_FORMATS
        ]
        sample_rate_options = [
            SelectOptionDict(value=str(r), label=str(r)) for r in TTS_SAMPLE_RATES
        ]
        stt_model_options = _model_options(
            STT_REALTIME_MODELS, opts.get(CONF_STT_MODEL)
        )
        stt_async_model_options = _model_options(
            STT_ASYNC_MODELS, opts.get(CONF_STT_ASYNC_MODEL)
        )
        tts_model_options = _model_options(TTS_MODELS, opts.get(CONF_TTS_MODEL))

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_STT_MODEL,
                    default=opts.get(CONF_STT_MODEL, DEFAULT_STT_MODEL),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=stt_model_options, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Optional(
                    CONF_STT_ASYNC_MODEL,
                    default=opts.get(CONF_STT_ASYNC_MODEL, DEFAULT_STT_ASYNC_MODEL),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=stt_async_model_options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_TTS_MODEL,
                    default=opts.get(CONF_TTS_MODEL, DEFAULT_TTS_MODEL),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=tts_model_options, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Optional(
                    CONF_TTS_VOICE,
                    default=opts.get(CONF_TTS_VOICE, DEFAULT_TTS_VOICE),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=voice_options, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Optional(
                    CONF_TTS_LANGUAGE,
                    default=opts.get(CONF_TTS_LANGUAGE, DEFAULT_TTS_LANGUAGE),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=lang_options, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Optional(
                    CONF_TTS_AUDIO_FORMAT,
                    default=opts.get(CONF_TTS_AUDIO_FORMAT, DEFAULT_TTS_AUDIO_FORMAT),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=format_options, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Optional(
                    CONF_TTS_SAMPLE_RATE,
                    default=str(
                        opts.get(CONF_TTS_SAMPLE_RATE, DEFAULT_TTS_SAMPLE_RATE)
                    ),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=sample_rate_options, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
            }
        )

        return schema
