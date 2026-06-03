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

from .calculations import (
    compressor_active as _compressor_active,
    find_value_from_raw_data,
    is_hot_water as _is_hot_water,
    is_truthy,
    read_float as _read_float,
)
from .computed_sensors import _coordinator_values
from .const import (
    BINARY_SENSOR_MAP,
    CLEAR_TEXT_NAMES,
    CONF_ADDITION_DURATION_THRESHOLD_S,
    CONF_ADDITION_POWER_THRESHOLD_W,
    CONF_FILTER_WARNING_DAYS,
    CONF_LOW_HW_HYSTERESIS_C,
    CONF_LOW_HW_THRESHOLD_C,
    CONF_MAX_LOAD_DURATION_S,
    CONF_MAX_LOAD_THRESHOLD_PCT,
    CONF_SHORT_CYCLE_THRESHOLD,
    DEFAULT_ADDITION_DURATION_THRESHOLD_S,
    DEFAULT_ADDITION_POWER_THRESHOLD_W,
    DEFAULT_FILTER_WARNING_DAYS,
    DEFAULT_LOW_HW_HYSTERESIS_C,
    DEFAULT_LOW_HW_THRESHOLD_C,
    DEFAULT_MAX_LOAD_DURATION_S,
    DEFAULT_MAX_LOAD_THRESHOLD_PCT,
    DEFAULT_SHORT_CYCLE_THRESHOLD,
    DOMAIN,
)
from .entity import build_device_info, device_unique_id


def _option(entry, key, default):
    """Return the option value or the default if unset/empty."""
    raw = entry.options.get(key, default)
    if raw in (None, ""):
        return default
    return raw

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
    entities.append(ShortCyclingBinarySensor(coordinator, entry))
    entities.append(AdditionHeaterActiveBinarySensor(coordinator, entry))
    entities.append(FilterChangeSoonBinarySensor(coordinator, entry))
    entities.append(LowHotWaterBinarySensor(coordinator, entry))
    entities.append(CompressorRunningAtMaxBinarySensor(coordinator, entry))

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
                # Use the same float-tolerant truthiness check as elsewhere:
                # the API can deliver "1" / "1.0" / "0.0" interchangeably.
                if str(self._on_value) == "1":
                    new_state = is_truthy(value_str)
                else:
                    new_state = str(value_str).strip() == str(self._on_value)
            else:
                new_state = False

        self._attr_available = new_availability
        self._attr_is_on = new_state if new_availability else None


class ShowerInProgressBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Heuristic binary sensor: True when hot water is being drawn rapidly.

    The detection compares the **actual** tank-temperature slope against
    the slope we'd **expect** given what the heat pump is doing right now:

    * When idle, the tank loses ~0.05 °C/min from standing losses. Anything
      meaningfully faster than that is water being drawn.
    * When the pump is producing hot water, the tank should be rising
      by roughly 0.3-0.6 °C/min. A near-flat or slightly negative slope
      during production is a strong signal of a shower beating the
      production rate.

    The trigger condition is therefore "actual slope falls more than
    ``DEVIATION_THRESHOLD_C_PER_MIN`` below the expected slope", combined
    with a small absolute-drop guard to suppress sensor-noise blips.

    Useful for automations: turn off pre-heat schedules, switch on a
    bathroom fan, log shower frequency, or feed shower events into the
    energy management project as "the tank is actively being used".
    """

    _attr_has_entity_name = True
    _attr_name = "Shower in progress"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:shower-head"

    # Expected slope of the tank-temperature reading in °C/min.
    # Negative = cooling, positive = warming. Calibrated against
    # observed RX95 behaviour on a 170 L tank.
    EXPECTED_SLOPE_IDLE = -0.05
    EXPECTED_SLOPE_HW_PRODUCTION = 0.40

    # Actual slope must fall this many °C/min *below* expected before
    # we count it as water being drawn. Larger negative number = stricter.
    DEVIATION_THRESHOLD_C_PER_MIN = -0.20

    # The tank must actually have dropped at least this much over the
    # rolling window before the alarm fires — protects against the
    # occasional single-poll temperature blip.
    MIN_ABSOLUTE_DROP_C = 0.5

    # Sliding window for slope computation.
    WINDOW_SECONDS = 5 * 60

    # Hold the "on" state for this long after the slope clears the
    # threshold, so the sensor doesn't toggle off briefly between draws.
    TRAIL_SECONDS = 120

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
        absolute_drop: float = 0.0
        if len(self._samples) >= 2:
            first_t, first_v = self._samples[0]
            span_min = (now - first_t).total_seconds() / 60.0
            if span_min >= 0.5:
                slope_per_min = (hw_temp - first_v) / span_min
                # Positive when the window contains an actual drop.
                absolute_drop = first_v - hw_temp

        pump_making_hw = _is_hot_water(values)
        expected_slope = (
            self.EXPECTED_SLOPE_HW_PRODUCTION
            if pump_making_hw
            else self.EXPECTED_SLOPE_IDLE
        )
        deviation = (
            slope_per_min - expected_slope if slope_per_min is not None else None
        )

        is_drawing = (
            deviation is not None
            and deviation <= self.DEVIATION_THRESHOLD_C_PER_MIN
            and absolute_drop >= self.MIN_ABSOLUTE_DROP_C
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
            "expected_slope_c_per_min": expected_slope,
            "deviation_c_per_min": (
                round(deviation, 3) if deviation is not None else None
            ),
            "deviation_threshold_c_per_min": self.DEVIATION_THRESHOLD_C_PER_MIN,
            "absolute_drop_c": round(absolute_drop, 2),
            "pump_making_hot_water": pump_making_hw,
        }
        self.async_write_ha_state()


class _ComfortzoneAlarmBase(CoordinatorEntity, BinarySensorEntity):
    """Common boilerplate for the heuristic alarm-style binary sensors."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
        suffix: str,
        name: str,
        icon: Optional[str] = None,
    ) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{device_unique_id(entry)}_{suffix}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = build_device_info(entry)
        self._attr_is_on = False


