"""Sensor entities for Comfortzone Heat Pump integration."""
import logging
from typing import Any, Optional, Dict, List

from homeassistant.components.sensor import ( SensorEntity, SensorDeviceClass, SensorStateClass )
from homeassistant.config_entries import ConfigEntry
# Added Power and Frequency units
from homeassistant.const import UnitOfTemperature, UnitOfTime, PERCENTAGE, UnitOfPower, UnitOfFrequency
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

# Import helper function and constants
from .const import DOMAIN, CLEAR_TEXT_NAMES
from .api import find_value_from_raw_data # Use only the find_value helper

_LOGGER = logging.getLogger(__name__)

# --- UPDATED SENSOR CONFIG using ClearTextNames and adding new ones ---
SENSOR_CONFIG: Dict[str, Dict[str, Any]] = {
    "indoor_temp": {"property_read": CLEAR_TEXT_NAMES["INDOOR_TEMP"], "name": "Indoor Temperature", "device_class": SensorDeviceClass.TEMPERATURE,"state_class": SensorStateClass.MEASUREMENT, "unit": UnitOfTemperature.CELSIUS, "icon": "mdi:home-thermometer-outline"},
    "set_indoor_temp": {"property_read": CLEAR_TEXT_NAMES["TARGET_INDOOR_TEMP"], "name": "Target Indoor Temperature", "device_class": SensorDeviceClass.TEMPERATURE, "state_class": None, "unit": UnitOfTemperature.CELSIUS, "icon": "mdi:thermostat"},
    "hot_water_temp": {"property_read": CLEAR_TEXT_NAMES["HOT_WATER_TEMP"], "name": "Hot Water Temperature", "device_class": SensorDeviceClass.TEMPERATURE, "state_class": SensorStateClass.MEASUREMENT, "unit": UnitOfTemperature.CELSIUS, "icon": "mdi:water-thermometer"},
    "target_hw_temp": {"property_read": CLEAR_TEXT_NAMES["TARGET_HW_TEMP"], "name": "Target Hot Water Temp Readback", "device_class": SensorDeviceClass.TEMPERATURE, "state_class": None, "unit": UnitOfTemperature.CELSIUS, "icon": "mdi:target", "options": {"entity_registry_enabled_default": False}},
    "outdoor_temp": {"property_read": CLEAR_TEXT_NAMES["OUTDOOR_TEMP"], "name": "Outdoor Temperature", "device_class": SensorDeviceClass.TEMPERATURE, "state_class": SensorStateClass.MEASUREMENT, "unit": UnitOfTemperature.CELSIUS, "icon": "mdi:thermometer"},
    "heat_carrier_in": {"property_read": CLEAR_TEXT_NAMES["FLOW_TEMP"], "name": "Heat Carrier In Temperature", "device_class": SensorDeviceClass.TEMPERATURE, "state_class": SensorStateClass.MEASUREMENT, "unit": UnitOfTemperature.CELSIUS, "icon": "mdi:coolant-temperature"},
    "heat_carrier_out": {"property_read": CLEAR_TEXT_NAMES["RETURN_TEMP"], "name": "Heat Carrier Out Temperature", "device_class": SensorDeviceClass.TEMPERATURE, "state_class": SensorStateClass.MEASUREMENT, "unit": UnitOfTemperature.CELSIUS, "icon": "mdi:coolant-temperature"},
    "heating_curve_readback": {"property_read": CLEAR_TEXT_NAMES["HEATING_CURVE"], "name": "Heating Curve Readback", "device_class": None, "state_class": None, "unit": None, "icon": "mdi:chart-line", "options": {"entity_registry_enabled_default": False}},
    "alarm_text": {"property_read": CLEAR_TEXT_NAMES["ALARM_TEXT"], "name": "Alarm Text", "device_class": None, "state_class": None, "unit": None, "icon": "mdi:alert-outline", "options": {"availability_property_key": CLEAR_TEXT_NAMES["ALARM_TEXT"], "availability_logic": "not_empty"}}, # Link availability to this text
    "time_to_filter_change": {"property_read": "Time to filter change", "name": "Time to Filter Change", "device_class": None, "state_class": SensorStateClass.MEASUREMENT, "unit": UnitOfTime.DAYS, "icon": "mdi:filter-clock"},
    "circulation_pump_speed": {"property_read": CLEAR_TEXT_NAMES["CIRC_PUMP_SPEED"], "name": "Circulation Pump Speed", "device_class": None, "state_class": SensorStateClass.MEASUREMENT, "unit": PERCENTAGE, "icon": "mdi:pump"},
    "fan_speed_current": {"property_read": CLEAR_TEXT_NAMES["FAN_SPEED_CURRENT"], "name": "Current Fan Speed", "device_class": None, "state_class": SensorStateClass.MEASUREMENT, "unit": PERCENTAGE, "icon": "mdi:fan"},
    "fan_state_raw": {"property_read": CLEAR_TEXT_NAMES["FAN_STATE"], "name": "Fan State Raw", "device_class": None, "state_class": None, "unit": None, "icon": "mdi:fan-alert", "options": {"entity_registry_enabled_default": False}},
    "last_log_time": {"property_read": "LogDateTimeUtc", "name": "Last Log Time", "device_class": SensorDeviceClass.TIMESTAMP, "state_class": None, "unit": None, "icon": "mdi:clock-outline", "options": {"entity_registry_enabled_default": False}},
    # --- Newly Added Sensors ---
    "exhaust_air_temp": {"property_read": CLEAR_TEXT_NAMES["EXHAUST_AIR_TEMP"], "name": "Exhaust Air Temperature", "device_class": SensorDeviceClass.TEMPERATURE, "state_class": SensorStateClass.MEASUREMENT, "unit": UnitOfTemperature.CELSIUS, "icon": "mdi:thermometer-lines"},
    "compressor_power": {"property_read": CLEAR_TEXT_NAMES["COMPRESSOR_POWER"], "name": "Compressor Power", "device_class": SensorDeviceClass.POWER, "state_class": SensorStateClass.MEASUREMENT, "unit": UnitOfPower.WATT, "icon": "mdi:lightning-bolt"},
    "addition_power": {"property_read": CLEAR_TEXT_NAMES["ADDITION_POWER"], "name": "Addition Power", "device_class": SensorDeviceClass.POWER, "state_class": SensorStateClass.MEASUREMENT, "unit": UnitOfPower.WATT, "icon": "mdi:heating-coil"},
    "compressor_freq": {"property_read": CLEAR_TEXT_NAMES["COMPRESSOR_FREQ"], "name": "Compressor Frequency", "device_class": SensorDeviceClass.FREQUENCY, "state_class": SensorStateClass.MEASUREMENT, "unit": UnitOfFrequency.HERTZ, "icon": "mdi:sine-wave"},
    "hot_water_priority": {"property_read": CLEAR_TEXT_NAMES["HW_PRIORITY"], "name": "Hot Water Priority", "device_class": None, "state_class": SensorStateClass.MEASUREMENT, "unit": None, "icon": "mdi:priority-high"}, # Measurement makes sense if it changes
    "total_output_power": {"property_read": CLEAR_TEXT_NAMES["TOTAL_POWER"], "name": "Total Output Power", "device_class": SensorDeviceClass.POWER, "state_class": SensorStateClass.MEASUREMENT, "unit": UnitOfPower.WATT, "icon": "mdi:lightning-bolt-circle"},
    "defrost_interval": {"property_read": CLEAR_TEXT_NAMES["DEFROST_INTERVAL"], "name": "Defrost Interval", "device_class": None, "state_class": SensorStateClass.MEASUREMENT, "unit": UnitOfTime.MINUTES, "icon": "mdi:timer"},
    "defrost_block_time": {"property_read": CLEAR_TEXT_NAMES["DEFROST_BLOCK_TIME"], "name": "Defroster Block Time", "device_class": None, "state_class": SensorStateClass.MEASUREMENT, "unit": UnitOfTime.MINUTES, "icon": "mdi:timer-off"},
    "compressor_freq_max": {"property_read": CLEAR_TEXT_NAMES["COMPRESSOR_FREQ_MAX"], "name": "Compressor Frequency Max", "device_class": SensorDeviceClass.FREQUENCY, "state_class": SensorStateClass.MEASUREMENT, "unit": UnitOfFrequency.HERTZ, "icon": "mdi:sine-wave"},
    "heater_element_allowed": {"property_read": CLEAR_TEXT_NAMES["HEATER_ELEMENT_ALLOWED"], "name": "Heater Element Allowed Temp", "device_class": SensorDeviceClass.TEMPERATURE, "state_class": SensorStateClass.MEASUREMENT, "unit": UnitOfTemperature.CELSIUS, "icon": "mdi:thermometer-low"},
}

