"""Config flow for Comfortzone Heat Pump."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_API_KEY
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    ComfortzoneApiAuthError,
    ComfortzoneApiClient,
    ComfortzoneApiClientError,
    ComfortzoneApiCommunicationError,
)
from .const import (
    CONF_COMPRESSOR_ELECTRICAL_FACTOR,
    CONF_DEVICE_ID,
    CONF_MODEL,
    CONF_PRICE_ENTITY,
    CONF_PRICE_IN_ORE,
    DEFAULT_COMPRESSOR_FACTOR,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

MODELS = ["RX95", "Other"]

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): str,
        vol.Required(CONF_DEVICE_ID): int,
        vol.Required(CONF_MODEL, default="RX95"): vol.In(MODELS),
    }
)


class ComfortzoneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Comfortzone Heat Pump."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return OptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            device_id_str = str(user_input[CONF_DEVICE_ID])
            await self.async_set_unique_id(device_id_str)
            self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            api_client = ComfortzoneApiClient(
                api_key=user_input[CONF_API_KEY],
                device_id=user_input[CONF_DEVICE_ID],
                session=session,
            )

            try:
                await api_client.async_get_data()
            except ComfortzoneApiAuthError:
                errors["base"] = "invalid_auth"
            except ComfortzoneApiCommunicationError:
                errors["base"] = "cannot_connect"
            except ComfortzoneApiClientError:
                errors["base"] = "unknown"
            except Exception:
                _LOGGER.exception("Unexpected error during validation")
                errors["base"] = "unknown"

            if not errors:
                return self.async_create_entry(
                    title=f"Comfortzone Heat Pump ({device_id_str})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            # Persist the model on the entry data (used by device_info / branding)
            new_data = {**self.config_entry.data}
            if CONF_MODEL in user_input:
                new_data[CONF_MODEL] = user_input[CONF_MODEL]

            # Strip empty strings so unset fields are stored as missing
            options = {
                key: value
                for key, value in user_input.items()
                if key != CONF_MODEL and value not in ("", None)
            }

            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data=new_data,
                options=options,
            )
            return self.async_create_entry(title="", data={})

        current_model = self.config_entry.data.get(CONF_MODEL, "RX95")
        opts = self.config_entry.options
        options_schema = vol.Schema(
            {
                vol.Required(CONF_MODEL, default=current_model): vol.In(MODELS),
                vol.Optional(
                    CONF_PRICE_ENTITY,
                    default=opts.get(CONF_PRICE_ENTITY, ""),
                ): str,
                vol.Optional(
                    CONF_PRICE_IN_ORE,
                    default=opts.get(CONF_PRICE_IN_ORE, True),
                ): bool,
                vol.Optional(
                    CONF_COMPRESSOR_ELECTRICAL_FACTOR,
                    default=opts.get(
                        CONF_COMPRESSOR_ELECTRICAL_FACTOR, DEFAULT_COMPRESSOR_FACTOR
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=1.0)),
            }
        )

        return self.async_show_form(step_id="init", data_schema=options_schema)
