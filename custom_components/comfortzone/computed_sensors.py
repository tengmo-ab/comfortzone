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

from .calculations import (
    compressor_active as _compressor_active,
    compute_addition_w as _compute_addition_w,
    compute_circulation_pump_w as _compute_circulation_pump_w,
    compute_compressor_electrical_w as _compute_compressor_electrical_w,
    compute_fan_w as _compute_fan_w,
    find_value_from_raw_data,
    is_defrosting as _is_defrosting,
    is_heating as _is_heating,
    is_hot_water as _is_hot_water,
    read_float as _read_float,
)
from .const import (
    CLEAR_TEXT_NAMES,
    CONF_COMPRESSOR_ELECTRICAL_FACTOR,
    CONF_LONG_HW_CYCLE_MIN,
    CONF_MODEL,
    CONF_PRICE_ENTITY,
    CONF_PRICE_IN_ORE,
    DEFAULT_COMPRESSOR_FACTOR,
    DEFAULT_LONG_HW_CYCLE_MIN,
    MIN_ELECTRICAL_FOR_COP_W,
    STANDBY_W,
)
from .entity import build_device_info, device_unique_id

_LOGGER = logging.getLogger(__name__)


# --- Helpers ---------------------------------------------------------------
# Most pure helpers live in calculations.py and are imported above with
# underscore aliases so the rest of this file keeps its existing names.


def _coordinator_values(coordinator: DataUpdateCoordinator) -> Optional[list]:
    """Return the Values list from the coordinator response or None if unavailable."""
    if not coordinator.last_update_success or not coordinator.data:
        return None
    data_block = coordinator.data.get("Data") or {}
    values = data_block.get("Values")
    return values if isinstance(values, list) else None


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

    def _model(self) -> str:
        """Return the configured pump model (defaults to RX95)."""
        return str(self.entry.data.get(CONF_MODEL, "RX95"))


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
            values, self._compressor_factor_override(), self._model()
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
            values, self._compressor_factor_override(), self._model()
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
            values, self._compressor_factor_override(), self._model()
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
            values, self._compressor_factor_override(), self._model()
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
            values, self._compressor_factor_override(), self._model()
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
            values, self._compressor_factor_override(), self._model()
        ) or 0.0
        return (
            compressor_e
            + _compute_addition_w(values)
            + _compute_circulation_pump_w(values)
            + _compute_fan_w(values)
            + STANDBY_W
        )


class AdditionEnergySensor(_IntegratedEnergySensor):
    """Cumulative kWh consumed by the resistive addition heater.

    Integrates the ``Addition effect`` reading (which is already in
    electrical watts) over time so the Home Assistant Energy panel can
    track exactly how much COP-1 electricity has been used. Pairs
    naturally with ``addition_heater_runtime`` (hours) and the
    ``addition_heater_active`` alarm.
    """

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="addition_heater_energy",
            name="Addition heater energy",
            icon="mdi:heating-coil",
        )

    def _current_power_w(self, values):
        # Addition power is already electrical W (not thermal) so it is
        # used directly without going through the compressor COP curve.
        return _compute_addition_w(values)


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
            values, self._compressor_factor_override(), self._model()
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
            values, self._compressor_factor_override(), self._model()
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
            values, self._compressor_factor_override(), self._model()
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


class AdditionRuntimeSensor(_RuntimeAccumulator):
    """Cumulative hours the resistive addition heater (`elpatron`) has been
    drawing meaningful power.

    Tracks any sample where ``Addition effect`` exceeds 100 W. Useful for
    spotting periods when the heat pump alone wasn't enough and the COP-1
    backup kicked in — every hour logged here is roughly 6 kWh of
    expensive electricity.
    """

    ADDITION_THRESHOLD_W = 100.0

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="addition_heater_runtime",
            name="Addition heater runtime",
            icon="mdi:heating-coil",
        )

    def _is_active(self, values):
        return _compute_addition_w(values) >= self.ADDITION_THRESHOLD_W


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
    excessive circulation flow, > 8 °C suggests a clogged filter or air
    pocket.

    The sensor only updates while the heat pump is actually heating —
    while making hot water or idle the same TE1/TE2 sensors are reading
    a stale or reverse-direction loop, so we hold the last measured value
    until heating resumes (rather than reporting nonsense like -30 °C).
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
            self.async_write_ha_state()
            return
        # Only meaningful while heating — otherwise the loop is parked or
        # being driven in the opposite direction by the exchange valve.
        if not _is_heating(values):
            self._attr_available = self._attr_native_value is not None
            self.async_write_ha_state()
            return
        flow = _read_float(values, CLEAR_TEXT_NAMES["FLOW_TEMP"])
        ret = _read_float(values, CLEAR_TEXT_NAMES["RETURN_TEMP"])
        if flow is None or ret is None:
            return  # keep the last reading
        self._attr_native_value = round(flow - ret, 2)
        self._attr_available = True
        self.async_write_ha_state()


