"""Computed/derived sensors for the Comfortzone integration.

These sensors are not direct readings from the API; they combine multiple
raw values (from the coordinator) into more useful information:

- Pump activity status (Heating / Making Hot Water / Idle / Defrosting)
- Estimated electrical power split into total, heating, hot-water and aux
- Cumulative energy (kWh) per mode for the Home Assistant Energy panel
- Optional cumulative cost (currency) per mode using a Nord Pool price entity
- Compressor cycle counter and per-mode runtime
- Defrost detection
- Heating circuit ΔT, tank decay rate, specific heating efficiency
- Instant COP

Compressor electrical input is estimated by interpolating the EN255 spec
curve points from the RX95 datasheet (factor 0.235 at 35°C flow → 0.314 at
50°C flow). Users can override with a fixed factor in the options flow.
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from typing import Any, Deque, Optional, Tuple

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
    RestoreSensor,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import find_value_from_raw_data
from .const import (
    CIRCULATION_PUMP_MAX_W,
    CLEAR_TEXT_NAMES,
    CONF_COMPRESSOR_ELECTRICAL_FACTOR,
    CONF_PRICE_ENTITY,
    CONF_PRICE_IN_ORE,
    COP_SPEC_FACTOR_HIGH,
    COP_SPEC_FACTOR_LOW,
    COP_SPEC_FLOW_HIGH_C,
    COP_SPEC_FLOW_LOW_C,
    DEFAULT_COMPRESSOR_FACTOR,
    FAN_MAX_W,
    MIN_ELECTRICAL_FOR_COP_W,
    STANDBY_W,
)
from .entity import build_device_info, device_unique_id

_LOGGER = logging.getLogger(__name__)


# --- Helpers ---------------------------------------------------------------


def _read_float(values_list: list, clear_text_name: str) -> Optional[float]:
    """Read a numeric value from RawData.Values, returning None if missing/invalid."""
    raw = find_value_from_raw_data(values_list, clear_text_name)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _coordinator_values(coordinator: DataUpdateCoordinator) -> Optional[list]:
    """Return the Values list from the coordinator response or None if unavailable."""
    if not coordinator.last_update_success or not coordinator.data:
        return None
    data_block = coordinator.data.get("Data") or {}
    values = data_block.get("Values")
    return values if isinstance(values, list) else None


def _compressor_factor_from_flow(flow_temp_c: Optional[float]) -> float:
    """Interpolate the thermal-to-electrical factor based on flow temperature.

    Anchored at the two EN255 spec points from the RX95 datasheet:
      35°C flow → factor 0.235 (COP 4.25)
      50°C flow → factor 0.314 (COP 3.18)
    Below 35°C and above 50°C the curve is clamped to the nearest spec point.
    """
    if flow_temp_c is None:
        # Fall back to the high (worst-case) factor when flow unknown
        return COP_SPEC_FACTOR_HIGH
    if flow_temp_c <= COP_SPEC_FLOW_LOW_C:
        return COP_SPEC_FACTOR_LOW
    if flow_temp_c >= COP_SPEC_FLOW_HIGH_C:
        return COP_SPEC_FACTOR_HIGH
    span = COP_SPEC_FLOW_HIGH_C - COP_SPEC_FLOW_LOW_C
    pos = (flow_temp_c - COP_SPEC_FLOW_LOW_C) / span
    return COP_SPEC_FACTOR_LOW + pos * (COP_SPEC_FACTOR_HIGH - COP_SPEC_FACTOR_LOW)


def _compute_compressor_electrical_w(
    values: list, override_factor: float
) -> Optional[float]:
    """Estimate compressor electrical input in W using flow-temp-based COP curve.

    A non-zero override_factor (set via options) bypasses the curve and uses
    that constant factor instead — useful when the user has empirical data.
    """
    thermal = _read_float(values, CLEAR_TEXT_NAMES["COMPRESSOR_POWER"])
    if thermal is None:
        return None
    if override_factor and override_factor > 0:
        return thermal * override_factor
    flow_c = _read_float(values, CLEAR_TEXT_NAMES["FLOW_TEMP"])
    return thermal * _compressor_factor_from_flow(flow_c)


def _compute_circulation_pump_w(values: list) -> float:
    """Estimate circulation pump electrical draw in W from reported speed (%)."""
    pct = _read_float(values, CLEAR_TEXT_NAMES["CIRC_PUMP_SPEED"]) or 0.0
    return (pct / 100.0) * CIRCULATION_PUMP_MAX_W


def _compute_fan_w(values: list) -> float:
    """Estimate fan electrical draw in W from reported speed (%)."""
    pct = _read_float(values, CLEAR_TEXT_NAMES["FAN_SPEED_CURRENT"]) or 0.0
    return (pct / 100.0) * FAN_MAX_W


def _compute_addition_w(values: list) -> float:
    """Read the resistive addition heater power in W (already electrical)."""
    return _read_float(values, CLEAR_TEXT_NAMES["ADDITION_POWER"]) or 0.0


def _compressor_active(values: list) -> bool:
    return find_value_from_raw_data(values, CLEAR_TEXT_NAMES["COMPRESSOR_ACTIVE"]) == "1"


def _heating_valve_open(values: list) -> bool:
    return find_value_from_raw_data(
        values, CLEAR_TEXT_NAMES["EXCHANGE_VALVE_HEATING"]
    ) == "1"


def _hw_valve_open(values: list) -> bool:
    return find_value_from_raw_data(
        values, CLEAR_TEXT_NAMES["EXCHANGE_VALVE_HW"]
    ) == "1"


def _is_heating(values: list) -> bool:
    """True when the pump is dedicated to space heating."""
    return _compressor_active(values) and _heating_valve_open(values)


def _is_hot_water(values: list) -> bool:
    """True when the pump is dedicated to hot water production."""
    return _compressor_active(values) and _hw_valve_open(values)


def _is_defrosting(values: list) -> bool:
    """Heuristic: compressor running but neither valve open ⇒ defrost cycle.

    On the RX95 the exchange valves switch between heating and hot-water
    duty. When the pump enters a defrost / pressure-equalisation cycle both
    valves close while the compressor continues to run.
    """
    return (
        _compressor_active(values)
        and not _heating_valve_open(values)
        and not _hw_valve_open(values)
    )


# --- Base classes ----------------------------------------------------------


class _ComfortzoneComputedBase(CoordinatorEntity, SensorEntity):
    """Common boilerplate for derived/computed sensors."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
        suffix: str,
        name: str,
        icon: Optional[str] = None,
        entity_category: Optional[EntityCategory] = None,
        enabled_by_default: bool = True,
    ) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{device_unique_id(entry)}_{suffix}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_entity_category = entity_category
        self._attr_entity_registry_enabled_default = enabled_by_default
        self._attr_device_info = build_device_info(entry)

    def _compressor_factor_override(self) -> float:
        """Return user-configured fixed factor, or 0 to mean 'use spec curve'."""
        return float(
            self.entry.options.get(
                CONF_COMPRESSOR_ELECTRICAL_FACTOR, DEFAULT_COMPRESSOR_FACTOR
            )
        )


