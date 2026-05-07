# Tests

Lightweight unit tests that exercise the integration's pure helpers and the
Loggamera API client. They run without a Home Assistant install — the
package's `__init__.py` is shielded from real HA imports through stubs in
`tests/conftest.py`.

## Running

From the project root:

```bash
pip install -r tests/requirements.txt
python -m pytest tests/
```

## What is covered

`test_calculations.py` validates the pure derivation helpers:

- **EN255 spec curve** — clamps below 35°C and above 50°C, linearly
  interpolates between them.
- **Override factor** — a non-zero option override bypasses the spec curve.
- **Missing data tolerance** — `None` values propagate gracefully so a
  sensor goes "unavailable" instead of throwing.
- **Mode predicates** — heating / hot-water / defrost / idle dispatch from
  compressor + valve states.
- **Aux scaling** — circulation pump, fan and addition heater linear
  conversions match the RX95 nameplate.

`test_api_client.py` validates the Loggamera client:

- Parses normal RawData responses.
- Treats `"Result":"busy"` (even HTML-wrapped) as a non-fatal soft failure.
- Raises `ComfortzoneApiAuthError` when the API reports authentication
  problems.
- `async_set_property` returns success on 2xx, **does not retry** 4xx, and
  retries exactly once on 5xx / timeout.
- Returns `False` cleanly when the API answers `Data.Result = false`.
- The internal write lock keeps **at most one HTTP write in flight** even
  when several calls are issued concurrently — the integration's main
  protection against overloading Loggamera.
- The 5-second minimum spacing between writes triggers an actual delay
  when two calls happen back-to-back.