class HotWaterLoopDeltaTSensor(_ComfortzoneComputedBase):
    """Absolute temperature differential across the heat exchanger while
    making hot water (°C).

    During domestic hot water production the same TE1/TE2 sensors read in
    the opposite direction relative to space heating, so the absolute
    difference is the meaningful number. Values around 25-40 °C are
    healthy on an RX95 — the compressor is lifting tank water from ~30 °C
    to ~60 °C in a single pass.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="hot_water_loop_delta_t",
            name="Hot water loop ΔT",
            icon="mdi:thermometer-water",
        )
        self._attr_native_value: float | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None:
            self._attr_available = False
            self.async_write_ha_state()
            return
        if not _is_hot_water(values):
            # Hold last value while pump is doing something else.
            self._attr_available = self._attr_native_value is not None
            self.async_write_ha_state()
            return
        flow = _read_float(values, CLEAR_TEXT_NAMES["FLOW_TEMP"])
        ret = _read_float(values, CLEAR_TEXT_NAMES["RETURN_TEMP"])
        if flow is None or ret is None:
            return
        self._attr_native_value = round(abs(flow - ret), 2)
        self._attr_available = True
        self.async_write_ha_state()
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


class TankHeatingRateSensor(_ComfortzoneComputedBase):
    """How fast the hot water tank gains heat (°C / hour) while the pump
    is actively producing hot water.

    Mirror image of ``TankDecayRateSensor``. Useful for tracking tank
    coil heat-transfer effectiveness over time — a drop here at constant
    compressor load is a good early indicator of limescale or fouling.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "°C/h"
    _attr_suggested_display_precision = 2

    WINDOW_SECONDS = 15 * 60  # shorter window — HW production cycles are short

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="tank_heating_rate",
            name="Tank heating rate",
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

        # Reset samples whenever HW production stops, so the next cycle
        # starts with a clean baseline rather than averaging across an idle gap.
        if not _is_hot_water(values):
            self._samples.clear()
            return

        self._samples.append((now, hw_temp))
        cutoff = now.timestamp() - self.WINDOW_SECONDS
        while self._samples and self._samples[0][0].timestamp() < cutoff:
            self._samples.popleft()

        if len(self._samples) < 2:
            return
        first_t, first_v = self._samples[0]
        delta_h = (now - first_t).total_seconds() / 3600.0
        if delta_h <= 0.05:
            return
        rate = (hw_temp - first_v) / delta_h
        self._attr_native_value = round(rate, 2)
        self._attr_available = True
        self.async_write_ha_state()


