"""Climate platform for Comfortzone Heat Pump."""
from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .api import (
    ComfortzoneApiClient,
    ComfortzoneApiClientError,
    ComfortzoneApiCommandError,
    find_value_from_raw_data,
)
from .const import CLEAR_TEXT_NAMES, DELAY_REFRESH_AFTER_SET, DOMAIN, TEMP_VALUE_FOR_OFF
from .entity import build_device_info, device_unique_id

_LOGGER = logging.getLogger(__name__)

DEFAULT_HEAT_TARGET = 21.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Comfortzone climate entity."""
    if DOMAIN not in hass.data or entry.entry_id not in hass.data[DOMAIN]:
        _LOGGER.error("Comfortzone data missing for entry %s", entry.entry_id)
        return
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DataUpdateCoordinator = data.get("coordinator")
    api_client: ComfortzoneApiClient = data.get("client")
    if not coordinator or not api_client:
        _LOGGER.error("Coordinator or API client missing for %s", entry.entry_id)
        return
    async_add_entities([ComfortzoneRX95ClimateEntity(coordinator, entry, api_client)])


class ComfortzoneRX95ClimateEntity(CoordinatorEntity, ClimateEntity):
    """Representation of a Comfortzone Heat Pump."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_min_temp = 10.0
    _attr_max_temp = 25.0
    _attr_target_temperature_step = 0.5

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
        api_client: ComfortzoneApiClient,
    ) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator)
        self.entry = entry
        self._client = api_client
        self._attr_unique_id = f"{device_unique_id(entry)}_climate"
        self._attr_device_info = build_device_info(entry)
        self._attr_hvac_mode = HVACMode.HEAT
        self._attr_current_temperature = None
        self._attr_target_temperature = None
        self._attr_hvac_action = None
        self._attr_extra_state_attributes = {}
        self._last_heat_target: Optional[float] = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        new_extra_attrs: dict[str, Any] = {}
        new_mode: Optional[HVACMode] = self._attr_hvac_mode
        new_action: Optional[HVACAction] = self._attr_hvac_action
        new_target: Optional[float] = self._attr_target_temperature
        new_temp: Optional[float] = self._attr_current_temperature
        new_availability = True

        data_block = (self.coordinator.data or {}).get("Data") or {}
        values_list = data_block.get("Values")

        if (
            not self.coordinator.last_update_success
            or not isinstance(values_list, list)
        ):
            new_availability = False
        else:
            try:
                indoor_temp_str = find_value_from_raw_data(
                    values_list, CLEAR_TEXT_NAMES["INDOOR_TEMP"]
                )
                target_temp_str = find_value_from_raw_data(
                    values_list, CLEAR_TEXT_NAMES["TARGET_INDOOR_TEMP"]
                )
                compressor_active_str = find_value_from_raw_data(
                    values_list, CLEAR_TEXT_NAMES["COMPRESSOR_ACTIVE"]
                )
                heating_valve_str = find_value_from_raw_data(
                    values_list, CLEAR_TEXT_NAMES["EXCHANGE_VALVE_HEATING"]
                )
                hw_valve_str = find_value_from_raw_data(
                    values_list, CLEAR_TEXT_NAMES["EXCHANGE_VALVE_HW"]
                )

                try:
                    new_temp = float(indoor_temp_str) if indoor_temp_str is not None else None
                    if new_temp is None:
                        new_availability = False
                except (ValueError, TypeError):
                    _LOGGER.warning("Could not parse IndoorTemperature: '%s'", indoor_temp_str)
                    new_availability = False

                target_temp_api: Optional[float] = None
                try:
                    if target_temp_str is not None:
                        target_temp_api = float(target_temp_str)
                        new_target = max(self.min_temp, min(self.max_temp, target_temp_api))
                    else:
                        new_availability = False
                except (ValueError, TypeError):
                    _LOGGER.warning("Could not parse SetIndoorTemp: '%s'", target_temp_str)
                    new_availability = False

                if new_availability and target_temp_api is not None:
                    if target_temp_api <= TEMP_VALUE_FOR_OFF:
                        new_mode = HVACMode.OFF
                    else:
                        new_mode = HVACMode.HEAT
                        self._last_heat_target = target_temp_api

                    if new_mode == HVACMode.OFF:
                        new_action = HVACAction.OFF
                    elif compressor_active_str == "1":
                        if heating_valve_str == "1":
                            new_action = HVACAction.HEATING
                        elif hw_valve_str == "1":
                            new_action = HVACAction.IDLE
                        else:
                            new_action = HVACAction.IDLE
                    else:
                        new_action = HVACAction.IDLE
                else:
                    new_mode = None
                    new_action = None

                new_extra_attrs["raw_compressor_active"] = compressor_active_str
                new_extra_attrs["raw_heating_valve"] = heating_valve_str
                new_extra_attrs["raw_hw_valve"] = hw_valve_str
                new_extra_attrs["outdoor_temp"] = find_value_from_raw_data(
                    values_list, CLEAR_TEXT_NAMES["OUTDOOR_TEMP"]
                )
                new_extra_attrs["hot_water_temp"] = find_value_from_raw_data(
                    values_list, CLEAR_TEXT_NAMES["HOT_WATER_TEMP"]
                )
                new_extra_attrs["last_api_update"] = data_block.get("LogDateTimeUtc")

            except Exception as err:
                _LOGGER.exception("Unexpected error processing climate update: %s", err)
                new_availability = False

        if new_availability:
            self._attr_hvac_mode = new_mode
            self._attr_hvac_action = new_action
            self._attr_target_temperature = new_target
            self._attr_current_temperature = new_temp
            self._attr_extra_state_attributes = new_extra_attrs
            self._attr_available = True
        else:
            self._attr_hvac_mode = None
            self._attr_hvac_action = None
            self._attr_target_temperature = None
            self._attr_current_temperature = None
            self._attr_extra_state_attributes = {}
            self._attr_available = False

        if self.hass:
            self.async_write_ha_state()

    async def _delayed_refresh(self, _now) -> None:
        """Request coordinator refresh after a delay."""
        if self.coordinator and self.hass:
            await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Enable (HEAT) or disable (OFF) heating by writing target temperature."""
        if hvac_mode not in self._attr_hvac_modes:
            _LOGGER.warning("Unsupported HVAC mode: %s", hvac_mode)
            return

        property_name = "SetIndoorTemp"
        if hvac_mode == HVACMode.OFF:
            target_value: float = TEMP_VALUE_FOR_OFF
        else:
            current = self._attr_target_temperature
            if current is not None and current > TEMP_VALUE_FOR_OFF:
                target_value = current
            elif self._last_heat_target is not None:
                target_value = self._last_heat_target
            else:
                target_value = DEFAULT_HEAT_TARGET
            _LOGGER.info(
                "Enabling HEAT mode by writing %s = %.1f",
                property_name,
                target_value,
            )

        try:
            success = await self._client.async_set_property(property_name, target_value)
            if success:
                self._attr_hvac_mode = hvac_mode
                self._attr_target_temperature = target_value
                self.async_write_ha_state()
                async_call_later(self.hass, DELAY_REFRESH_AFTER_SET, self._delayed_refresh)
            else:
                _LOGGER.error("Failed to set HVAC mode to %s via API", hvac_mode)
        except (ComfortzoneApiCommandError, ComfortzoneApiClientError) as err:
            _LOGGER.error("API error setting HVAC mode to %s: %s", hvac_mode, err)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        try:
            target_temp = float(temperature)
        except (TypeError, ValueError):
            _LOGGER.error("Invalid temperature value provided: %s", temperature)
            return

        clamped_temp = max(self.min_temp, min(self.max_temp, target_temp))
        new_mode = HVACMode.OFF if clamped_temp <= TEMP_VALUE_FOR_OFF else HVACMode.HEAT
        try:
            success = await self._client.async_set_property("SetIndoorTemp", clamped_temp)
            if success:
                self._attr_target_temperature = clamped_temp
                self._attr_hvac_mode = new_mode
                if new_mode == HVACMode.HEAT:
                    self._last_heat_target = clamped_temp
                self.async_write_ha_state()
                async_call_later(self.hass, DELAY_REFRESH_AFTER_SET, self._delayed_refresh)
            else:
                _LOGGER.error("Failed to set target temperature via API")
        except (ComfortzoneApiCommandError, ComfortzoneApiClientError) as err:
            _LOGGER.error("API error setting target temperature: %s", err)
