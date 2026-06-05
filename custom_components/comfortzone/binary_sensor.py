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
    CONF_LARGE_DRAW_THRESHOLD_C,
    CONF_MAX_LOAD_DURATION_S,
    CONF_MAX_LOAD_THRESHOLD_PCT,
    CONF_SHORT_CYCLE_THRESHOLD,
    DEFAULT_ADDITION_DURATION_THRESHOLD_S,
    DEFAULT_ADDITION_POWER_THRESHOLD_W,
    DEFAULT_FILTER_WARNING_DAYS,
    DEFAULT_LARGE_DRAW_THRESHOLD_C,
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
    entities.append(LargeHotWaterDrawBinarySensor(coordinator, entry))
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


class LargeHotWaterDrawBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Heuristic binary sensor: True when a large hot-water draw is underway.

    *Renamed from ``shower_in_progress`` in 2.11.0.* Real-world data showed
    the tank temperature simply cannot tell a shower apart from a bath or a
    big dishwashing run — and worse, the largest drops in the data were
    **not** showers while the actual showers left almost no trace. So this
    sensor is now honest about what it measures: a sizeable draw of hot
    water, whatever the cause.

    How it works — a "missing heat" accumulator (°C):

    Every poll we compare the tank's **actual** temperature change against
    the change we'd **expect** from the pump's current mode:

    * Idle / heating: the tank only loses standing-loss heat
      (~0.01 °C/min). Any faster fall is water leaving the tank.
    * Making hot water: the tank rises at ~0.15 °C/min on RX95 (this was
      mis-set to 0.40 before 2.11.0, which made every production cycle look
      like a draw and produced constant false positives). A flat or falling
      tank *during* production means a draw is beating the pump.

    The per-interval shortfall ``expected − actual`` (°C/min) is integrated
    over time into an accumulator. The sensor turns **on** once the
    accumulator passes ``large_draw_threshold_c`` and **off** once the tank
    recovers and the accumulator decays back down (hysteresis).

    Two guards keep it honest:

    * **Artifact rejection.** When the pump switches to hot-water mode the
      ``Hot water temp`` reading can plunge tens of degrees in a couple of
      minutes because the exchange valve repoints the sensor onto the cold
      loop — physically impossible for a real tap. Any drop faster than
      ``ARTIFACT_MAX_DROP_C_PER_MIN`` is treated as a sensor artifact and
      excluded from the accumulator.
    * **Small draws fade out.** Brief draws (a hand-wash, a night toilet
      fill) never build enough deficit to cross the threshold, so they're
      ignored by design.

    Known blind spot: a shower taken while the pump is *already* producing
    into a still-hot tank leaves no signal here at all — the pump masks it.
    The ``hot_water_cycle_duration`` sensor catches those after the fact.
    """

    _attr_has_entity_name = True
    _attr_name = "Large hot water draw"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:water-pump"

    # Expected tank-temperature slope in °C/min by pump mode. Negative =
    # cooling. Recalibrated against five observed RX95 production cycles
    # (all ~0.15-0.17 °C/min) and overnight standing losses (~0.2-0.6 °C/h).
    EXPECTED_SLOPE_IDLE = -0.01
    EXPECTED_SLOPE_HW_PRODUCTION = 0.15

    # Drops faster than this (°C/min) are physically impossible for a tap on
    # this tank and are treated as the valve/sensor repointing artifact.
    # Real draws observed in the field top out around 0.5 °C/min.
    ARTIFACT_MAX_DROP_C_PER_MIN = 1.0

    # The accumulator decays back to zero once the tank recovers; the sensor
    # clears when it falls to this fraction of the on-threshold (hysteresis).
    CLEAR_FRACTION = 0.33

    # Cap the accumulator so a long event doesn't take forever to clear.
    MAX_ACCUMULATOR_C = 12.0

    # Ignore implausibly long gaps between polls (restart, API outage).
    MAX_GAP_SECONDS = 600

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{device_unique_id(entry)}_large_hot_water_draw"
        self._attr_device_info = build_device_info(entry)
        self._prev_t: Optional[datetime] = None
        self._prev_temp: Optional[float] = None
        self._deficit_c: float = 0.0
        self._attr_is_on = False
        self._attr_extra_state_attributes = {}

    def _threshold(self) -> float:
        return float(_option(
            self.entry, CONF_LARGE_DRAW_THRESHOLD_C, DEFAULT_LARGE_DRAW_THRESHOLD_C
        ))

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

        pump_making_hw = _is_hot_water(values)
        expected_slope = (
            self.EXPECTED_SLOPE_HW_PRODUCTION
            if pump_making_hw
            else self.EXPECTED_SLOPE_IDLE
        )

        inst_slope: Optional[float] = None
        artifact = False
        if self._prev_t is not None and self._prev_temp is not None:
            dt_min = (now - self._prev_t).total_seconds() / 60.0
            if 0 < dt_min <= self.MAX_GAP_SECONDS / 60.0:
                inst_slope = (hw_temp - self._prev_temp) / dt_min
                if inst_slope <= -self.ARTIFACT_MAX_DROP_C_PER_MIN:
                    # Sensor/valve repointing — do not count toward the draw.
                    artifact = True
                else:
                    shortfall = expected_slope - inst_slope  # °C/min below expected
                    self._deficit_c += shortfall * dt_min
                    self._deficit_c = max(
                        0.0, min(self.MAX_ACCUMULATOR_C, self._deficit_c)
                    )

        self._prev_t = now
        self._prev_temp = hw_temp

        threshold = self._threshold()
        clear_at = threshold * self.CLEAR_FRACTION
        if self._attr_is_on:
            if self._deficit_c <= clear_at:
                self._attr_is_on = False
        else:
            if self._deficit_c >= threshold:
                self._attr_is_on = True

        self._attr_available = True
        self._attr_extra_state_attributes = {
            "hot_water_temp": hw_temp,
            "slope_c_per_min": (
                round(inst_slope, 3) if inst_slope is not None else None
            ),
            "expected_slope_c_per_min": expected_slope,
            "accumulated_deficit_c": round(self._deficit_c, 2),
            "threshold_c": threshold,
            "clear_threshold_c": round(clear_at, 2),
            "artifact_rejected": artifact,
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