class HotWaterCycleDurationSensor(_ComfortzoneComputedBase, RestoreSensor):
    """Duration (minutes) of the most recently completed hot-water cycle.

    A cycle is the span from the pump starting hot-water production until it
    stops. The value updates when a cycle ends and stays put until the next
    one finishes.

    This is the cycle-length heuristic for catching showers the tank
    temperature can't see: a draw taken *while* the pump is already
    producing doesn't make the tank fall (the pump keeps pace), but it does
    force the pump to run longer to refill. A cycle markedly longer than the
    rolling baseline therefore hints that someone drew hot water mid-cycle.

    Attributes expose the rolling baseline (median of recent cycles) and an
    ``unusually_long`` flag — true when the last cycle exceeded both the
    configured absolute floor (``long_hw_cycle_min``) and ~1.5× the rolling
    median. It is a hint, not proof: a deeply discharged tank also produces
    a long cycle without any mid-cycle draw.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_suggested_display_precision = 1

    # How many recent completed cycles to keep for the rolling baseline.
    BASELINE_CYCLES = 10
    # Last cycle must exceed this multiple of the median to count as long.
    LONG_MEDIAN_FACTOR = 1.5
    # Ignore absurd cycle lengths (stuck state, restart) above this.
    MAX_PLAUSIBLE_CYCLE_MIN = 600.0

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="hot_water_cycle_duration",
            name="Hot water cycle duration",
            icon="mdi:timer-sand",
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._cycle_start: Optional[datetime] = None
        self._last_was_hw: bool = False
        self._recent: Deque[float] = deque(maxlen=self.BASELINE_CYCLES)
        self._attr_native_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                self._attr_native_value = float(last.native_value)
            except (TypeError, ValueError):
                self._attr_native_value = None

    def _long_floor_min(self) -> float:
        return float(
            self.entry.options.get(
                CONF_LONG_HW_CYCLE_MIN, DEFAULT_LONG_HW_CYCLE_MIN
            )
        )

    @staticmethod
    def _median(values) -> Optional[float]:
        ordered = sorted(values)
        n = len(ordered)
        if n == 0:
            return None
        mid = n // 2
        if n % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2.0

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None:
            return
        now = dt_util.utcnow()
        is_hw = _is_hot_water(values)

        if is_hw and not self._last_was_hw:
            # Rising edge — a new production cycle begins.
            self._cycle_start = now
        elif not is_hw and self._last_was_hw and self._cycle_start is not None:
            # Falling edge — cycle just finished.
            duration_min = (now - self._cycle_start).total_seconds() / 60.0
            self._cycle_start = None
            if 0 < duration_min <= self.MAX_PLAUSIBLE_CYCLE_MIN:
                median_before = self._median(self._recent)
                floor_min = self._long_floor_min()
                unusually_long = duration_min >= floor_min and (
                    median_before is None
                    or duration_min >= median_before * self.LONG_MEDIAN_FACTOR
                )
                self._recent.append(duration_min)
                self._attr_native_value = round(duration_min, 1)
                self._attr_extra_state_attributes = {
                    "unusually_long": unusually_long,
                    "baseline_median_min": (
                        round(self._median(self._recent), 1)
                        if self._recent
                        else None
                    ),
                    "long_floor_min": floor_min,
                    "cycles_sampled": len(self._recent),
                }
                self.async_write_ha_state()

        self._last_was_hw = is_hw
        self._attr_available = True


class DhwProductionRateSensor(_ComfortzoneComputedBase):
    """Thermal output power averaged over a short window while making
    hot water (kW).

    Proxies the heat pump's effective HW production capacity right now.
    Drops over time on a fixed compressor load suggest fouling /
    limescale on the tank coil.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_suggested_display_precision = 2

    WINDOW_SECONDS = 5 * 60

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="dhw_production_rate",
            name="DHW production rate",
            icon="mdi:water-boiler",
            enabled_by_default=False,
        )
        self._samples: Deque[Tuple[datetime, float]] = deque()
        self._attr_native_value: float | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None:
            return
        if not _is_hot_water(values):
            self._samples.clear()
            return
        thermal_w = _read_float(values, CLEAR_TEXT_NAMES["TOTAL_POWER"])
        if thermal_w is None:
            return
        now = dt_util.utcnow()
        self._samples.append((now, thermal_w))
        cutoff = now.timestamp() - self.WINDOW_SECONDS
        while self._samples and self._samples[0][0].timestamp() < cutoff:
            self._samples.popleft()
        if not self._samples:
            return
        avg_w = sum(v for _, v in self._samples) / len(self._samples)
        self._attr_native_value = round(avg_w / 1000.0, 3)
        self._attr_available = True
        self.async_write_ha_state()


class CompressorLoadPercentageSensor(_ComfortzoneComputedBase):
    """Compressor load as a percentage of its inverter maximum frequency.

    Computed live from ``Compressor frequency`` divided by
    ``Compressor freq. max``. 0 % means idle, 100 % means the inverter
    has no headroom left. A controller can use this to decide whether
    raising the heat curve is even possible right now — if the
    compressor is already at 100 % and indoor temp is below target,
    the only remaining option is the resistive backup.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="compressor_load_percentage",
            name="Compressor load",
            icon="mdi:speedometer",
        )
        self._attr_native_value: float | None = None

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
            self._attr_native_value = None
        else:
            self._attr_native_value = round((freq / freq_max) * 100, 1)
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
                values, self._compressor_factor_override(), self._model()
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
        AdditionEnergySensor(coordinator, entry),
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
        AdditionRuntimeSensor(coordinator, entry),
        # Diagnostics on the heating and hot-water loops
        HeatingCircuitDeltaTSensor(coordinator, entry),
        HotWaterLoopDeltaTSensor(coordinator, entry),
        # Tank dynamics & house thermal performance
        TankDecayRateSensor(coordinator, entry),
        TankHeatingRateSensor(coordinator, entry),
        HotWaterCycleDurationSensor(coordinator, entry),
        DhwProductionRateSensor(coordinator, entry),
        CompressorLoadPercentageSensor(coordinator, entry),
        SpecificHeatingEnergySensor(coordinator, entry),
        # Diagnostics: night fan schedule
        ReducedFanScheduleSensor(coordinator, entry, "weekdays"),
        ReducedFanScheduleSensor(coordinator, entry, "weekends"),
    ]