# --- Activity status -------------------------------------------------------


class PumpActivitySensor(_ComfortzoneComputedBase):
    """String sensor describing what the pump is doing right now."""

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator,
            entry,
            suffix="pump_activity_status",
            name="Pump activity status",
            icon="mdi:pump-outline",
        )
        self._attr_native_value: str | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None:
            self._attr_available = False
            self._attr_native_value = None
            self.async_write_ha_state()
            return

        if not _compressor_active(values):
            self._attr_native_value = "Idle"
        elif _is_heating(values):
            self._attr_native_value = "Heating"
        elif _is_hot_water(values):
            self._attr_native_value = "Making Hot Water"
        elif _is_defrosting(values):
            self._attr_native_value = "Defrosting"
        else:
            self._attr_native_value = "Compressor active (unknown mode)"
        self._attr_available = True
        self.async_write_ha_state()


# --- Power sensors ---------------------------------------------------------


class _PowerSensorBase(_ComfortzoneComputedBase):
    """Base class for derived power sensors (W)."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator, entry, suffix, name, icon, enabled_by_default=True):
        super().__init__(
            coordinator, entry, suffix=suffix, name=name, icon=icon,
            enabled_by_default=enabled_by_default,
        )
        self._attr_native_value: float | None = None

    def _compute_w(self, values: list) -> Optional[float]:
        raise NotImplementedError

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None:
            self._attr_available = False
            self._attr_native_value = None
            self.async_write_ha_state()
            return
        new_w = self._compute_w(values)
        self._attr_available = new_w is not None
        self._attr_native_value = round(new_w) if new_w is not None else None
        self.async_write_ha_state()


class TotalElectricalPowerSensor(_PowerSensorBase):
    """Estimated total electrical power consumption of the heat pump (W)."""

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="estimated_total_power",
            name="Estimated total power consumption",
            icon="mdi:meter-electric-outline",
        )

    def _compute_w(self, values):
        compressor_e = _compute_compressor_electrical_w(
            values, self._compressor_factor_override()
        )
        if compressor_e is None:
            return None
        addition = _compute_addition_w(values)
        circ = _compute_circulation_pump_w(values)
        fan = _compute_fan_w(values)
        return compressor_e + addition + circ + fan + STANDBY_W


class AuxPowerSensor(_PowerSensorBase):
    """Estimated auxiliary electrical power (fan + standby) consumption in W."""

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="estimated_aux_power",
            name="Estimated aux power consumption",
            icon="mdi:fan",
            enabled_by_default=False,
        )

    def _compute_w(self, values):
        return _compute_fan_w(values) + STANDBY_W


class HeatingPowerSensor(_PowerSensorBase):
    """Electrical power drawn while operating in space heating mode (W)."""

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="heating_power",
            name="Heating mode power",
            icon="mdi:radiator",
        )

    def _compute_w(self, values):
        if not _is_heating(values):
            return 0.0
        compressor_e = _compute_compressor_electrical_w(
            values, self._compressor_factor_override()
        )
        if compressor_e is None:
            return 0.0
        return (
            compressor_e
            + _compute_addition_w(values)
            + _compute_circulation_pump_w(values)
        )


class HotWaterPowerSensor(_PowerSensorBase):
    """Electrical power drawn while producing domestic hot water (W)."""

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="hot_water_power",
            name="Hot water mode power",
            icon="mdi:water-boiler",
        )

    def _compute_w(self, values):
        if not _is_hot_water(values):
            return 0.0
        compressor_e = _compute_compressor_electrical_w(
            values, self._compressor_factor_override()
        )
        if compressor_e is None:
            return 0.0
        return (
            compressor_e
            + _compute_addition_w(values)
            + _compute_circulation_pump_w(values)
        )


# --- Energy sensors (cumulative kWh, Energy panel compatible) -------------


class _IntegratedEnergySensor(_ComfortzoneComputedBase, RestoreSensor):
    """Cumulative energy in kWh integrated from a per-mode power source."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator, entry, suffix, name, icon):
        super().__init__(coordinator, entry, suffix=suffix, name=name, icon=icon)
        self._accumulated_kwh: float = 0.0
        self._last_sample_time: Optional[datetime] = None
        self._last_power_w: Optional[float] = None
        self._attr_native_value = 0.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                self._accumulated_kwh = float(last.native_value)
                self._attr_native_value = self._accumulated_kwh
            except (TypeError, ValueError):
                self._accumulated_kwh = 0.0

    def _current_power_w(self, values: list) -> float:
        raise NotImplementedError

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None:
            return

        now = dt_util.utcnow()
        new_power = self._current_power_w(values)

        if self._last_sample_time is not None and self._last_power_w is not None:
            dt_seconds = (now - self._last_sample_time).total_seconds()
            if 0 < dt_seconds < 3600:
                avg_w = (self._last_power_w + new_power) / 2.0
                self._accumulated_kwh += (avg_w * dt_seconds) / 3_600_000.0

        self._last_sample_time = now
        self._last_power_w = new_power
        self._attr_native_value = round(self._accumulated_kwh, 6)
        self._attr_available = True
        self.async_write_ha_state()


