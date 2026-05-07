"""Pure calculation helpers for the Comfortzone integration.

This module is intentionally free of Home Assistant imports so the helpers
can be unit-tested in isolation. Everything here operates on the raw list
of value dictionaries that the Loggamera RawData endpoint returns:

    [
        {"ClearTextName": "Indoor temp (TE3)", "Value": "21.7", ...},
        {"ClearTextName": "Compressor active", "Value": "1", ...},
        ...
    ]

Functions in this module never raise on missing or malformed data — they
return ``None`` (for numbers) or ``False`` (for booleans) and let the
caller decide whether to mark an entity as unavailable.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from .const import (
    CIRCULATION_PUMP_MAX_W,
    CLEAR_TEXT_NAMES,
    COP_SPEC_FACTOR_HIGH,
    COP_SPEC_FACTOR_LOW,
    COP_SPEC_FLOW_HIGH_C,
    COP_SPEC_FLOW_LOW_C,
    FAN_MAX_W,
)


# --- RawData primitives ----------------------------------------------------


def find_value_from_raw_data(
    values_list: Optional[Iterable[Any]],
    identifier: str,
    key_to_match: str = "ClearTextName",
) -> Optional[str]:
    """Return the ``Value`` string for the entry matching ``identifier``."""
    if not values_list:
        return None
    for item in values_list:
        if isinstance(item, dict) and item.get(key_to_match) == identifier:
            return item.get("Value")
    return None


def read_float(values_list: Optional[Iterable[Any]], clear_text_name: str) -> Optional[float]:
    """Read a numeric value from RawData.Values, ``None`` if missing/invalid."""
    raw = find_value_from_raw_data(values_list, clear_text_name)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# --- Heat-pump electrical estimation --------------------------------------


def compressor_factor_from_flow(flow_temp_c: Optional[float]) -> float:
    """Interpolate the thermal-to-electrical factor based on flow temperature.

    Anchored at the EN255 spec points from the Comfortzone RX95 datasheet:

      35°C flow → factor 0.235 (COP 4.25)
      50°C flow → factor 0.314 (COP 3.18)

    Outside the [35, 50] °C window the curve is clamped to the nearest
    spec point so values stay in physically reasonable territory.
    When ``flow_temp_c`` is ``None`` the worst-case (high) factor is used.
    """
    if flow_temp_c is None:
        return COP_SPEC_FACTOR_HIGH
    if flow_temp_c <= COP_SPEC_FLOW_LOW_C:
        return COP_SPEC_FACTOR_LOW
    if flow_temp_c >= COP_SPEC_FLOW_HIGH_C:
        return COP_SPEC_FACTOR_HIGH
    span = COP_SPEC_FLOW_HIGH_C - COP_SPEC_FLOW_LOW_C
    pos = (flow_temp_c - COP_SPEC_FLOW_LOW_C) / span
    return COP_SPEC_FACTOR_LOW + pos * (COP_SPEC_FACTOR_HIGH - COP_SPEC_FACTOR_LOW)


def compute_compressor_electrical_w(
    values: Optional[Iterable[Any]], override_factor: float
) -> Optional[float]:
    """Estimate compressor electrical input in W from reported thermal output.

    A non-zero ``override_factor`` bypasses the spec curve and uses that
    constant factor — used when the user has empirical data and prefers a
    fixed conservative number (e.g. 0.4).
    """
    thermal = read_float(values, CLEAR_TEXT_NAMES["COMPRESSOR_POWER"])
    if thermal is None:
        return None
    if override_factor and override_factor > 0:
        return thermal * override_factor
    flow_c = read_float(values, CLEAR_TEXT_NAMES["FLOW_TEMP"])
    return thermal * compressor_factor_from_flow(flow_c)


def compute_circulation_pump_w(values: Optional[Iterable[Any]]) -> float:
    """Estimate circulation pump electrical draw in W from reported speed (%)."""
    pct = read_float(values, CLEAR_TEXT_NAMES["CIRC_PUMP_SPEED"]) or 0.0
    return (pct / 100.0) * CIRCULATION_PUMP_MAX_W


def compute_fan_w(values: Optional[Iterable[Any]]) -> float:
    """Estimate fan electrical draw in W from reported speed (%)."""
    pct = read_float(values, CLEAR_TEXT_NAMES["FAN_SPEED_CURRENT"]) or 0.0
    return (pct / 100.0) * FAN_MAX_W


def compute_addition_w(values: Optional[Iterable[Any]]) -> float:
    """Read the resistive addition heater power in W (already electrical)."""
    return read_float(values, CLEAR_TEXT_NAMES["ADDITION_POWER"]) or 0.0


# --- Operating-mode predicates --------------------------------------------


def compressor_active(values: Optional[Iterable[Any]]) -> bool:
    """True when the heat pump's compressor is reported as running."""
    return find_value_from_raw_data(
        values, CLEAR_TEXT_NAMES["COMPRESSOR_ACTIVE"]
    ) == "1"


def heating_valve_open(values: Optional[Iterable[Any]]) -> bool:
    """True when the exchange valve is routed to space heating."""
    return find_value_from_raw_data(
        values, CLEAR_TEXT_NAMES["EXCHANGE_VALVE_HEATING"]
    ) == "1"


def hw_valve_open(values: Optional[Iterable[Any]]) -> bool:
    """True when the exchange valve is routed to hot water production."""
    return find_value_from_raw_data(
        values, CLEAR_TEXT_NAMES["EXCHANGE_VALVE_HW"]
    ) == "1"


def is_heating(values: Optional[Iterable[Any]]) -> bool:
    """True when the pump is dedicated to space heating right now."""
    return compressor_active(values) and heating_valve_open(values)


def is_hot_water(values: Optional[Iterable[Any]]) -> bool:
    """True when the pump is dedicated to hot water production right now."""
    return compressor_active(values) and hw_valve_open(values)


def is_defrosting(values: Optional[Iterable[Any]]) -> bool:
    """Heuristic: compressor running but neither valve open ⇒ defrost cycle."""
    return (
        compressor_active(values)
        and not heating_valve_open(values)
        and not hw_valve_open(values)
    )
