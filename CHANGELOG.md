# Changelog

All notable changes to the Comfortzone Heat Pump integration are documented here.
This project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- New `calculations.py` module containing all pure derivation helpers
  (factor curve, RawData parsing, mode predicates) — independently
  importable and unit-testable without Home Assistant installed.
- `tests/` directory with 23 pytest cases covering the spec-based COP
  curve, mode predicates, and the API client's retry / spacing behaviour
  including a concurrent-write scenario that proves the internal write
  lock keeps HTTP calls strictly sequential.

### Changed
- `api.py` and `computed_sensors.py` now import their helpers from
  `calculations.py`. No behaviour change; the existing
  `from .api import find_value_from_raw_data` continues to work via a
  re-export.

## [2.2.0] – 2026-05-07

### Added
- **Spec-based COP curve** for compressor electrical estimation. The factor
  now interpolates linearly between the EN255 datasheet anchor points
  (`0.235` at 35°C flow → `0.314` at 50°C flow) so estimates track the
  pump's real performance under different operating conditions. Users can
  still override with a fixed factor in options (set to 0 to keep the
  spec curve, default).
- **Defrost detection** — `defrost_cycle_count` and `last_defrost_duration`
  use a heuristic ("compressor active but neither valve open") and report
  their result when the next-cycle resolution lands.
- **Compressor cycle counter** for wear/short-cycling diagnostics.
- **Per-mode runtime sensors** (`heating_runtime`, `hot_water_runtime`)
  reporting cumulative hours.
- **Heating circuit ΔT** sensor (flow − return) for diagnosing
  circulation-flow problems.
- **Tank decay rate** sensor (`tank_decay_rate`) measuring °C/h drop in the
  tank while the pump is not producing hot water.
- **Specific heating energy** sensor (`specific_heating_energy`) — moving
  average of kWh required per °C indoor temperature rise.
- **Reduced fan schedule** diagnostic sensors for weekday and weekend
  quiet-mode windows.
- **Shower-in-progress** binary sensor — heuristic detection from a fast
  drop in tank temperature while no production is happening.
- **Pump activity status** now reports `Defrosting` when both valves are
  closed mid-compressor-run.
- **Price entity at install** — the initial setup now offers a
  `sensor`-domain entity selector for a Nord Pool / electricity price
  sensor, making per-mode cost tracking work out of the box.

### Changed
- `instant_cop` is **enabled by default**. Reports unavailable when the
  estimated electrical input is below 100 W to avoid noise near idle.
- Default for `price_in_ore` changed to **off**. The modern Nord Pool
  integration reports SEK/kWh natively; the toggle is only useful for
  legacy setups.
- Compressor electrical factor option's default is now `0` (= use spec
  curve). The previous `0.4` constant remains available as an override.

## [2.1.0] – 2026-05-07

### Added
- **Pump activity status** sensor (`pump_activity_status`) – reports `Heating` /
  `Making Hot Water` / `Idle` based on compressor and valve state.
- **Estimated total power consumption** sensor (`estimated_total_power`) –
  combined electrical W from compressor (thermal-to-electrical conversion),
  resistive addition heater, circulation pump, fan and standby load.
- **Estimated aux power consumption** sensor (`estimated_aux_power`) – fan +
  standby only; disabled by default.
- **Per-mode power sensors** – `heating_power` and `hot_water_power`. Reports
  the live electrical draw while the pump is dedicated to that mode.
- **Per-mode energy sensors** – `heating_energy`, `hot_water_energy` and
  `total_energy`. Cumulative kWh with `total_increasing` state class so they
  drop straight into the Home Assistant Energy panel.
- **Per-mode cost sensors** – `heating_cost` and `hot_water_cost`. Cumulative
  cost in your HA-configured currency, computed from a user-supplied Nord Pool
  (or other) electricity-price entity. Disabled by default.
- **Instant COP** sensor (`instant_cop`) – live thermal-output / electrical-in
  ratio. Disabled by default.
- **Options flow:** new fields for *electricity price entity*, *price in
  öre/kWh*, and *compressor electrical factor*.
- Translations for all new options strings (English + Swedish).

### Changed
- README updated with Energy-panel guidance and computed-sensor reference.
- Bumped manifest version to 2.1.0.

### Security
- Rewrote git history to scrub a development device-id that had appeared in
  earlier README revisions. Replaced with a generic placeholder.

## [2.0.0]

Initial HACS-compliant release with full RX95 entity coverage, options flow
for the model selector, diagnostics, branded device card and Swedish
translation.