class HeatingEnergySensor(_IntegratedEnergySensor):
    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="heating_energy",
            name="Heating energy",
            icon="mdi:radiator",
        )

    def _current_power_w(self, values):
        if not _is_heating(values):
            return 0.0
        compressor_e = _compute_compressor_electrical_w(
            values, self._compressor_factor_override()
        ) or 0.0
        return (
            compressor_e
            + _compute_addition_w(values)
            + _compute_circulation_pump_w(values)
        )


class HotWaterEnergySensor(_IntegratedEnergySensor):
    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="hot_water_energy",
            name="Hot water energy",
            icon="mdi:water-boiler",
        )

    def _current_power_w(self, values):
        if not _is_hot_water(values):
            return 0.0
        compressor_e = _compute_compressor_electrical_w(
            values, self._compressor_factor_override()
        ) or 0.0
        return (
            compressor_e
            + _compute_addition_w(values)
            + _compute_circulation_pump_w(values)
        )


class TotalEnergySensor(_IntegratedEnergySensor):
    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="total_energy",
            name="Total electrical energy",
            icon="mdi:meter-electric",
        )

    def _current_power_w(self, values):
        compressor_e = _compute_compressor_electrical_w(
            values, self._compressor_factor_override()
        ) or 0.0
        return (
            compressor_e
            + _compute_addition_w(values)
            + _compute_circulation_pump_w(values)
            + _compute_fan_w(values)
            + STANDBY_W
        )


