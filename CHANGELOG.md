# Changelog

All notable changes to the Comfortzone Heat Pump integration are documented here.
This project uses [Semantic Versioning](https://semver.org/).

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
