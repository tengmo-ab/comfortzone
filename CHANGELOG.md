# Changelog

All notable changes to the Comfortzone Heat Pump integration are documented here.
This project uses [Semantic Versioning](https://semver.org/).

## [2.11.0] – 2026-06-05

### Changed — hot-water draw detection rebuilt and renamed
A full day of labelled real-world data (two confirmed showers, several
non-shower draws) showed the old `shower_in_progress` sensor was built on
a false premise: **tank temperature can't tell a shower from a bath or a
big dishwashing run**, and in practice the *largest* tank drops were not
showers while the actual showers left almost no trace. The detector is
rebuilt around what the data actually supports.

- **Renamed** `binary_sensor.comfortzone_shower_in_progress` →
  `binary_sensor.comfortzone_large_hot_water_draw`. The entity ID changes
  (the old one is dropped); update any automations that referenced it. The
  rename is deliberate honesty about what the sensor can know.
- **New "missing heat" accumulator model.** Each poll integrates how far
  the tank falls short of where the pump's current mode says it should be.
  The sensor turns on once the accumulated shortfall passes a configurable
  threshold and off once the tank recovers (hysteresis). This replaces the
  brittle instantaneous-slope + fixed-drop logic.
- **Recalibrated expected production slope** from 0.40 → **0.15 °C/min**.
  The old value was almost 3× the real RX95 rate (measured across five
  cycles), which made every production cycle look like a draw — the root
  cause of the constant false positives at cycle start.
- **Artifact rejection.** When the pump switches to hot-water mode the
  `Hot water temp` reading can plunge tens of degrees in minutes because
  the exchange valve repoints the sensor onto the cold loop (the same
  directional effect behind the old −30 °C heating ΔT). Drops faster than
  1.0 °C/min are now treated as sensor artifacts and excluded — this stops
  a 28 °C "phantom drop" from being read as a giant draw.
- **Configurable sensitivity** via the options flow
  (`large_draw_threshold_c`, default 3 °C of accumulated shortfall).

### Added
- **Hot water cycle duration sensor** (`hot_water_cycle_duration`,
  diagnostic). Reports the length in minutes of the last completed
  hot-water production cycle, with a rolling-median baseline and an
  `unusually_long` attribute. This is the cycle-length heuristic for
  catching the one case the draw sensor is blind to: a shower taken while
  the pump is *already* producing into a still-hot tank leaves no mark on
  the tank temperature, but it does force the pump to run longer to refill.
  The "unusually long" floor is configurable (`long_hw_cycle_min`,
  default 50 min).

## [2.10.0] – 2026-06-04

### Added
- **Addition heater energy sensor** (`addition_heater_energy`). Cumulative
  kWh integrated from the `Addition effect` reading, with
  `state_class: total_increasing` so the Home Assistant Energy panel
  picks it up directly. Lets you track exactly how much COP-1 backup
  electricity has been used over time and place it in the Energy panel
  as an "Individual device" entry. Pairs naturally with the existing
  `addition_heater_runtime` (hours) and `addition_heater_active`
  (problem alarm) sensors.

## [2.9.0] – 2026-06-03

### Fixed
- **HACS dashboard icon** — `hacs.json` now declares an `icon` URL so
  HACS can render the integration's logo in the dashboard listing
  instead of a blank tile. The HA device-page icon was already served
  from `brand/icon.png` via HA 2026.3+'s local brand mechanism.
- **Shower detection rewritten to be mode-aware.** The previous logic
  suppressed detection completely while the pump was producing hot
  water and used a single fixed slope threshold. Real-world data showed
  this missed obvious draws (e.g. a 14 °C tank drop over 45 minutes
  that never triggered). The new detector compares the **actual** tank
  slope against the **expected** slope given the pump's current
  activity:
  - Idle / heating: expected ~−0.05 °C/min (standing losses)
  - Making hot water: expected ~+0.40 °C/min (active production)
  An alarm fires when the actual slope falls more than 0.20 °C/min
  below expected AND the tank has actually dropped ≥ 0.5 °C in the
  rolling 5-minute window. This catches showers during HW production
  (when the tank is being recharged) and ignores transient sensor
  blips. The trailing on-window is also bumped from 90 s to 120 s.
- Sensor attributes now expose `actual slope`, `expected slope`,
  `deviation`, `absolute drop` and `pump_making_hot_water` so it's
  easy to see exactly why the alarm did or didn't fire.

## [2.8.0] – 2026-05-18

### Changed — electrical-input estimation recalibrated
Real-world observations showed the per-mode power/energy/cost sensors
under-counting by ~0.4–0.7 kWh/h during sustained heat-pump operation
on cold nights. Root causes: the EN255 datasheet COP figures assume
ideal lab conditions (12 °C wet outdoor air), defrost cycles bypass the
thermal × factor pipeline entirely, and the controller's standby draw
was set too low. Three calibrations land in this release:

- **Cold-outdoor COP penalty.** The base spec-curve factor is now
  multiplied by `1 + 0.04 × (12 − outdoor_°C)` (capped at +60 %) when
  the air is colder than the EN255 reference temperature. At
  −5 °C outside this raises the estimate by ~68 %; at +12 °C or warmer
  it is a no-op. Override factor still bypasses this when set.
- **Defrost flat estimate.** When the pump enters a defrost cycle
  (compressor running, both exchange valves closed) the integration
  now reports a flat 1500 W electrical estimate instead of using the
  near-zero thermal reading.
- **Standby bumped from 15 W to 25 W** to better match real-world
  controller + PCB + sensor draw on RX95.

Six new pytest cases cover the new behaviour (47 total, all green).

## [2.7.0] – 2026-05-08

### Added
- **Compressor running-at-max binary sensor**
  (`compressor_running_at_max`). Trips when inverter load has been
  continuously at or above the configured threshold for the configured
  duration. Useful as a clean automation trigger for "pump is out of
  headroom" reasoning — e.g. accept that indoor target won't be
  reached, defer DHW production, or ease the heat curve so the pump
  isn't forced into the resistive backup. Defaults: 90 % for 5 min;
  both configurable via the options flow.

## [2.6.0] – 2026-05-08

### Added
- **Configurable alarm thresholds.** All four alarm sensors gained
  options-flow controls:
  - `short_cycle_threshold` — starts per hour (default 6)
  - `addition_power_threshold_w` (default 500 W) and
    `addition_duration_threshold_s` (default 600 s, i.e. 10 min)
  - `filter_warning_days` (default 7)
  - `low_hw_threshold_c` (default 40 °C) and
    `low_hw_hysteresis_c` (default 3 °C; alarm clears at threshold +
    hysteresis)
- **Addition heater runtime sensor** (`addition_heater_runtime`).
  Cumulative hours the resistive backup has been drawing >100 W.
  Lets you spot how much expensive COP-1 electricity has slipped past
  the heat pump and informs whether settings need adjusting.
- **DHW production rate sensor** (`dhw_production_rate`). Thermal kW
  averaged over a 5-minute window during HW production. Drops over
  time at constant compressor load are an early sign of fouling /
  limescale on the tank coil. Disabled by default.
- **Tank heating rate sensor** (`tank_heating_rate`). Mirror image
  of `tank_decay_rate` — °C per hour while the tank is being charged.
  Disabled by default.
- **Compressor load percentage sensor** (`compressor_load_percentage`).
  `frequency / freq_max × 100`. Lets a controller know whether the
  compressor still has headroom or is already pinned at 100 %.

## [2.5.0] – 2026-05-08

### Fixed
- `heating_circuit_delta_t` no longer reports nonsense (e.g. -30 °C)
  while the pump is producing hot water or idling. The sensor now
  updates **only during heating mode** and holds the last meaningful
  reading otherwise — values land in the expected 0–7 °C band.

### Added
- **Hot water loop ΔT** sensor (`hot_water_loop_delta_t`). Mirror of
  the heating ΔT sensor for DHW production: shows the absolute
  temperature differential the heat exchanger lifts in a single pass
  (~25–40 °C is healthy on RX95).
- **Compressor short-cycling alarm** (`compressor_short_cycling`,
  PROBLEM device class). Trips when the compressor starts more than
  6 times in the last hour — typically means undersized emitters,
  hysteresis tuning, or refrigerant-charge issues.
- **Addition heater active alarm** (`addition_heater_active`). Trips
  when the resistive elpatron has been drawing >500 W for more than
  10 minutes. Aligned with the goal of avoiding COP-1 backup heat.
- **Filter change soon warning** (`filter_change_due_soon`). Soft
  warning when fewer than 7 days remain on the filter timer.
- **Low hot water warning** (`low_hot_water`). Trips at <40 °C tank
  temperature, clears at >43 °C (hysteresis). Useful trigger for
  "load DHW now if price is below average" automations.

## [2.4.0] – 2026-05-07

### Added
- **Per-model COP curves.** The thermal-to-electrical conversion factor is
  now looked up per pump model in `MODEL_COP_CURVES`. Selecting `RX95` uses
  its EN255 datasheet curve (factor 0.235 at 35°C flow → 0.314 at 50°C
  flow); other models fall back to a generic 0.30 factor that the user can
  refine via the override option. Adding support for a new model is now a
  single dict entry — no helper-function changes required.
- Three additional pytest cases covering the per-model lookup and the
  generic fallback (41 cases total, all green).

### Changed
- `compressor_factor_from_flow()` and `compute_compressor_electrical_w()`
  now take an explicit `model` parameter (defaults to `RX95`). The
  `OptimisticConfirmedMixin` exposes the configured model so every
  computed sensor automatically uses the right curve for the configured
  pump.
- README and option help texts clarify that the spec-curve numbers are
  RX95-specific and that other models use a generic fallback until their
  datasheet values are added.

## [2.3.0] – 2026-05-07

### Added
- **Optimistic-until-confirmed reads after writes.** New
  `OptimisticConfirmedMixin` keeps the entity showing the user-written
  value (`SetIndoorTemp`, `SetHotWaterTemp`, `SetHeatCurve`,
  `SetHotWaterExtraEnabled`, `SetHolidayReductionDays`) until the
  Loggamera API actually confirms it on a follow-up poll, with a 90 s
  timeout safety net. Eliminates UI flicker when the API hasn't caught
  up to a fresh write at the next coordinator refresh.
- **Float-tolerant boolean helper** (`is_truthy`) that treats `"1"`,
  `"1.0"`, `"true"`, `"YES"`, `"on"` and similar variants identically.
  Used by the mode predicates and switches.
- New `calculations.py` module exposing the pure derivation helpers
  (spec curve, RawData parsing, mode predicates). Independently
  importable and unit-testable without Home Assistant installed.

### Changed
- **Faster post-write feedback.** First refresh after a successful set
  drops from 20 s to **5 s**, with a follow-up at 15 s as a safety net.
  Combined with the optimistic mixin, the user sees their setting take
  effect almost immediately without flicker.
- **Float-tolerant numeric parsing** across sensor / number / switch /
  binary_sensor / climate. The Loggamera API may report integer-looking
  values as `"70"` or `"70.0"` interchangeably depending on the field;
  the integration now accepts both.
- `api.py` and `computed_sensors.py` now import their helpers from
  `calculations.py`. The existing
  `from .api import find_value_from_raw_data` continues to work via a
  re-export, so any external code keeps compiling.
- README clarified: the "Price reports öre/kWh" toggle is **off by
  default** because the modern, official Nord Pool integration in HA
  already reports `SEK/kWh`. Only enable it for legacy template-based
  Nord Pool sensors that still emit öre.

### Internal
- 38 pytest cases (kept locally — `tests/` is now gitignored to keep
  the HACS package lean) covering the spec-based COP curve, mode
  predicates with float-shaped booleans, and the API client's
  retry / spacing / concurrency guarantees.

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