# --- Cost sensors ----------------------------------------------------------


class _IntegratedCostSensor(_ComfortzoneComputedBase, RestoreSensor):
    """Accumulated cost (currency) for a per-mode power source."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 2
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator, entry, suffix, name, icon):
        super().__init__(
            coordinator, entry,
            suffix=suffix, name=name, icon=icon,
            enabled_by_default=False,
        )
        self._accumulated_cost: float = 0.0
        self._last_sample_time: Optional[datetime] = None
        self._last_power_w: Optional[float] = None
        self._attr_native_value = 0.0
        self._attr_native_unit_of_measurement = "SEK"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                self._accumulated_cost = float(last.native_value)
                self._attr_native_value = self._accumulated_cost
            except (TypeError, ValueError):
                self._accumulated_cost = 0.0

    def _current_power_w(self, values: list) -> float:
        raise NotImplementedError

    def _current_price_per_kwh(self) -> Optional[float]:
        """Return current price in SEK/kWh, or None if not configured."""
        price_entity = self.entry.options.get(CONF_PRICE_ENTITY) or self.entry.data.get(
            CONF_PRICE_ENTITY
        )
        if not price_entity:
            return None
        state = self.hass.states.get(price_entity)
        if state is None or state.state in ("unknown", "unavailable", None):
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        in_ore = self.entry.options.get(CONF_PRICE_IN_ORE)
        if in_ore is None:
            in_ore = self.entry.data.get(CONF_PRICE_IN_ORE, False)
        if in_ore:
            value /= 100.0
        return value

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        price = self._current_price_per_kwh()

        if values is None or price is None:
            self._attr_available = price is not None and self._last_sample_time is not None
            return

        now = dt_util.utcnow()
        new_power = self._current_power_w(values)

        if self._last_sample_time is not None and self._last_power_w is not None:
            dt_seconds = (now - self._last_sample_time).total_seconds()
            if 0 < dt_seconds < 3600:
                avg_w = (self._last_power_w + new_power) / 2.0
                kwh_delta = (avg_w * dt_seconds) / 3_600_000.0
                self._accumulated_cost += kwh_delta * price

        self._last_sample_time = now
        self._last_power_w = new_power
        self._attr_native_value = round(self._accumulated_cost, 4)
        self._attr_available = True
        self.async_write_ha_state()


class HeatingCostSensor(_IntegratedCostSensor):
    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="heating_cost",
            name="Heating cost",
            icon="mdi:cash",
        )

    def _current_power_w(self, values):
        if not _is_heating(values):
            return 0.0
        compressor_e = _compute_compressor_electrical_w(
            values, self._compressor_factor_override()
        ) or 0.0
        return (
            compressor_e
            + _compute_addition_w(values)
            + _compute_circulation_pump_w(values)
        )


class HotWaterCostSensor(_IntegratedCostSensor):
    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="hot_water_cost",
            name="Hot water cost",
            icon="mdi:cash",
        )

    def _current_power_w(self, values):
        if not _is_hot_water(values):
            return 0.0
        compressor_e = _compute_compressor_electrical_w(
            values, self._compressor_factor_override()
        ) or 0.0
        return (
            compressor_e
            + _compute_addition_w(values)
            + _compute_circulation_pump_w(values)
        )


# --- Instant COP -----------------------------------------------------------


class InstantCopSensor(_ComfortzoneComputedBase):
    """Instantaneous coefficient of performance: thermal_out / electrical_in.

    Reports unavailable when estimated electrical input is below
    MIN_ELECTRICAL_FOR_COP_W to avoid noise from idle / standby periods.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2
    _attr_native_unit_of_measurement = None

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="instant_cop",
            name="Instant COP",
            icon="mdi:speedometer",
        )
        self._attr_native_value: float | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None or not _compressor_active(values):
            self._attr_available = False
            self._attr_native_value = None
            self.async_write_ha_state()
            return
        thermal = _read_float(values, CLEAR_TEXT_NAMES["TOTAL_POWER"])
        compressor_e = _compute_compressor_electrical_w(
            values, self._compressor_factor_override()
        )
        addition = _compute_addition_w(values)
        circ = _compute_circulation_pump_w(values)
        electrical_in = (compressor_e or 0.0) + addition + circ
        if thermal is None or electrical_in < MIN_ELECTRICAL_FOR_COP_W:
            self._attr_native_value = None
            self._attr_available = False
        else:
            self._attr_native_value = round(thermal / electrical_in, 2)
            self._attr_available = True
        self.async_write_ha_state()


