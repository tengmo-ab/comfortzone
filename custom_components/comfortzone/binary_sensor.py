"""Binary sensor entities for Comfortzone Heat Pump integration."""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from typing import Deque, Optional, Tuple

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import find_value_from_raw_data
from .computed_sensors import _coordinator_values, _is_hot_water, _read_float
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

    # Computed/heuristic binary sensors that don't map to a single API field
    entities.append(ShowerInProgressBinarySensor(coordinator, entry))

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


class ShowerInProgressBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Heuristic binary sensor: True when hot water is being drawn rapidly.

    Watches the hot-water tank temperature for a sustained downward slope
    while the pump is **not** producing hot water. A typical shower draws
    enough water from a 170 L tank to drop the top sensor by 1-3 °C in a
    few minutes, which is easy to distinguish from standing-loss decay
    (which is closer to 0.1-0.3 °C/h).

    Useful for automations: turn off pre-heat schedules, switch on a
    bathroom fan, log shower frequency, or feed shower events into the
    energy management project as "do not start hot water now, the tank is
    actively being used".
    """

    _attr_has_entity_name = True
    _attr_name = "Shower in progress"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:shower-head"

    # How rapidly the tank temperature must fall to count as a draw
    DROP_THRESHOLD_C_PER_MIN = 0.25
    # Trailing window over which the slope is computed
    WINDOW_SECONDS = 4 * 60
    # Hold the "on" state for this long after slope returns to normal
    TRAIL_SECONDS = 90

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{device_unique_id(entry)}_shower_in_progress"
        self._attr_device_info = build_device_info(entry)
        self._samples: Deque[Tuple[datetime, float]] = deque()
        self._last_active_at: Optional[datetime] = None
        self._attr_is_on = False
        self._attr_extra_state_attributes = {}

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        now = dt_util.utcnow()
        if values is None:
            self._attr_available = False
            self.async_write_ha_state()
            return

        hw_temp = _read_float(values, CLEAR_TEXT_NAMES["HOT_WATER_TEMP"])
        if hw_temp is None:
            self._attr_available = False
            self.async_write_ha_state()
            return

        # Maintain rolling window
        self._samples.append((now, hw_temp))
        cutoff = now.timestamp() - self.WINDOW_SECONDS
        while self._samples and self._samples[0][0].timestamp() < cutoff:
            self._samples.popleft()

        slope_per_min: Optional[float] = None
        if len(self._samples) >= 2:
            first_t, first_v = self._samples[0]
            span_min = (now - first_t).total_seconds() / 60.0
            if span_min >= 0.5:
                slope_per_min = (hw_temp - first_v) / span_min

        is_drawing = (
            slope_per_min is not None
            and slope_per_min <= -self.DROP_THRESHOLD_C_PER_MIN
            and not _is_hot_water(values)
        )

        if is_drawing:
            self._last_active_at = now
            self._attr_is_on = True
        elif (
            self._last_active_at
            and (now - self._last_active_at).total_seconds() < self.TRAIL_SECONDS
        ):
            self._attr_is_on = True
        else:
            self._attr_is_on = False

        self._attr_available = True
        self._attr_extra_state_attributes = {
            "hot_water_temp": hw_temp,
            "slope_c_per_min": (
                round(slope_per_min, 3) if slope_per_min is not None else None
            ),
            "drop_threshold_c_per_min": -self.DROP_THRESHOLD_C_PER_MIN,
        }
        self.async_write_ha_state()
