"""Sensor entities for Comfortzone Heat Pump integration."""
from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import find_value_from_raw_data
from .const import CLEAR_TEXT_NAMES, DOMAIN
from .entity import build_device_info, device_unique_id

_LOGGER = logging.getLogger(__name__)

SENSOR_CONFIG: dict[str, dict[str, Any]] = {
    "indoor_temp": {
        "property_read": CLEAR_TEXT_NAMES["INDOOR_TEMP"],
        "name": "Indoor temperature",
        "device_class": SensorDeviceClass.TEMPERATURE,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfTemperature.CELSIUS,
        "icon": "mdi:home-thermometer-outline",
    },
    "set_indoor_temp": {
        "property_read": CLEAR_TEXT_NAMES["TARGET_INDOOR_TEMP"],
        "name": "Target indoor temperature",
        "device_class": SensorDeviceClass.TEMPERATURE,
        "state_class": None,
        "unit": UnitOfTemperature.CELSIUS,
        "icon": "mdi:thermostat",
        "options": {"entity_category": EntityCategory.DIAGNOSTIC},
    },
    "hot_water_temp": {
        "property_read": CLEAR_TEXT_NAMES["HOT_WATER_TEMP"],
        "name": "Hot water temperature",
        "device_class": SensorDeviceClass.TEMPERATURE,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfTemperature.CELSIUS,
        "icon": "mdi:water-thermometer",
    },
    "target_hw_temp": {
        "property_read": CLEAR_TEXT_NAMES["TARGET_HW_TEMP"],
        "name": "Target hot water temp readback",
        "device_class": SensorDeviceClass.TEMPERATURE,
        "state_class": None,
        "unit": UnitOfTemperature.CELSIUS,
        "icon": "mdi:target",
        "options": {
            "entity_registry_enabled_default": False,
            "entity_category": EntityCategory.DIAGNOSTIC,
        },
    },
    "outdoor_temp": {
        "property_read": CLEAR_TEXT_NAMES["OUTDOOR_TEMP"],
        "name": "Outdoor temperature",
        "device_class": SensorDeviceClass.TEMPERATURE,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfTemperature.CELSIUS,
        "icon": "mdi:thermometer",
    },
    "heat_carrier_in": {
        "property_read": CLEAR_TEXT_NAMES["FLOW_TEMP"],
        "name": "Heat carrier in temperature",
        "device_class": SensorDeviceClass.TEMPERATURE,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfTemperature.CELSIUS,
        "icon": "mdi:coolant-temperature",
    },
    "heat_carrier_out": {
        "property_read": CLEAR_TEXT_NAMES["RETURN_TEMP"],
        "name": "Heat carrier out temperature",
        "device_class": SensorDeviceClass.TEMPERATURE,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfTemperature.CELSIUS,
        "icon": "mdi:coolant-temperature",
    },
    "heating_curve_readback": {
        "property_read": CLEAR_TEXT_NAMES["HEATING_CURVE"],
        "name": "Heating curve readback",
        "device_class": None,
        "state_class": None,
        "unit": None,
        "icon": "mdi:chart-line",
        "options": {
            "entity_registry_enabled_default": False,
            "entity_category": EntityCategory.DIAGNOSTIC,
        },
    },
    "alarm_text": {
        "property_read": CLEAR_TEXT_NAMES["ALARM_TEXT"],
        "name": "Alarm text",
        "device_class": None,
        "state_class": None,
        "unit": None,
        "icon": "mdi:alert-outline",
        "options": {
            "availability_property_key": CLEAR_TEXT_NAMES["ALARM_TEXT"],
            "availability_logic": "not_empty",
            "entity_category": EntityCategory.DIAGNOSTIC,
        },
    },
    "time_to_filter_change": {
        "property_read": "Time to filter change",
        "name": "Time to filter change",
        "device_class": None,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfTime.DAYS,
        "icon": "mdi:filter-clock",
    },
    "circulation_pump_speed": {
        "property_read": CLEAR_TEXT_NAMES["CIRC_PUMP_SPEED"],
        "name": "Circulation pump speed",
        "device_class": None,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": PERCENTAGE,
        "icon": "mdi:pump",
    },
    "fan_speed_current": {
        "property_read": CLEAR_TEXT_NAMES["FAN_SPEED_CURRENT"],
        "name": "Current fan speed",
        "device_class": None,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": PERCENTAGE,
        "icon": "mdi:fan",
    },
    "fan_state_raw": {
        "property_read": CLEAR_TEXT_NAMES["FAN_STATE"],
        "name": "Fan state raw",
        "device_class": None,
        "state_class": None,
        "unit": None,
        "icon": "mdi:fan-alert",
        "options": {
            "entity_registry_enabled_default": False,
            "entity_category": EntityCategory.DIAGNOSTIC,
        },
    },
    "last_log_time": {
        "property_read": "LogDateTimeUtc",
        "name": "Last log time",
        "device_class": SensorDeviceClass.TIMESTAMP,
        "state_class": None,
        "unit": None,
        "icon": "mdi:clock-outline",
        "options": {
            "entity_registry_enabled_default": False,
            "entity_category": EntityCategory.DIAGNOSTIC,
        },
    },
    "exhaust_air_temp": {
        "property_read": CLEAR_TEXT_NAMES["EXHAUST_AIR_TEMP"],
        "name": "Exhaust air temperature",
        "device_class": SensorDeviceClass.TEMPERATURE,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfTemperature.CELSIUS,
        "icon": "mdi:thermometer-lines",
    },
    "compressor_power": {
        "property_read": CLEAR_TEXT_NAMES["COMPRESSOR_POWER"],
        "name": "Compressor power",
        "device_class": SensorDeviceClass.POWER,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfPower.WATT,
        "icon": "mdi:lightning-bolt",
    },
    "addition_power": {
        "property_read": CLEAR_TEXT_NAMES["ADDITION_POWER"],
        "name": "Addition power",
        "device_class": SensorDeviceClass.POWER,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfPower.WATT,
        "icon": "mdi:heating-coil",
    },
    "compressor_freq": {
        "property_read": CLEAR_TEXT_NAMES["COMPRESSOR_FREQ"],
        "name": "Compressor frequency",
        "device_class": SensorDeviceClass.FREQUENCY,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfFrequency.HERTZ,
        "icon": "mdi:sine-wave",
    },
    "hot_water_priority": {
        "property_read": CLEAR_TEXT_NAMES["HW_PRIORITY"],
        "name": "Hot water priority",
        "device_class": None,
        "state_class": None,
        "unit": None,
        "icon": "mdi:priority-high",
        "options": {"entity_category": EntityCategory.DIAGNOSTIC},
    },
    "total_output_power": {
        "property_read": CLEAR_TEXT_NAMES["TOTAL_POWER"],
        "name": "Total output power",
        "device_class": SensorDeviceClass.POWER,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfPower.WATT,
        "icon": "mdi:lightning-bolt-circle",
    },
    "defrost_interval": {
        "property_read": CLEAR_TEXT_NAMES["DEFROST_INTERVAL"],
        "name": "Defrost interval",
        "device_class": None,
        "state_class": None,
        "unit": UnitOfTime.MINUTES,
        "icon": "mdi:timer",
        "options": {"entity_category": EntityCategory.DIAGNOSTIC},
    },
    "defrost_block_time": {
        "property_read": CLEAR_TEXT_NAMES["DEFROST_BLOCK_TIME"],
        "name": "Defroster block time",
        "device_class": None,
        "state_class": None,
        "unit": UnitOfTime.MINUTES,
        "icon": "mdi:timer-off",
        "options": {"entity_category": EntityCategory.DIAGNOSTIC},
    },
    "compressor_freq_max": {
        "property_read": CLEAR_TEXT_NAMES["COMPRESSOR_FREQ_MAX"],
        "name": "Compressor frequency max",
        "device_class": SensorDeviceClass.FREQUENCY,
        "state_class": None,
        "unit": UnitOfFrequency.HERTZ,
        "icon": "mdi:sine-wave",
        "options": {"entity_category": EntityCategory.DIAGNOSTIC},
    },
    "heater_element_allowed": {
        "property_read": CLEAR_TEXT_NAMES["HEATER_ELEMENT_ALLOWED"],
        "name": "Heater element allowed temperature",
        "device_class": SensorDeviceClass.TEMPERATURE,
        "state_class": None,
        "unit": UnitOfTemperature.CELSIUS,
        "icon": "mdi:thermometer-low",
        "options": {"entity_category": EntityCategory.DIAGNOSTIC},
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Comfortzone sensor entities."""
    if DOMAIN not in hass.data or entry.entry_id not in hass.data[DOMAIN]:
        _LOGGER.error("Comfortzone data missing for entry %s", entry.entry_id)
        return
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DataUpdateCoordinator = data.get("coordinator")
    if not coordinator:
        _LOGGER.error("Coordinator missing for %s", entry.entry_id)
        return
    entities = []
    for suffix, config in SENSOR_CONFIG.items():
        if "property_read" in config:
            entities.append(ComfortzoneSensorEntity(coordinator, entry, suffix, config))
    async_add_entities(entities)


class ComfortzoneSensorEntity(CoordinatorEntity, SensorEntity):
    """Representation of a Comfortzone Sensor entity."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default: bool = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
        entity_suffix: str,
        config: dict,
    ) -> None:
        """Initialize the sensor entity."""
        super().__init__(coordinator)
        self._config = config
        self.entry = entry
        self._entity_suffix = entity_suffix
        self._attr_unique_id = f"{device_unique_id(entry)}_{entity_suffix}"
        self._attr_name = config["name"]
        self._attr_device_class = config.get("device_class")
        self._attr_native_unit_of_measurement = config.get("unit")
        self._attr_state_class = config.get("state_class")
        self._attr_icon = config.get("icon")
        opts = config.get("options", {})
        if "entity_registry_enabled_default" in opts:
            self._attr_entity_registry_enabled_default = opts["entity_registry_enabled_default"]
        if "entity_category" in opts:
            self._attr_entity_category = opts["entity_category"]
        self._attr_device_info = build_device_info(entry)
        self._availability_property_key = opts.get("availability_property_key")
        self._availability_logic = opts.get("availability_logic", "exists")
        self._attr_native_value = None
        self._attr_available = self.coordinator.last_update_success
        if self.coordinator.data:
            self._update_state_from_coordinator()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        current_availability = self._attr_available
        current_value = self._attr_native_value
        self._update_state_from_coordinator()
        if (
            self._attr_native_value != current_value
            or self._attr_available != current_availability
        ):
            if self.hass:
                self.async_write_ha_state()

    def _update_state_from_coordinator(self) -> None:
        """Update the entity state from coordinator data."""
        new_value: StateType | None = None
        new_availability = True

        if not self.coordinator.last_update_success or not self.coordinator.data:
            new_availability = False
        else:
            data_dict = self.coordinator.data.get("Data", {})
            values_list = data_dict.get("Values")
            prop_read = self._config["property_read"]

            value_str: Optional[str] = None
            if prop_read == "LogDateTimeUtc":
                value_str = data_dict.get("LogDateTimeUtc")
            elif values_list is not None:
                value_str = find_value_from_raw_data(values_list, prop_read, "ClearTextName")

            if new_availability and self._availability_property_key:
                availability_value_str = find_value_from_raw_data(
                    values_list, self._availability_property_key, "ClearTextName"
                )
                if availability_value_str is None:
                    new_availability = False
                elif self._availability_logic == "not_empty" and availability_value_str == "":
                    new_availability = False

            if new_availability:
                if value_str is None:
                    _LOGGER.debug("Property '%s' missing for %s", prop_read, self.name)
                    new_availability = False
                else:
                    try:
                        if self.device_class == SensorDeviceClass.TEMPERATURE:
                            new_value = float(value_str)
                        elif self.device_class == SensorDeviceClass.TIMESTAMP:
                            parsed = dt_util.parse_datetime(value_str)
                            assert parsed is not None
                            new_value = parsed
                        elif self.device_class == SensorDeviceClass.POWER:
                            new_value = int(value_str)
                        elif self.device_class == SensorDeviceClass.FREQUENCY:
                            new_value = int(value_str)
                        elif self.native_unit_of_measurement == PERCENTAGE:
                            new_value = int(float(value_str))
                        elif self.native_unit_of_measurement == UnitOfTime.DAYS:
                            new_value = int(value_str)
                        elif (
                            self.native_unit_of_measurement is None
                            and self._attr_state_class == SensorStateClass.MEASUREMENT
                        ):
                            try:
                                new_value = int(value_str)
                            except ValueError:
                                new_value = float(value_str)
                        else:
                            new_value = value_str
                    except (ValueError, TypeError, AssertionError):
                        _LOGGER.warning(
                            "Could not parse sensor value for %s: %s", self.name, value_str
                        )
                        new_availability = False
                        new_value = None

        self._attr_available = new_availability
        if self._attr_available and new_value != self._attr_native_value:
            self._attr_native_value = new_value
        elif not self._attr_available:
            self._attr_native_value = None
