"""Unit tests for the pure calculation helpers.

These tests don't need Home Assistant to be installed because every helper
in ``custom_components/comfortzone/calculations.py`` is intentionally free
of HA imports. They validate the rules used by the computed sensors so that
the Energy panel sees believable numbers.
"""
from __future__ import annotations

import math

import pytest

from custom_components.comfortzone.calculations import (
    compressor_active,
    compressor_factor_from_flow,
    compute_addition_w,
    compute_circulation_pump_w,
    compute_compressor_electrical_w,
    compute_fan_w,
    find_value_from_raw_data,
    is_defrosting,
    is_heating,
    is_hot_water,
    read_float,
)
from custom_components.comfortzone.const import (
    CIRCULATION_PUMP_MAX_W,
    COP_SPEC_FACTOR_HIGH,
    COP_SPEC_FACTOR_LOW,
    FAN_MAX_W,
)


def _values(**kwargs) -> list[dict]:
    """Build a fake RawData.Values list from a flat ``ClearTextName=value`` map."""
    return [
        {"ClearTextName": name, "Value": str(value)}
        for name, value in kwargs.items()
    ]


# --- Test 1: spec curve clamps below 35°C -----------------------------------
def test_compressor_factor_clamps_below_low_anchor():
    """Below the EN255 low anchor we should pin to the COP-4.25 factor."""
    assert compressor_factor_from_flow(20.0) == COP_SPEC_FACTOR_LOW
    assert compressor_factor_from_flow(35.0) == COP_SPEC_FACTOR_LOW


# --- Test 2: spec curve clamps above 50°C -----------------------------------
def test_compressor_factor_clamps_above_high_anchor():
    """Above the EN255 high anchor we should pin to the COP-3.18 factor."""
    assert compressor_factor_from_flow(50.0) == COP_SPEC_FACTOR_HIGH
    assert compressor_factor_from_flow(60.0) == COP_SPEC_FACTOR_HIGH


# --- Test 3: linear interpolation in between --------------------------------
def test_compressor_factor_interpolates_midpoint():
    """At 42.5°C (midpoint) the factor should be the arithmetic mean."""
    expected = (COP_SPEC_FACTOR_LOW + COP_SPEC_FACTOR_HIGH) / 2
    assert math.isclose(compressor_factor_from_flow(42.5), expected, rel_tol=1e-9)


# --- Test 4: override factor bypasses the spec curve ------------------------
def test_compressor_electrical_override_bypasses_spec_curve():
    """A non-zero override factor should be used directly with the thermal output."""
    values = _values(
        **{
            "Compressor effect": "3500",
            "Flow line temp (TE1)": "27",  # would normally clamp to LOW
        }
    )
    # Override 0.4 → 3500 W * 0.4 = 1400 W (matches the user's empirical model)
    assert compute_compressor_electrical_w(values, override_factor=0.4) == 1400.0


# --- Test 5: spec curve used when override is 0 -----------------------------
def test_compressor_electrical_uses_spec_curve_when_override_zero():
    """When override_factor=0 the helper must fall back to flow-based interpolation."""
    values = _values(**{"Compressor effect": "3500", "Flow line temp (TE1)": "50"})
    # 50°C flow → factor 0.314 → 3500 * 0.314 = 1099 W
    expected = 3500 * COP_SPEC_FACTOR_HIGH
    assert math.isclose(
        compute_compressor_electrical_w(values, override_factor=0.0),
        expected,
        rel_tol=1e-9,
    )


# --- Test 6: returns None when thermal reading missing ---------------------
def test_compressor_electrical_returns_none_when_no_thermal():
    """Missing 'Compressor effect' should propagate as None (sensor unavailable)."""
    values = _values(**{"Flow line temp (TE1)": "40"})
    assert compute_compressor_electrical_w(values, override_factor=0.4) is None


# --- Test 7: aux helpers scale linearly with reported speeds ---------------
def test_aux_helpers_scale_with_reported_speeds():
    """Pump and fan should be percentage * nameplate W."""
    values = _values(
        **{
            "Circulation pump speed": "60",
            "Fan speed (current)": "70",
            "Addition effect": "0",
        }
    )
    assert math.isclose(compute_circulation_pump_w(values), 0.60 * CIRCULATION_PUMP_MAX_W)
    assert math.isclose(compute_fan_w(values), 0.70 * FAN_MAX_W)
    assert compute_addition_w(values) == 0.0


# --- Test 8: mode predicates pick the right state --------------------------
@pytest.mark.parametrize(
    "compressor,heating_valve,hw_valve,expected",
    [
        ("0", "0", "0", "idle"),
        ("1", "1", "0", "heating"),
        ("1", "0", "1", "hot_water"),
        ("1", "0", "0", "defrost"),
    ],
)
def test_mode_predicates(compressor, heating_valve, hw_valve, expected):
    values = _values(
        **{
            "Compressor active": compressor,
            "Exchange valve heating (on/off)": heating_valve,
            "Exchange valve hot water (on/off)": hw_valve,
        }
    )
    assert is_heating(values) == (expected == "heating")
    assert is_hot_water(values) == (expected == "hot_water")
    assert is_defrosting(values) == (expected == "defrost")
    assert compressor_active(values) == (compressor == "1")


# --- Test 9: read_float gracefully handles bad/missing input ---------------
def test_read_float_handles_bad_and_missing_input():
    values = [
        {"ClearTextName": "Indoor temp (TE3)", "Value": "21.7"},
        {"ClearTextName": "Garbage field", "Value": "not-a-number"},
        {"ClearTextName": "Empty field", "Value": ""},
    ]
    assert read_float(values, "Indoor temp (TE3)") == 21.7
    assert read_float(values, "Garbage field") is None
    assert read_float(values, "Empty field") is None
    assert read_float(values, "Missing field") is None
    assert read_float(None, "Indoor temp (TE3)") is None


# --- Test 10: find_value_from_raw_data ignores junk entries ----------------
def test_find_value_skips_non_dict_entries():
    """The helper must tolerate non-dict items the API might return."""
    values = [
        "garbage",
        None,
        {"ClearTextName": "Outdoor temp (TE0)", "Value": "12.1"},
        {"OtherKey": "not the one"},
    ]
    assert find_value_from_raw_data(values, "Outdoor temp (TE0)") == "12.1"
    assert find_value_from_raw_data(values, "Missing") is None
    assert find_value_from_raw_data([], "Anything") is None
