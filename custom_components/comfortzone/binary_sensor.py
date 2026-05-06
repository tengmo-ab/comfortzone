"""Binary Sensor entities for Comfortzone Heat Pump integration."""
import logging
from typing import Any, Dict, Optional, List

from homeassistant.components.binary_sensor import ( BinarySensorEntity, BinarySensorDeviceClass )
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

# Import helper function and constants
from .const import DOMAIN, CLEAR_TEXT_NAMES, BINARY_SENSOR_MAP
from .api import find_value_from_raw_data

_LOGGER = logging.getLogger(__name__)

# --- Config now just lists keys from BINARY_SENSOR_MAP + details ---
BINARY_SENSOR_TYPES = {
    "filter_alarm": {"name": "Filter Alarm Active", "device_class": BinarySensorDeviceClass.PROBLEM, "on_value": "1"},
    "main_alarm": {"name": "Alarm Active", "device_class": BinarySensorDeviceClass.PROBLEM, "on_value": None}, # Special case check
    "compressor_active": {"name": "Compressor Active", "device_class": BinarySensorDeviceClass.RUNNING, "on_value": "1"},
    "room_thermostat": {"name": "Room Thermostat Active", "device_class": BinarySensorDeviceClass.CONNECTIVITY, "on_value": "1", "options": {"entity_registry_enabled_default": False}},
    # --- ADDED VALVE SENSORS ---
    "heating_valve": {
        "name": "Heating Valve Active", "device_class": BinarySensorDeviceClass.HEAT, # Use HEAT class
        "on_value": "1", # Assumes "1" means heating valve is active
        "options": {"entity_registry_enabled_default": False} # Diagnostic, disable default
    },
    "hot_water_valve": {
        "name": "Hot Water Valve Active", "device_class": None, # Generic
        "icon_on": "mdi:valve-open", "icon_off": "mdi:valve-closed", # Custom icons
        "on_value": "1", # Assumes "1" means HW valve is active
        "options": {"entity_registry_enabled_default": False} # Diagnostic, disable default
    },
    "cooling_installed": {"name": "Cooling Installed", "device_class": None, "icon_on": "mdi:snowflake", "icon_off": "mdi:snowflake-off", "on_value": "1"},
    "cooling_enabled": {"name": "Cooling Enabled", "device_class": None, "icon_on": "mdi:power", "icon_off": "mdi:power-sleep", "on_value": "1"},
    "dual_heating_curves": {"name": "Dual Heating Curves", "device_class": None, "icon_on": "mdi:chart-bell-curve", "icon_off": "mdi:chart-line", "on_value": "1"},
}

async def async_setup_entry( hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,) -> None:
    """Set up the Comfortzone binary sensor entities."""
    if DOMAIN not in hass.data or entry.entry_id not in hass.data[DOMAIN]: _LOGGER.error("Comfortzone Heat Pump data not found for entry %s", entry.entry_id); return
    data = hass.data[DOMAIN][entry.entry_id]; coordinator: DataUpdateCoordinator = data.get("coordinator")
    if not coordinator: _LOGGER.error("Coordinator not found for entry %s", entry.entry_id); return

    entities = []
    for suffix, config_details in BINARY_SENSOR_TYPES.items():
        if suffix in BINARY_SENSOR_MAP:
             # Combine details from BINARY_SENSOR_TYPES with lookup key from BINARY_SENSOR_MAP
             config = {**config_details, "property_read": BINARY_SENSOR_MAP[suffix], "entity_suffix": suffix}
             entities.append(ComfortzoneBinarySensorEntity(coordinator, entry, suffix, config))
        else: _LOGGER.error("Binary sensor suffix '%s' not found in BINARY_SENSOR_MAP in const.py", suffix)

    async_add_entities(entities)

class ComfortzoneBinarySensorEntity(CoordinatorEntity, BinarySensorEntity):
    """Representation of a Comfortzone Binary Sensor entity."""
    _attr_entity_registry_enabled_default: bool = True

    def __init__( self, coordinator: DataUpdateCoordinator, entry: ConfigEntry, entity_suffix: str, config: dict,) -> None:
        """Initialize the binary sensor entity."""
        super().__init__(coordinator)
        self._config = config
        self.entry = entry
        self._entity_suffix = entity_suffix
        self._attr_unique_id = f"{entry.entry_id}_{entity_suffix}"
        self._attr_name = config["name"]
        self._attr_device_class = config.get("device_class")
        if "entity_registry_enabled_default" in config.get("options", {}): self._attr_entity_registry_enabled_default = config["options"]["entity_registry_enabled_default"]
        self._attr_device_info = { "identifiers": {(DOMAIN, entry.entry_id)} }
        self._property_read = config["property_read"]
        self._on_value = config.get("on_value")
        self._attr_is_on = None
        self._attr_available = self.coordinator.last_update_success
        if self.coordinator.data: self._update_state_from_coordinator()

    @property
    def suggested_object_id(self) -> str | None:
        """Suggest object ID."""
        return f"{DOMAIN}_{self._entity_suffix}"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        current_availability = self._attr_available; current_is_on = self._attr_is_on
        self._update_state_from_coordinator()
        if self._attr_is_on != current_is_on or self._attr_available != current_availability:
            if self.hass: self.async_write_ha_state()

    # --- UPDATED: Use find_value helper and check logic ---
    def _update_state_from_coordinator(self) -> None:
        """Update the entity state from coordinator data."""
        new_state = self._attr_is_on
        new_availability = True

        if not self.coordinator.last_update_success or not self.coordinator.data or 'Values' not in self.coordinator.data.get("Data", {}):
            new_availability = False
        else:
            values_list = self.coordinator.data.get("Data", {}).get("Values", [])
            value_str = find_value_from_raw_data(values_list, self._property_read, "ClearTextName")

            if value_str is None:
                _LOGGER.debug("Read property '%s' not found for binary sensor %s", self._property_read, self.name)
                new_availability = False
            else:
                # Special check for AlarmText (ON if not empty string)
                if self._property_read == CLEAR_TEXT_NAMES["ALARM_TEXT"]: new_state = (value_str != "")
                # Normal check comparing to configured ON value string (e.g. "1")
                elif self._on_value is not None: new_state = (value_str == self._on_value)
                else: new_state = False # Default to False if on_value not defined

        self._attr_available = new_availability
        self._attr_is_on = new_state if new_availability else None