# --- Cycle counter ---------------------------------------------------------


class _RisingEdgeCounter(_ComfortzoneComputedBase, RestoreSensor):
    """Generic counter that increments on a 0→1 transition of a predicate."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator, entry, suffix, name, icon):
        super().__init__(coordinator, entry, suffix=suffix, name=name, icon=icon)
        self._count: int = 0
        self._last_was_active: bool = False
        self._attr_native_value = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                self._count = int(float(last.native_value))
                self._attr_native_value = self._count
            except (TypeError, ValueError):
                self._count = 0

    def _is_active(self, values: list) -> bool:
        raise NotImplementedError

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None:
            return
        is_active = self._is_active(values)
        if is_active and not self._last_was_active:
            self._count += 1
            self._attr_native_value = self._count
            self.async_write_ha_state()
        self._last_was_active = is_active
        self._attr_available = True


class CompressorCycleCounter(_RisingEdgeCounter):
    """Total number of compressor start cycles since installation."""

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="compressor_cycle_count",
            name="Compressor cycle count",
            icon="mdi:counter",
        )

    def _is_active(self, values):
        return _compressor_active(values)


class DefrostCycleCounter(_RisingEdgeCounter):
    """Total number of detected defrost cycles."""

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="defrost_cycle_count",
            name="Defrost cycle count",
            icon="mdi:snowflake-melt",
        )

    def _is_active(self, values):
        return _is_defrosting(values)


# --- Last defrost duration ------------------------------------------------


class LastDefrostDurationSensor(_ComfortzoneComputedBase, RestoreSensor):
    """Duration of the most recent defrost cycle, in minutes."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 1
    _attr_device_class = SensorDeviceClass.DURATION

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="last_defrost_duration",
            name="Last defrost duration",
            icon="mdi:snowflake-melt",
        )
        self._defrost_started: Optional[datetime] = None
        self._last_was_defrost: bool = False
        self._attr_native_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                self._attr_native_value = float(last.native_value)
            except (TypeError, ValueError):
                self._attr_native_value = None

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None:
            return
        now = dt_util.utcnow()
        is_defrost = _is_defrosting(values)
        if is_defrost and not self._last_was_defrost:
            self._defrost_started = now
        elif not is_defrost and self._last_was_defrost and self._defrost_started:
            duration_min = (now - self._defrost_started).total_seconds() / 60.0
            self._attr_native_value = round(duration_min, 2)
            self._defrost_started = None
            self.async_write_ha_state()
        self._last_was_defrost = is_defrost
        self._attr_available = True