class ShortCyclingBinarySensor(_ComfortzoneAlarmBase):
    """Flags when the compressor is starting too often.

    Inverter heat pumps should ramp speed rather than turn the compressor
    on and off repeatedly. More than the configured threshold of starts
    in the last hour suggests short cycling — typically caused by an
    undersized heat emitter, low refrigerant charge, or oversized
    hysteresis. Sustained short cycling shortens compressor life
    dramatically. Threshold is configurable via the options flow.
    """

    WINDOW_SECONDS = 60 * 60

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="compressor_short_cycling",
            name="Compressor short-cycling",
            icon="mdi:alert-octagon",
        )
        self._start_times: Deque[datetime] = deque()
        self._last_was_running: bool = False

    def _threshold(self) -> int:
        return int(_option(
            self.entry, CONF_SHORT_CYCLE_THRESHOLD, DEFAULT_SHORT_CYCLE_THRESHOLD
        ))

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None:
            self._attr_available = False
            self.async_write_ha_state()
            return
        now = dt_util.utcnow()
        running = _compressor_active(values)
        if running and not self._last_was_running:
            self._start_times.append(now)
        cutoff = now.timestamp() - self.WINDOW_SECONDS
        while self._start_times and self._start_times[0].timestamp() < cutoff:
            self._start_times.popleft()
        self._last_was_running = running

        threshold = self._threshold()
        starts = len(self._start_times)
        self._attr_is_on = starts >= threshold
        self._attr_available = True
        self._attr_extra_state_attributes = {
            "starts_last_hour": starts,
            "threshold": threshold,
        }
        self.async_write_ha_state()


class AdditionHeaterActiveBinarySensor(_ComfortzoneAlarmBase):
    """Flags when the resistive addition heater (`elpatron`) has been
    drawing meaningful power for a sustained period.

    The whole point of running an exhaust-air heat pump is to *avoid*
    the COP-1 resistive heater. A few brief activations during defrost
    or DHW boost are fine; sustained activation worth surfacing so the
    user (or a controller) can react. Both the power threshold and the
    duration are configurable via the options flow.
    """

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="addition_heater_active",
            name="Addition heater active",
            icon="mdi:heating-coil",
        )
        self._active_since: Optional[datetime] = None

    def _power_threshold(self) -> float:
        return float(_option(
            self.entry,
            CONF_ADDITION_POWER_THRESHOLD_W,
            DEFAULT_ADDITION_POWER_THRESHOLD_W,
        ))

    def _duration_threshold(self) -> int:
        return int(_option(
            self.entry,
            CONF_ADDITION_DURATION_THRESHOLD_S,
            DEFAULT_ADDITION_DURATION_THRESHOLD_S,
        ))

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None:
            self._attr_available = False
            self.async_write_ha_state()
            return
        addition_w = _read_float(values, CLEAR_TEXT_NAMES["ADDITION_POWER"]) or 0.0
        now = dt_util.utcnow()
        power_threshold = self._power_threshold()
        duration_threshold = self._duration_threshold()
        if addition_w >= power_threshold:
            if self._active_since is None:
                self._active_since = now
            elapsed = (now - self._active_since).total_seconds()
            self._attr_is_on = elapsed >= duration_threshold
        else:
            self._active_since = None
            self._attr_is_on = False
        self._attr_available = True
        self._attr_extra_state_attributes = {
            "addition_power_w": addition_w,
            "active_seconds": (
                (now - self._active_since).total_seconds()
                if self._active_since is not None
                else 0
            ),
            "power_threshold_w": power_threshold,
            "duration_threshold_s": duration_threshold,
        }
        self.async_write_ha_state()


