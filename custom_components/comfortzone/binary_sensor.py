"""Binary sensor entities for Comfortzone Heat Pump integration."""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .api import find_value_from_raw_data
from .const import BINARY_SENSOR_MAP, CLEAR_TEXT_NAMES, DOMAIN
from .entity import build_device_info, device_unique_id

_LOGGER = logging.getLogger(__name__)

BINARY_SENSOR_TYPES: dict[str, dict] = {
    "filter_alarm": {
        "name": "Filter alarm active",
        "device_class": BinarySensorDeviceClass.PROBLEM,
        "on_value": "1",
    },
    "main_alarm": {
        "name": "Alarm active",
        "device_class": BinarySensorDeviceClass.PROBLEM,
        "on_value": None,
    },
    "compressor_active": {
        "name": "Compressor active",
        "device_class": BinarySensorDeviceClass.RUNNING,
        "on_value": "1",
    },
    "room_thermostat": {
        "name": "Room thermostat active",
        "device_class": BinarySensorDeviceClass.CONNECTIVITY,
        "on_value": "1",
        "options": {
            "entity_registry_enabled_default": False,
            "entity_category": EntityCategory.DIAGNOSTIC,
        },
    },
    "heating_valve": {
        "name": "Heating valve active",
        "device_class": BinarySensorDeviceClass.HEAT,
        "on_value": "1",
        "options": {
            "entity_registry_enabled_default": False,
            "entity_category": EntityCategory.DIAGNOSTIC,
        },
    },
    "hot_water_valve": {
        "name": "Hot water valve active",
        "device_class": None,
        "icon_on": "mdi:valve-open",
        "icon_off": "mdi:valve-closed",
        "on_value": "1",
        "options": {
            "entity_registry_enabled_default": False,
            "entity_category": EntityCategory.DIAGNOSTIC,
        },
    },
    "cooling_installed": {
        "name": "Cooling installed",
        "device_class": None,
        "icon_on": "mdi:snowflake",
        "icon_off": "mdi:snowflake-off",
        "on_value": "1",
        "options": {"entity_category": EntityCategory.DIAGNOSTIC},
    },
    "cooling_enabled": {
        "name": "Cooling enabled",
        "device_class": None,
        "icon_on": "mdi:power",
        "icon_off": "mdi:power-sleep",
        "on_value": "1",
    },
    "dual_heating_curves": {
        "name": "Dual heating curves",
        "device_class": None,
        "icon_on": "mdi:chart-bell-curve",
        "icon_off": "mdi:chart-line",
        "on_value": "1",
        "options": {"entity_category": EntityCategory.DIAGNOSTIC},
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Comfortzone binary sensor entities."""
    if DOMAIN not in hass.data or entry.entry_id not in hass.data[DOMAIN]:
        _LOGGER.error("Comfortzone data missing for entry %s", entry.entry_id)
        return
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DataUpdateCoordinator = data.get("coordinator")
    if not coordinator:
        _LOGGER.error("Coordinator missing for %s", entry.entry_id)
        return

    entities = []
    for suffix, config_details in BINARY_SENSOR_TYPES.items():
        if suffix not in BINARY_SENSOR_MAP:
            _LOGGER.error("Binary sensor suffix '%s' not in BINARY_SENSOR_MAP", suffix)
            continue
        config = {
            **config_details,
            "property_read": BINARY_SENSOR_MAP[suffix],
            "entity_suffix": suffix,
        }
        entities.append(ComfortzoneBinarySensorEntity(coordinator, entry, suffix, config))

    async_add_entities(entities)


class ComfortzoneBinarySensorEntity(CoordinatorEntity, BinarySensorEntity):
    """Representation of a Comfortzone Binary Sensor entity."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default: bool = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
        entity_suffix: str,
        config: dict,
    ) -> None:
        """Initialize the binary sensor entity."""
        super().__init__(coordinator)
        self._config = config
        self.entry = entry
        self._entity_suffix = entity_suffix
        self._attr_unique_id = f"{device_unique_id(entry)}_{entity_suffix}"
        self._attr_name = config["name"]
        self._attr_device_class = config.get("device_class")
        opts = config.get("options", {})
        if "entity_registry_enabled_default" in opts:
            self._attr_entity_registry_enabled_default = opts["entity_registry_enabled_default"]
        if "entity_category" in opts:
            self._attr_entity_category = opts["entity_category"]
        self._attr_device_info = build_device_info(entry)
        self._property_read = config["property_read"]
        self._on_value = config.get("on_value")
        self._attr_is_on = None
        self._attr_available = self.coordinator.last_update_success
        if self.coordinator.data:
            self._update_state_from_coordinator()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        current_availability = self._attr_available
        current_is_on = self._attr_is_on
        self._update_state_from_coordinator()
        if (
            self._attr_is_on != current_is_on
            or self._attr_available != current_availability
        ):
            if self.hass:
                self.async_write_ha_state()

    def _update_state_from_coordinator(self) -> None:
        """Update the entity state from coordinator data."""
        new_state = self._attr_is_on
        new_availability = True

        data_block = (self.coordinator.data or {}).get("Data") or {}
        values_list = data_block.get("Values")
        if not self.coordinator.last_update_success or not isinstance(values_list, list):
            new_availability = False
        else:
            value_str = find_value_from_raw_data(values_list, self._property_read, "ClearTextName")
            if value_str is None:
                new_availability = False
            elif self._property_read == CLEAR_TEXT_NAMES["ALARM_TEXT"]:
                new_state = value_str != ""
            elif self._on_value is not None:
                new_state = value_str == self._on_value
            else:
                new_state = False

        self._attr_available = new_availability
        self._attr_is_on = new_state if new_availability else None
