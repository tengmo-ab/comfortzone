"""Config flow for Comfortzone Heat Pump."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_API_KEY
from homeassistant.core import callback
from homeassistant.helpers import selector
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


def _price_entity_selector():
    """Return an EntitySelector restricted to sensors with a numeric state."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")
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
                # Pull optional cost-related fields out of user_input and
                # store them as options rather than core data, so that
                # they can be edited later via the options flow without
                # rewriting auth credentials.
                core_data = {
                    CONF_API_KEY: user_input[CONF_API_KEY],
                    CONF_DEVICE_ID: user_input[CONF_DEVICE_ID],
                    CONF_MODEL: user_input[CONF_MODEL],
                }
                option_data: dict[str, Any] = {}
                if user_input.get(CONF_PRICE_ENTITY):
                    option_data[CONF_PRICE_ENTITY] = user_input[CONF_PRICE_ENTITY]
                if CONF_PRICE_IN_ORE in user_input:
                    option_data[CONF_PRICE_IN_ORE] = user_input[CONF_PRICE_IN_ORE]
                return self.async_create_entry(
                    title=f"Comfortzone Heat Pump ({device_id_str})",
                    data=core_data,
                    options=option_data,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_API_KEY): str,
                vol.Required(CONF_DEVICE_ID): int,
                vol.Required(CONF_MODEL, default="RX95"): vol.In(MODELS),
                vol.Optional(CONF_PRICE_ENTITY): _price_entity_selector(),
                vol.Optional(CONF_PRICE_IN_ORE, default=False): bool,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            new_data = {**self.config_entry.data}
            if CONF_MODEL in user_input:
                new_data[CONF_MODEL] = user_input[CONF_MODEL]

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
        # The previous user step may have stashed the price entity in data;
        # support both locations as the source of the current value.
        current_price_entity = opts.get(CONF_PRICE_ENTITY) or self.config_entry.data.get(
            CONF_PRICE_ENTITY, ""
        )
        current_price_in_ore = opts.get(
            CONF_PRICE_IN_ORE, self.config_entry.data.get(CONF_PRICE_IN_ORE, False)
        )

        schema_dict: dict[Any, Any] = {
            vol.Required(CONF_MODEL, default=current_model): vol.In(MODELS),
        }
        if current_price_entity:
            schema_dict[
                vol.Optional(CONF_PRICE_ENTITY, default=current_price_entity)
            ] = _price_entity_selector()
        else:
            schema_dict[vol.Optional(CONF_PRICE_ENTITY)] = _price_entity_selector()
        schema_dict[
            vol.Optional(CONF_PRICE_IN_ORE, default=current_price_in_ore)
        ] = bool
        schema_dict[
            vol.Optional(
                CONF_COMPRESSOR_ELECTRICAL_FACTOR,
                default=opts.get(
                    CONF_COMPRESSOR_ELECTRICAL_FACTOR, DEFAULT_COMPRESSOR_FACTOR
                ),
            )
        ] = vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0))

        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema_dict))