class FilterChangeSoonBinarySensor(_ComfortzoneAlarmBase):
    """Heads-up that the filter is due for replacement within a configurable
    number of days.

    The pump exposes a hard ``filter_alarm`` which only fires once the
    timer has hit zero. This soft warning gives users a chance to order
    a new filter and schedule the swap before being forced into it.
    Default: 7 days, configurable via the options flow.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="filter_change_soon",
            name="Filter change due soon",
            icon="mdi:filter-clock",
        )

    def _threshold_days(self) -> float:
        return float(_option(
            self.entry, CONF_FILTER_WARNING_DAYS, DEFAULT_FILTER_WARNING_DAYS
        ))

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None:
            self._attr_available = False
            self.async_write_ha_state()
            return
        days_left = _read_float(values, "Time to filter change")
        if days_left is None:
            self._attr_available = False
        else:
            threshold = self._threshold_days()
            self._attr_available = True
            self._attr_is_on = days_left <= threshold
            self._attr_extra_state_attributes = {
                "days_remaining": days_left,
                "threshold_days": threshold,
            }
        self.async_write_ha_state()


class LowHotWaterBinarySensor(_ComfortzoneAlarmBase):
    """Warns when the tank temperature is too low for a comfortable shower.

    Trips at the configured threshold and clears once the tank is back
    above ``threshold + hysteresis`` to give a stable signal automations
    can act on (e.g. "if tank is low and grid price is below average,
    kick off a hot-water boost"). Defaults: 40 °C with 3 °C hysteresis,
    both configurable via the options flow.
    """

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="low_hot_water",
            name="Low hot water",
            icon="mdi:water-thermometer",
        )

    def _on_threshold(self) -> float:
        return float(_option(
            self.entry, CONF_LOW_HW_THRESHOLD_C, DEFAULT_LOW_HW_THRESHOLD_C
        ))

    def _hysteresis(self) -> float:
        return float(_option(
            self.entry, CONF_LOW_HW_HYSTERESIS_C, DEFAULT_LOW_HW_HYSTERESIS_C
        ))

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None:
            self._attr_available = False
            self.async_write_ha_state()
            return
        tank_c = _read_float(values, CLEAR_TEXT_NAMES["HOT_WATER_TEMP"])
        if tank_c is None:
            self._attr_available = False
            self.async_write_ha_state()
            return
        on_c = self._on_threshold()
        off_c = on_c + self._hysteresis()
        self._attr_available = True
        if self._attr_is_on:
            if tank_c >= off_c:
                self._attr_is_on = False
        else:
            if tank_c <= on_c:
                self._attr_is_on = True
        self._attr_extra_state_attributes = {
            "tank_temp_c": tank_c,
            "on_threshold_c": on_c,
            "off_threshold_c": off_c,
        }
        self.async_write_ha_state()


class CompressorRunningAtMaxBinarySensor(_ComfortzoneAlarmBase):
    """Trips when the inverter compressor has been running near its
    maximum frequency for a sustained period.

    A clean trigger for automations that need to react when the heat
    pump is out of headroom — e.g. accept that the indoor target won't
    be reached, defer DHW production, or back off the heat curve so the
    pump isn't forced to call on the resistive backup.

    The threshold (default 90 %) and duration (default 5 minutes) are
    configurable via the options flow.
    """

    # This isn't really a "problem" in the alarm sense — it's
    # informational state about the pump's current capacity headroom.
    _attr_device_class = None

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="compressor_running_at_max",
            name="Compressor running at max",
            icon="mdi:gauge-full",
        )
        self._above_threshold_since: Optional[datetime] = None

    def _threshold_pct(self) -> float:
        return float(_option(
            self.entry,
            CONF_MAX_LOAD_THRESHOLD_PCT,
            DEFAULT_MAX_LOAD_THRESHOLD_PCT,
        ))

    def _duration_s(self) -> int:
        return int(_option(
            self.entry,
            CONF_MAX_LOAD_DURATION_S,
            DEFAULT_MAX_LOAD_DURATION_S,
        ))

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None:
            self._attr_available = False
            self.async_write_ha_state()
            return
        freq = _read_float(values, CLEAR_TEXT_NAMES["COMPRESSOR_FREQ"])
        freq_max = _read_float(values, CLEAR_TEXT_NAMES["COMPRESSOR_FREQ_MAX"])
        if freq is None or freq_max is None or freq_max <= 0:
            self._attr_available = False
            self.async_write_ha_state()
            return
        load_pct = (freq / freq_max) * 100
        threshold = self._threshold_pct()
        duration = self._duration_s()
        now = dt_util.utcnow()
        if load_pct >= threshold:
            if self._above_threshold_since is None:
                self._above_threshold_since = now
            elapsed = (now - self._above_threshold_since).total_seconds()
            self._attr_is_on = elapsed >= duration
        else:
            self._above_threshold_since = None
            self._attr_is_on = False
        self._attr_available = True
        self._attr_extra_state_attributes = {
            "load_pct": round(load_pct, 1),
            "threshold_pct": threshold,
            "duration_threshold_s": duration,
            "above_threshold_seconds": (
                (now - self._above_threshold_since).total_seconds()
                if self._above_threshold_since is not None
                else 0
            ),
        }
        self.async_write_ha_state()