async def async_setup_entry( hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,) -> None:
    """Set up the Comfortzone sensor entities."""
    # ... (Setup logic remains same) ...
    if DOMAIN not in hass.data or entry.entry_id not in hass.data[DOMAIN]: _LOGGER.error("Comfortzone Heat Pump data not found for entry %s", entry.entry_id); return
    data = hass.data[DOMAIN][entry.entry_id]; coordinator: DataUpdateCoordinator = data.get("coordinator")
    if not coordinator: _LOGGER.error("Coordinator not found for entry %s", entry.entry_id); return
    entities = []
    for suffix, config in SENSOR_CONFIG.items():
         if "property_read" in config: entities.append(ComfortzoneSensorEntity(coordinator, entry, suffix, config))
         else: _LOGGER.error("Sensor config missing property_read: %s", config)
    async_add_entities(entities)


class ComfortzoneSensorEntity(CoordinatorEntity, SensorEntity):
    """Representation of a Comfortzone Sensor entity."""
    _attr_entity_registry_enabled_default: bool = True

    def __init__( self, coordinator: DataUpdateCoordinator, entry: ConfigEntry, entity_suffix: str, config: dict,) -> None:
        """Initialize the sensor entity."""
        # ... (__init__ remains same, stores config) ...
        super().__init__(coordinator); self._config = config; self.entry = entry; self._entity_suffix = entity_suffix
        self._attr_unique_id = f"{entry.entry_id}_{entity_suffix}"; self._attr_name = config["name"]; self._attr_device_class = config.get("device_class")
        self._attr_native_unit_of_measurement = config.get("unit"); self._attr_state_class = config.get("state_class"); self._attr_icon = config.get("icon")
        if "entity_registry_enabled_default" in config.get("options", {}): self._attr_entity_registry_enabled_default = config["options"]["entity_registry_enabled_default"]
        self._attr_device_info = { "identifiers": {(DOMAIN, entry.entry_id)} }
        self._availability_property_key = config.get("options", {}).get("availability_property_key") # Use specific key name
        self._availability_logic = config.get("options", {}).get("availability_logic", "exists") # Default check is just existence
        self._attr_native_value = None; self._attr_available = self.coordinator.last_update_success
        if self.coordinator.data: self._update_state_from_coordinator()

    @property
    def suggested_object_id(self) -> str | None:
        """Suggest object ID."""
        # ... (remains same) ...
        return f"{DOMAIN}_{self._entity_suffix}"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # ... (remains same) ...
        current_availability = self._attr_available; current_value = self._attr_native_value; self._update_state_from_coordinator()
        if self._attr_native_value != current_value or self._attr_available != current_availability:
            if self.hass: self.async_write_ha_state()

    # --- UPDATED: Uses find_value helper, parses values, checks availability prop ---
    def _update_state_from_coordinator(self) -> None:
        """Update the entity state from coordinator data."""
        new_value: StateType | None = None
        new_availability = True # Assume available unless proven otherwise

        if not self.coordinator.last_update_success or not self.coordinator.data:
            new_availability = False
        else:
            # Data structure is now direct response from RawData
            data_dict = self.coordinator.data.get("Data", {})
            values_list = data_dict.get("Values")
            prop_read = self._config["property_read"]

            value_str: Optional[str] = None
            # Handle special cases reading directly from Data object
            if prop_read == "LogDateTimeUtc":
                 value_str = data_dict.get("LogDateTimeUtc")
            # Default: read from Values list using helper
            elif values_list is not None:
                 value_str = find_value_from_raw_data(values_list, prop_read, "ClearTextName")
            # else: value_str remains None if values_list missing

            # Check availability property if defined
            if new_availability and self._availability_property_key:
                availability_value_str = find_value_from_raw_data(values_list, self._availability_property_key, "ClearTextName")
                if availability_value_str is None: # If availability prop missing, treat main sensor as unavailable
                     new_availability = False
                elif self._availability_logic == "not_empty":
                     if availability_value_str == "": new_availability = False
                # Add other availability logic checks here if needed based on value != "0" etc.

            # Process the main value if still available
            if new_availability:
                if value_str is None:
                    # Allow PoolTemp to have None value without making sensor unavailable
                    if prop_read == CLEAR_TEXT_NAMES.get("POOL_TEMP", "PoolTemp"):
                        new_value = None
                    else: # Otherwise, missing value means unavailable
                         _LOGGER.debug("Read property '%s' not found or is None for sensor %s", prop_read, self.name)
                         new_availability = False
                         new_value = None
                else:
                    # Try conversions based on device class / unit
                    try:
                        if self.device_class == SensorDeviceClass.TEMPERATURE: new_value = float(value_str)
                        elif self.device_class == SensorDeviceClass.TIMESTAMP: new_value = dt_util.parse_datetime(value_str); assert new_value is not None
                        elif self.device_class == SensorDeviceClass.POWER: new_value = int(value_str)
                        elif self.device_class == SensorDeviceClass.FREQUENCY: new_value = int(value_str)
                        elif self.native_unit_of_measurement == PERCENTAGE: new_value = int(float(value_str))
                        elif self.native_unit_of_measurement == UnitOfTime.DAYS: new_value = int(value_str)
                        # Fallback for unitless or unknown numeric types - try int first, then float, then string
                        elif self.native_unit_of_measurement is None and self._attr_state_class == SensorStateClass.MEASUREMENT:
                             try: new_value = int(value_str)
                             except ValueError: new_value = float(value_str)
                        else: new_value = value_str # Default to string
                    except (ValueError, TypeError, AssertionError): _LOGGER.warning("Could not parse sensor value for %s: %s", self.name, value_str); new_availability = False; new_value = None

        # --- Final State Assignment ---
        if self._attr_available != new_availability: _LOGGER.debug("Sensor '%s' availability changed to %s", self.name, new_availability)
        self._attr_available = new_availability
        if self._attr_available and new_value != self._attr_native_value: self._attr_native_value = new_value
        elif not self._attr_available: self._attr_native_value = None