# --- Per-mode runtime ------------------------------------------------------


class _RuntimeAccumulator(_ComfortzoneComputedBase, RestoreSensor):
    """Cumulative time (hours) spent with a predicate active."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator, entry, suffix, name, icon):
        super().__init__(coordinator, entry, suffix=suffix, name=name, icon=icon)
        self._accumulated_hours: float = 0.0
        self._last_sample_time: Optional[datetime] = None
        self._last_was_active: bool = False
        self._attr_native_value = 0.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                self._accumulated_hours = float(last.native_value)
                self._attr_native_value = self._accumulated_hours
            except (TypeError, ValueError):
                self._accumulated_hours = 0.0

    def _is_active(self, values: list) -> bool:
        raise NotImplementedError

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None:
            return
        now = dt_util.utcnow()
        is_active = self._is_active(values)
        if self._last_sample_time and self._last_was_active:
            dt_s = (now - self._last_sample_time).total_seconds()
            if 0 < dt_s < 3600:
                self._accumulated_hours += dt_s / 3600.0
        self._last_sample_time = now
        self._last_was_active = is_active
        self._attr_native_value = round(self._accumulated_hours, 4)
        self._attr_available = True
        self.async_write_ha_state()


class HeatingRuntimeSensor(_RuntimeAccumulator):
    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="heating_runtime",
            name="Heating runtime",
            icon="mdi:radiator",
        )

    def _is_active(self, values):
        return _is_heating(values)


class HotWaterRuntimeSensor(_RuntimeAccumulator):
    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="hot_water_runtime",
            name="Hot water runtime",
            icon="mdi:water-boiler",
        )

    def _is_active(self, values):
        return _is_hot_water(values)


# --- Heating circuit ΔT ----------------------------------------------------


class HeatingCircuitDeltaTSensor(_ComfortzoneComputedBase):
    """Flow minus return temperature on the space-heating loop (°C).

    Healthy delta is roughly 3-7 °C while heating; values < 2 °C suggest
    excessive circulation, > 8 °C suggests a clogged filter or air pocket.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="heating_circuit_delta_t",
            name="Heating circuit ΔT",
            icon="mdi:thermometer-chevron-up",
        )
        self._attr_native_value: float | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None:
            self._attr_available = False
            self._attr_native_value = None
            self.async_write_ha_state()
            return
        flow = _read_float(values, CLEAR_TEXT_NAMES["FLOW_TEMP"])
        ret = _read_float(values, CLEAR_TEXT_NAMES["RETURN_TEMP"])
        if flow is None or ret is None:
            self._attr_available = False
            self._attr_native_value = None
        else:
            self._attr_native_value = round(flow - ret, 2)
            self._attr_available = True
        self.async_write_ha_state()


# --- Tank decay rate -------------------------------------------------------


