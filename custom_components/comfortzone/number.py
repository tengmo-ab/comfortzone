"""Number entities for Comfortzone Heat Pump integration."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
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
from .const import CLEAR_TEXT_NAMES, DELAY_REFRESH_AFTER_SET, DOMAIN
from .entity import build_device_info, device_unique_id

_LOGGER = logging.getLogger(__name__)

NUMBER_ENTITIES_CONFIG = [
    {
        "entity_suffix": "hot_water_temp_setpoint",
        "property_set": "SetHotWaterTemp",
        "property_read": CLEAR_TEXT_NAMES["TARGET_HW_TEMP"],
        "name": "Hot water target temperature",
        "icon": "mdi:water-boiler",
        "unit": UnitOfTemperature.CELSIUS,
        "min": 30.0,
        "max": 65.0,
        "step": 1.0,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.CONFIG,
    },
    {
        "entity_suffix": "holiday_reduction_days",
        "property_set": "SetHolidayReductionDays",
        "property_read": CLEAR_TEXT_NAMES["HOLIDAY_DAYS"],
        "name": "Holiday reduction days",
        "icon": "mdi:calendar-arrow-right",
        "unit": UnitOfTime.DAYS,
        "min": 0,
        "max": 9,
        "step": 1,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.CONFIG,
    },
    {
        "entity_suffix": "heat_curve",
        "property_set": "SetHeatCurve",
        "property_read": CLEAR_TEXT_NAMES["HEATING_CURVE"],
        "name": "Heat curve",
        "icon": "mdi:chart-line",
        "unit": None,
        "min": 0.0,
        "max": 6.0,
        "step": 0.1,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Comfortzone number entities."""
    if DOMAIN not in hass.data or entry.entry_id not in hass.data[DOMAIN]:
        _LOGGER.error("Comfortzone data missing for entry %s", entry.entry_id)
        return
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DataUpdateCoordinator = data.get("coordinator")
    api_client: ComfortzoneApiClient = data.get("client")
    if not coordinator or not api_client:
        _LOGGER.error("Coordinator or API client missing for %s", entry.entry_id)
        return

    entities = [
        ComfortzoneNumberEntity(coordinator, api_client, entry, config)
        for config in NUMBER_ENTITIES_CONFIG
    ]
    async_add_entities(entities)


class ComfortzoneNumberEntity(CoordinatorEntity, NumberEntity):
    """Representation of a Comfortzone Number entity."""

    _attr_has_entity_name = True
    _attr_native_value: float | None = None

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        api_client: ComfortzoneApiClient,
        entry: ConfigEntry,
        config: dict,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._client = api_client
        self._config = config
        self.entry = entry
        suffix = config["entity_suffix"]
        self._attr_unique_id = f"{device_unique_id(entry)}_{suffix}"
        self._attr_name = config["name"]
        self._attr_icon = config.get("icon")
        self._attr_native_unit_of_measurement = config.get("unit")
        self._attr_native_min_value = config["min"]
        self._attr_native_max_value = config["max"]
        self._attr_native_step = config["step"]
        self._attr_mode = config["mode"]
        if "entity_category" in config:
            self._attr_entity_category = config["entity_category"]
        self._attr_device_info = build_device_info(entry)
        self._attr_native_value = None
        self._attr_available = self.coordinator.last_update_success
        if self.coordinator.data:
            self._update_state_from_coordinator()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self._config.get("property_read") is None:
            return
        current_value = self._attr_native_value
        current_availability = self._attr_available
        self._update_state_from_coordinator()
        if (
            self._attr_native_value != current_value
            or self._attr_available != current_availability
        ):
            if self.hass:
                self.async_write_ha_state()

    def _update_state_from_coordinator(self) -> None:
        """Update the entity state from coordinator data."""
        prop_read = self._config.get("property_read")
        if prop_read is None:
            return

        new_value = self._attr_native_value
        new_availability = True

        data_block = (self.coordinator.data or {}).get("Data") or {}
        values_list = data_block.get("Values")
        if not self.coordinator.last_update_success or not isinstance(values_list, list):
            new_availability = False
        else:
            value_str = find_value_from_raw_data(values_list, prop_read, "ClearTextName")
            if value_str is None:
                new_availability = False
            else:
                try:
                    if self.native_unit_of_measurement == UnitOfTime.DAYS:
                        value_num = int(value_str)
                    else:
                        value_num = float(value_str)
                    new_value = max(self.native_min_value, min(self.native_max_value, value_num))
                except (TypeError, ValueError):
                    _LOGGER.warning(
                        "Could not parse number value for %s: '%s'", self.name, value_str
                    )
                    new_availability = False

        self._attr_available = new_availability
        self._attr_native_value = new_value if new_availability else None

    async def _delayed_refresh(self, _now) -> None:
        """Request coordinator refresh after a delay."""
        if self.coordinator and self.hass:
            await self.coordinator.async_request_refresh()

    async def async_set_native_value(self, value: float) -> None:
        """Send the new value to the API."""
        prop_set = self._config["property_set"]
        try:
            clamped = max(self.native_min_value, min(self.native_max_value, value))
            value_to_send = int(clamped) if self.native_step == 1.0 else clamped

            success = await self._client.async_set_property(prop_set, value_to_send)
            if success:
                self._attr_native_value = clamped
                self.async_write_ha_state()
                async_call_later(self.hass, DELAY_REFRESH_AFTER_SET, self._delayed_refresh)
            else:
                _LOGGER.error("Failed to set %s via API", self.name)
        except (ComfortzoneApiCommandError, ComfortzoneApiClientError) as err:
            _LOGGER.error("API error setting %s: %s", self.name, err)
        except ValueError:
            _LOGGER.error("Invalid value %s for %s", value, self.name)
