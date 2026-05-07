"""Computed/derived sensors for the Comfortzone integration.

These sensors are not direct readings from the API; they combine multiple
raw values (from the coordinator) into more useful information:

- Pump activity status (Heating / Making Hot Water / Idle / Defrosting)
- Estimated electrical power split into total, heating, hot-water and aux
- Cumulative energy (kWh) per mode for the Home Assistant Energy panel
- Optional cumulative cost (currency) per mode using a Nord Pool price entity

The numbers depend on a tunable conversion factor (compressor thermal -> electrical)
and on the user supplying a price entity in the options flow when cost is desired.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

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
    DEFAULT_COMPRESSOR_FACTOR,
    FAN_MAX_W,
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


def _compute_compressor_electrical_w(
    values: list, compressor_factor: float
) -> Optional[float]:
    """Estimate compressor electrical input in W based on reported thermal output."""
    thermal = _read_float(values, CLEAR_TEXT_NAMES["COMPRESSOR_POWER"])
    if thermal is None:
        return None
    return thermal * compressor_factor


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


def _is_heating(values: list) -> bool:
    """Return True if the heat pump is currently directed at space heating."""
    compressor = find_value_from_raw_data(values, CLEAR_TEXT_NAMES["COMPRESSOR_ACTIVE"])
    valve = find_value_from_raw_data(values, CLEAR_TEXT_NAMES["EXCHANGE_VALVE_HEATING"])
    return compressor == "1" and valve == "1"


def _is_hot_water(values: list) -> bool:
    """Return True if the heat pump is currently directed at hot water production."""
    compressor = find_value_from_raw_data(values, CLEAR_TEXT_NAMES["COMPRESSOR_ACTIVE"])
    valve = find_value_from_raw_data(values, CLEAR_TEXT_NAMES["EXCHANGE_VALVE_HW"])
    return compressor == "1" and valve == "1"


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

    def _compressor_factor(self) -> float:
        """Return the user-configured (or default) compressor electrical factor."""
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

        compressor = find_value_from_raw_data(values, CLEAR_TEXT_NAMES["COMPRESSOR_ACTIVE"])
        if compressor != "1":
            self._attr_native_value = "Idle"
        elif _is_heating(values):
            self._attr_native_value = "Heating"
        elif _is_hot_water(values):
            self._attr_native_value = "Making Hot Water"
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
        compressor_e = _compute_compressor_electrical_w(values, self._compressor_factor())
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
        compressor_e = _compute_compressor_electrical_w(values, self._compressor_factor())
        if compressor_e is None:
            return 0.0
        addition = _compute_addition_w(values)
        circ = _compute_circulation_pump_w(values)
        return compressor_e + addition + circ


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
        compressor_e = _compute_compressor_electrical_w(values, self._compressor_factor())
        if compressor_e is None:
            return 0.0
        addition = _compute_addition_w(values)
        circ = _compute_circulation_pump_w(values)
        return compressor_e + addition + circ


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
        """Return the instantaneous power for this energy bucket."""
        raise NotImplementedError

    @callback
    def _handle_coordinator_update(self) -> None:
        values = _coordinator_values(self.coordinator)
        if values is None:
            # Don't mark unavailable: keep last accumulated total visible.
            return

        now = dt_util.utcnow()
        new_power = self._current_power_w(values)

        if self._last_sample_time is not None and self._last_power_w is not None:
            dt_seconds = (now - self._last_sample_time).total_seconds()
            if 0 < dt_seconds < 3600:  # ignore impossibly long gaps
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
        compressor_e = _compute_compressor_electrical_w(values, self._compressor_factor()) or 0.0
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
        compressor_e = _compute_compressor_electrical_w(values, self._compressor_factor()) or 0.0
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
        compressor_e = _compute_compressor_electrical_w(values, self._compressor_factor()) or 0.0
        return (
            compressor_e
            + _compute_addition_w(values)
            + _compute_circulation_pump_w(values)
            + _compute_fan_w(values)
            + STANDBY_W
        )


# --- Cost sensors ----------------------------------------------------------


class _IntegratedCostSensor(_ComfortzoneComputedBase, RestoreSensor):
    """Accumulated cost (currency) for a per-mode power source.

    Reads the current spot price from a user-configured price entity in the
    options flow. If no price entity is configured the sensor stays unavailable.
    """

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 2
    # Currency device-class lets HA pick the user's currency unit automatically.
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
        # Set from the user's HA config or default to SEK; HA picks currency.
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
        """Return current price in SEK per kWh, or None if not configured/available."""
        price_entity = self.entry.options.get(CONF_PRICE_ENTITY)
        if not price_entity:
            return None
        state = self.hass.states.get(price_entity)
        if state is None or state.state in ("unknown", "unavailable", None):
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        # If the price is reported in öre (default for Nord Pool helpers in Sweden),
        # divide by 100 to convert to SEK.
        if self.entry.options.get(CONF_PRICE_IN_ORE, True):
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
        compressor_e = _compute_compressor_electrical_w(values, self._compressor_factor()) or 0.0
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
        compressor_e = _compute_compressor_electrical_w(values, self._compressor_factor()) or 0.0
        return (
            compressor_e
            + _compute_addition_w(values)
            + _compute_circulation_pump_w(values)
        )


# --- COP (instantaneous) ---------------------------------------------------


class InstantCopSensor(_ComfortzoneComputedBase):
    """Instantaneous coefficient of performance: thermal_out / electrical_in."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2
    _attr_native_unit_of_measurement = None

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry,
            suffix="instant_cop",
            name="Instant COP",
            icon="mdi:speedometer",
            enabled_by_default=False,
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
        thermal = _read_float(values, CLEAR_TEXT_NAMES["TOTAL_POWER"])
        compressor_e = _compute_compressor_electrical_w(values, self._compressor_factor())
        addition = _compute_addition_w(values)
        circ = _compute_circulation_pump_w(values)
        electrical_in = (compressor_e or 0.0) + addition + circ
        if thermal is None or electrical_in <= 0:
            self._attr_native_value = None
            self._attr_available = False
        else:
            self._attr_native_value = round(thermal / electrical_in, 2)
            self._attr_available = True
        self.async_write_ha_state()


# --- Entry point -----------------------------------------------------------


def build_computed_sensors(
    coordinator: DataUpdateCoordinator, entry: ConfigEntry
) -> list[SensorEntity]:
    """Return all computed/derived sensors for this config entry."""
    return [
        PumpActivitySensor(coordinator, entry),
        TotalElectricalPowerSensor(coordinator, entry),
        AuxPowerSensor(coordinator, entry),
        HeatingPowerSensor(coordinator, entry),
        HotWaterPowerSensor(coordinator, entry),
        HeatingEnergySensor(coordinator, entry),
        HotWaterEnergySensor(coordinator, entry),
        TotalEnergySensor(coordinator, entry),
        HeatingCostSensor(coordinator, entry),
        HotWaterCostSensor(coordinator, entry),
        InstantCopSensor(coordinator, entry),
    ]