class TankDecayRateSensor(_ComfortzoneComputedBase):
    """How fast the hot water tank loses heat (°C / hour) while idle.

    Tracks samples of (timestamp, hot_water_temp) over a short window and
    computes the slope only when the pump is **not** producing hot water.
    Useful as a real-world proxy for tank standing losses and a heuristic
    for shower detection (a sharp drop = water being drawn).
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "°C/h"
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:water-thermometer"

    WINDOW_SECONDS = 30 * 60  # 30 minutes of history

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="tank_decay_rate",
            name="Tank decay rate",
            icon="mdi:water-thermometer",
            enabled_by_default=False,
        )
        self._samples: Deque[Tuple[datetime, float]] = deque()
        self._attr_native_value: float | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None:
            return
        now = dt_util.utcnow()
        hw_temp = _read_float(values, CLEAR_TEXT_NAMES["HOT_WATER_TEMP"])
        if hw_temp is None:
            return

        # Append and drop entries older than the window
        self._samples.append((now, hw_temp))
        cutoff = now.timestamp() - self.WINDOW_SECONDS
        while self._samples and self._samples[0][0].timestamp() < cutoff:
            self._samples.popleft()

        # Only compute decay when *not* actively producing hot water
        if _is_hot_water(values):
            return

        if len(self._samples) < 2:
            return
        first_t, first_v = self._samples[0]
        delta_h = (now - first_t).total_seconds() / 3600.0
        if delta_h <= 0.05:
            return  # not enough span yet
        rate = (hw_temp - first_v) / delta_h  # negative = cooling
        self._attr_native_value = round(rate, 3)
        self._attr_available = True
        self.async_write_ha_state()


# --- Specific heating energy ----------------------------------------------


class SpecificHeatingEnergySensor(_ComfortzoneComputedBase, RestoreSensor):
    """Estimate of how many kWh are required per °C indoor temperature rise.

    Only updates while the pump is in heating mode. Uses an exponential
    moving average to smooth the noisy ratio of (energy delta / indoor
    delta) over short observation windows.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kWh/°C"
    _attr_suggested_display_precision = 2
    EMA_ALPHA = 0.15

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="specific_heating_energy",
            name="Specific heating energy",
            icon="mdi:home-thermometer",
            enabled_by_default=False,
        )
        self._anchor_indoor: Optional[float] = None
        self._anchor_kwh: float = 0.0
        self._kwh_total: float = 0.0
        self._last_sample_time: Optional[datetime] = None
        self._last_power_w: Optional[float] = None
        self._ema: Optional[float] = None
        self._attr_native_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                self._ema = float(last.native_value)
                self._attr_native_value = self._ema
            except (TypeError, ValueError):
                self._ema = None

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None:
            return
        now = dt_util.utcnow()
        indoor = _read_float(values, CLEAR_TEXT_NAMES["INDOOR_TEMP"])
        if indoor is None:
            return

        # Maintain a running heating-energy total (mirrors the global energy sensor)
        if _is_heating(values):
            compressor_e = _compute_compressor_electrical_w(
                values, self._compressor_factor_override()
            ) or 0.0
            new_power = (
                compressor_e
                + _compute_addition_w(values)
                + _compute_circulation_pump_w(values)
            )
        else:
            new_power = 0.0

        if self._last_sample_time and self._last_power_w is not None:
            dt_s = (now - self._last_sample_time).total_seconds()
            if 0 < dt_s < 3600:
                avg_w = (self._last_power_w + new_power) / 2.0
                self._kwh_total += (avg_w * dt_s) / 3_600_000.0
        self._last_sample_time = now
        self._last_power_w = new_power

        if not _is_heating(values):
            # Pump idle / making HW: anchor needs reset before next heating window
            self._anchor_indoor = None
            return

        if self._anchor_indoor is None:
            self._anchor_indoor = indoor
            self._anchor_kwh = self._kwh_total
            return

        delta_t = indoor - self._anchor_indoor
        delta_kwh = self._kwh_total - self._anchor_kwh
        if delta_t >= 0.3 and delta_kwh > 0.05:
            ratio = delta_kwh / delta_t
            self._ema = (
                ratio if self._ema is None else self.EMA_ALPHA * ratio + (1 - self.EMA_ALPHA) * self._ema
            )
            self._attr_native_value = round(self._ema, 3)
            self._attr_available = True
            self.async_write_ha_state()
            # Slide the anchor forward so we keep measuring fresh windows
            self._anchor_indoor = indoor
            self._anchor_kwh = self._kwh_total


