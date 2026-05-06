"""Constants for the Comfortzone Heat Pump integration."""

DOMAIN = "comfortzone"

API_ENDPOINT = "https://platform.loggamera.se/Api/v1/RawData"
API_ENDPOINT_SET = "https://platform.loggamera.se/Api/v1/SetProperty"

CONF_API_KEY = "api_key"
CONF_DEVICE_ID = "device_id"
CONF_MODEL = "model"

# Target temp value used to signify "OFF" mode for the climate entity
TEMP_VALUE_FOR_OFF = 10.0

# Delay in seconds before refreshing coordinator after a successful 'set' command
DELAY_REFRESH_AFTER_SET = 20

# ClearTextNames needed for parsing RawData
CLEAR_TEXT_NAMES = {
    # Climate / Core Temps
    "INDOOR_TEMP": "Indoor temp (TE3)",
    "OUTDOOR_TEMP": "Outdoor temp (TE0)",
    "TARGET_INDOOR_TEMP": "Indoor temp set temp",
    "HOT_WATER_TEMP": "Hot water temp (TE24)",
    "TARGET_HW_TEMP": "Hot water set temp",
    "FLOW_TEMP": "Flow line temp (TE1)",
    "RETURN_TEMP": "Return temp (TE2)",
    # Setpoints / Settings
    "HEATING_CURVE": "Heating curve",
    "HOLIDAY_DAYS": "Holiday time (days)",
    "HW_EXTRA_MODE": "Extra hot water mode",
    # States / Alarms / Valves
    "COMPRESSOR_ACTIVE": "Compressor active",
    "EXCHANGE_VALVE_HEATING": "Exchange valve heating (on/off)",
    "EXCHANGE_VALVE_HW": "Exchange valve hot water (on/off)",
    "FILTER_ALARM": "Filter alarm (on/off)",
    "ALARM_TEXT": "AlarmInClearText",
    "FAN_STATE": "Fan state",
    "ROOM_THERMOSTAT_SWITCH": "Room thermostat switch (IN7)",
    # Power / frequency / fan
    "EXHAUST_AIR_TEMP": "Exhaust air temp (TE7)",
    "COMPRESSOR_POWER": "Compressor effect",
    "ADDITION_POWER": "Addition effect",
    "COMPRESSOR_FREQ": "Compressor frequency",
    "HW_PRIORITY": "Hot water priority",
    "CIRC_PUMP_SPEED": "Circulation pump speed",
    "FAN_SPEED_CURRENT": "Fan speed (current)",
    "TOTAL_POWER": "Total output power",
    # Diagnostics / config readback
    "DEFROST_INTERVAL": "Defrost interval",
    "DEFROST_BLOCK_TIME": "Defroster block time",
    "COMPRESSOR_FREQ_MAX": "Compressor freq. max",
    "COOLING_INSTALLED": "Cooling installed",
    "COOLING_ENABLED": "Cooling enabled",
    "DUAL_HEATING_CURVES": "Dual heating curves",
    "HEATER_ELEMENT_ALLOWED": "Heater element allowed",
}

# Maps binary_sensor suffix -> ClearTextName
BINARY_SENSOR_MAP = {
    "filter_alarm": CLEAR_TEXT_NAMES["FILTER_ALARM"],
    "main_alarm": CLEAR_TEXT_NAMES["ALARM_TEXT"],
    "compressor_active": CLEAR_TEXT_NAMES["COMPRESSOR_ACTIVE"],
    "room_thermostat": CLEAR_TEXT_NAMES["ROOM_THERMOSTAT_SWITCH"],
    "heating_valve": CLEAR_TEXT_NAMES["EXCHANGE_VALVE_HEATING"],
    "hot_water_valve": CLEAR_TEXT_NAMES["EXCHANGE_VALVE_HW"],
    "cooling_installed": CLEAR_TEXT_NAMES["COOLING_INSTALLED"],
    "cooling_enabled": CLEAR_TEXT_NAMES["COOLING_ENABLED"],
    "dual_heating_curves": CLEAR_TEXT_NAMES["DUAL_HEATING_CURVES"],
}