# --- Reduced fan diagnostics ----------------------------------------------


class ReducedFanScheduleSensor(_ComfortzoneComputedBase):
    """Diagnostic sensor showing the configured night/quiet fan schedule."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry, scope: str):
        """scope is either 'weekdays' or 'weekends'."""
        suffix = f"reduced_fan_{scope}_schedule"
        super().__init__(
            coordinator, entry,
            suffix=suffix,
            name=f"Reduced fan {scope} schedule",
            icon="mdi:fan-clock",
            enabled_by_default=False,
        )
        self._scope = scope
        self._attr_native_value: str | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None:
            self._attr_available = False
            self._attr_native_value = None
            self.async_write_ha_state()
            return
        prefix = "WEEKDAYS" if self._scope == "weekdays" else "WEEKENDS"
        start_h = find_value_from_raw_data(
            values, CLEAR_TEXT_NAMES[f"REDUCED_FAN_{prefix}_START_H"]
        )
        start_m = find_value_from_raw_data(
            values, CLEAR_TEXT_NAMES[f"REDUCED_FAN_{prefix}_START_M"]
        )
        stop_h = find_value_from_raw_data(
            values, CLEAR_TEXT_NAMES[f"REDUCED_FAN_{prefix}_STOP_H"]
        )
        stop_m = find_value_from_raw_data(
            values, CLEAR_TEXT_NAMES[f"REDUCED_FAN_{prefix}_STOP_M"]
        )
        if all(v is not None for v in (start_h, start_m, stop_h, stop_m)):
            try:
                self._attr_native_value = (
                    f"{int(start_h):02d}:{int(start_m):02d}-"
                    f"{int(stop_h):02d}:{int(stop_m):02d}"
                )
                self._attr_available = True
            except (TypeError, ValueError):
                self._attr_native_value = None
                self._attr_available = False
        else:
            self._attr_native_value = None
            self._attr_available = False
        self.async_write_ha_state()


# --- Entry point -----------------------------------------------------------


def build_computed_sensors(
    coordinator: DataUpdateCoordinator, entry: ConfigEntry
) -> list[SensorEntity]:
    """Return all computed/derived sensors for this config entry."""
    return [
        # Status / activity
        PumpActivitySensor(coordinator, entry),
        # Power
        TotalElectricalPowerSensor(coordinator, entry),
        AuxPowerSensor(coordinator, entry),
        HeatingPowerSensor(coordinator, entry),
        HotWaterPowerSensor(coordinator, entry),
        # Energy (kWh, Energy panel ready)
        HeatingEnergySensor(coordinator, entry),
        HotWaterEnergySensor(coordinator, entry),
        TotalEnergySensor(coordinator, entry),
        # Cost (currency)
        HeatingCostSensor(coordinator, entry),
        HotWaterCostSensor(coordinator, entry),
        # Performance / efficiency
        InstantCopSensor(coordinator, entry),
        # Wear / cycling
        CompressorCycleCounter(coordinator, entry),
        DefrostCycleCounter(coordinator, entry),
        LastDefrostDurationSensor(coordinator, entry),
        # Per-mode runtime
        HeatingRuntimeSensor(coordinator, entry),
        HotWaterRuntimeSensor(coordinator, entry),
        # Diagnostics on the heating loop
        HeatingCircuitDeltaTSensor(coordinator, entry),
        # Tank dynamics & house thermal performance
        TankDecayRateSensor(coordinator, entry),
        SpecificHeatingEnergySensor(coordinator, entry),
        # Diagnostics: night fan schedule
        ReducedFanScheduleSensor(coordinator, entry, "weekdays"),
        ReducedFanScheduleSensor(coordinator, entry, "weekends"),
    ]
