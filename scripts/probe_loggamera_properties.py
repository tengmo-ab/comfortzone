#!/usr/bin/env python3
"""Probe the Loggamera API for a Comfortzone pump's fan-speed property.

Loggamera does not publish the list of writable properties, and the fan speed
is not mentioned anywhere in the public API description even though the
Comfortzone Android app can change it. This script finds out what your own
pump answers to.

The API makes this harder than it should be: a rejected write comes back as
**HTTP 200** with ``{"Error": {"Message": "unsupported set parameter"}}``,
which does not say whether the *property name* or the *value* was the
problem. So the probe runs controls first and compares error signatures:

* a **positive control** — a property known to work (e.g. ``SetHeatCurve``),
  written with the value the pump already has, so it is a no-op. This proves
  the API key can write at all and shows what success looks like.
* a **negative control** — a deliberately absurd property name. Whatever
  error that produces is the signature of "no such property".

Any candidate whose error differs from the negative control is a real lead:
the name probably exists and something about the value was rejected.

    # 1. Dump every field the pump reports, fan-related first.
    python3 scripts/probe_loggamera_properties.py --api-key KEY --device-id 12345

    # 2. Controls + full candidate sweep.
    python3 scripts/probe_loggamera_properties.py --api-key KEY --device-id 12345 --probe-write

    # 3. Add your own guesses to the sweep.
    python3 scripts/probe_loggamera_properties.py ... --probe-write --extra-names SetFan,setFanSpeed

    # 4. Check whether other API versions/endpoints accept more properties.
    python3 scripts/probe_loggamera_properties.py ... --probe-endpoints

Fan speed values, from the reverse-engineered control protocol
(github.com/qix67/comfortzone_heatpump):

    1 = low, 2 = normal, 3 = fast/boost, 4 = on timer/scheduled (protocol 1.8+)

Confirmed on an RX95: the mode is reported back as ``Fan state`` (register
2069), not ``Fan speed``.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

API_HOST = "https://platform.loggamera.se"
API_RAWDATA = f"{API_HOST}/Api/v1/RawData"
API_SETPROPERTY = f"{API_HOST}/Api/v1/SetProperty"

# Alternate endpoints to try with a known-good property. If a different API
# version accepts the control write, it is worth re-running the sweep there.
ENDPOINT_CANDIDATES = [
    f"{API_HOST}/Api/v1/SetProperty",
    f"{API_HOST}/Api/v2/SetProperty",
    f"{API_HOST}/Api/v1/SetValue",
    f"{API_HOST}/Api/v1/ExecuteCommand",
    f"{API_HOST}/Api/v1/Command",
]

# Candidate names for the writable fan property. The known-good names are
# hand-picked semantic labels rather than anything derivable from the read
# field ("Heating curve" -> SetHeatCurve), so this is deliberately broad:
# spelling variants, both fan/ventilation vocabularies, and other verbs.
WRITE_CANDIDATES = [
    # fan + speed
    "SetFanSpeed", "SetFanSpeeds", "SetFanSpeedMode", "SetFanSpeedLevel",
    "SetFanSpeedSetting", "SetFanSpeedValue", "SetFanSpeedState",
    # fan + other nouns
    "SetFan", "SetFanMode", "SetFanState", "SetFanLevel", "SetFanSetting",
    "SetFanProgram", "SetFanStep", "SetFanPosition", "SetFanControl",
    "SetFanTimer", "SetFanSchedule", "SetFanBoost", "SetFanNormal",
    # ventilation vocabulary
    "SetVentilation", "SetVentilationMode", "SetVentilationSpeed",
    "SetVentilationLevel", "SetVentilationState", "SetVentilationSetting",
    "SetVent", "SetVentSpeed", "SetVentMode",
    # airflow vocabulary
    "SetAirFlow", "SetAirflow", "SetAirFlowLevel", "SetAirSpeed",
    # other verbs -- the API already uses Reset* and Acknowledge*
    "ChangeFanSpeed", "SelectFanSpeed", "UpdateFanSpeed", "WriteFanSpeed",
    # casing variants, in case the API is case-sensitive
    "setFanSpeed", "setfanspeed", "FanSpeed", "fanSpeed", "fan_speed",
    # the register id / read field name used directly as the property
    "2069", "FanState", "Fan state", "Fan speed",
]

FAN_MODE_NAMES = {1: "low", 2: "normal", 3: "boost", 4: "scheduled"}

# Loggamera support states the fan is written with SetFanState taking string
# values (Off / Low / Normal / High). The earlier sweep tried SetFanState with
# the integer 4 and was rejected -- but the API rejects a bad *value* with the
# same message as a bad *name*, so that told us nothing about the name.
# --probe-values settles it by writing each token and reading the mode back.
VALUE_PROPERTY_DEFAULT = "SetFanState"

# Tokens to try, most likely first. "Off" is included here (unlike in the
# integration) because this probe restores the original mode afterwards and
# the point is to learn the full vocabulary -- but it is placed last so an
# interrupted run is unlikely to leave the fan stopped.
VALUE_TOKENS = [
    "Low", "Normal", "High",
    "Boost", "Auto", "Timer", "Schedule", "Scheduled",
    "low", "normal", "high",
    1, 2, 3, 4,
    "Off",
]

# Properties known to work, paired with the field holding their current value
# so the probe can write a value the pump already has (a no-op).
CONTROL_PROPERTIES = [
    ("SetHeatCurve", "Heating curve"),
    ("SetHotWaterTemp", "Hot water set temp"),
]

BOGUS_NAME = "SetDefinitelyNotARealPropertyXyzzy"

# Substrings that mark a RawData field as worth a closer look.
INTERESTING = ("fan", "fläkt", "flakt", "ventil", "boost", "speed")

DEFAULT_SPACING_SEC = 3.0


def post(url: str, payload: dict, timeout: int = 30) -> tuple[int, object]:
    """POST JSON and return ``(status_code, parsed_body_or_text)``."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        status = err.code
        body = err.read().decode("utf-8", "replace")
    except urllib.error.URLError as err:
        return 0, f"connection failed: {err.reason}"

    try:
        return status, json.loads(body)
    except ValueError:
        return status, body


def classify(status: int, body: object) -> tuple[bool, str]:
    """Return ``(succeeded, error_signature)`` for a SetProperty response.

    The signature is what makes the sweep readable: identical signatures mean
    the API treated those names identically, so a candidate that differs from
    the negative control is the interesting one.
    """
    if status != 200:
        return False, f"HTTP {status}"
    if isinstance(body, str):
        return False, f"non-JSON: {body[:60]}"
    if not isinstance(body, dict):
        return False, f"unexpected body type {type(body).__name__}"

    error = body.get("Error")
    if error:
        if isinstance(error, dict):
            return False, str(error.get("Message") or error)
        return False, str(error)

    data = body.get("Data")
    if isinstance(data, dict) and data.get("Result") is False:
        return False, "Data.Result = false"
    return True, "ok"


def fetch_values(api_key: str, device_id: int) -> list[dict]:
    """Return the RawData ``Values`` list, or exit with a readable error."""
    status, body = post(API_RAWDATA, {"ApiKey": api_key, "DeviceId": device_id})
    if status != 200 or not isinstance(body, dict):
        sys.exit(f"RawData failed (HTTP {status}): {str(body)[:400]}")
    if body.get("Error"):
        sys.exit(f"RawData returned an error: {body['Error']}")
    values = (body.get("Data") or {}).get("Values")
    if not isinstance(values, list):
        sys.exit(f"Unexpected RawData shape: {str(body)[:400]}")
    return values


def value_of(values: list[dict], clear_text_name: str) -> str | None:
    """Return the raw value for a ClearTextName, if present."""
    for item in values:
        if item.get("ClearTextName") == clear_text_name:
            return item.get("Value")
    return None


def dump_values(values: list[dict]) -> None:
    """Print every reported field, fan-related ones first."""
    def is_interesting(item: dict) -> bool:
        haystack = f"{item.get('Name', '')} {item.get('ClearTextName', '')}".lower()
        return any(word in haystack for word in INTERESTING)

    interesting = [item for item in values if is_interesting(item)]
    rest = [item for item in values if not is_interesting(item)]

    print(f"\n=== Fan / ventilation related fields ({len(interesting)}) ===")
    if not interesting:
        print("  (none found -- please share the full dump below)")
    for item in interesting:
        print(
            f"  ClearTextName={item.get('ClearTextName')!r:45} "
            f"Name={item.get('Name')!r:30} Value={item.get('Value')!r} "
            f"UnitPresentation={item.get('UnitPresentation')!r}"
        )
        unit = str(item.get("UnitPresentation") or "")
        raw = str(item.get("Value", "")).strip()
        # A mode is a small unitless integer; a percentage is not a mode.
        if not unit:
            try:
                if int(float(raw)) in FAN_MODE_NAMES:
                    print(f"      ^ unitless value in 1-4 -- candidate mode "
                          f"({FAN_MODE_NAMES[int(float(raw))]})")
            except (TypeError, ValueError):
                pass

    print(f"\n=== All other fields ({len(rest)}) ===")
    for item in rest:
        print(
            f"  ClearTextName={item.get('ClearTextName')!r:45} "
            f"Name={item.get('Name')!r:30} Value={item.get('Value')!r}"
        )


def current_fan_mode(values: list[dict]) -> int | None:
    """Best guess at the pump's current fan mode (1-4), if it reports one."""
    for item in values:
        name = str(item.get("ClearTextName", "")).lower()
        if "fan" not in name or "current" in name:
            continue
        # Percentages are settings, not modes.
        if str(item.get("UnitPresentation") or ""):
            continue
        try:
            mode = int(float(item.get("Value")))
        except (TypeError, ValueError):
            continue
        if mode in FAN_MODE_NAMES:
            return mode
    return None


def try_write(api_key: str, device_id: int, name: str, value: object,
              url: str = API_SETPROPERTY) -> tuple[bool, str, object]:
    """Attempt one SetProperty write and classify the outcome."""
    status, body = post(url, {
        "ApiKey": api_key,
        "DeviceId": device_id,
        "PropertyName": name,
        "Value": value,
    })
    ok, signature = classify(status, body)
    return ok, signature, body


def run_controls(api_key: str, device_id: int, values: list[dict],
                 spacing: float, url: str = API_SETPROPERTY) -> str | None:
    """Run positive and negative controls. Returns the 'no such name' signature."""
    print("\n=== Controls ===")
    print("Establishing what success and 'unknown property' look like on this "
          "account, so the sweep below can be read properly.\n")

    positive_ok = False
    for prop_name, read_field in CONTROL_PROPERTIES:
        current = value_of(values, read_field)
        if current is None:
            print(f"  skipped   {prop_name:20} (pump does not report {read_field!r})")
            continue
        # Write back the value the pump already has -- changes nothing.
        try:
            current_value = float(current)
            if current_value.is_integer():
                current_value = int(current_value)
        except (TypeError, ValueError):
            print(f"  skipped   {prop_name:20} (cannot parse current value {current!r})")
            continue

        ok, signature, _ = try_write(
            api_key, device_id, prop_name, current_value, url=url
        )
        print(f"  {'ACCEPTED' if ok else 'rejected':8}  {prop_name:20} "
              f"(no-op write of current value {current_value}) -> {signature}")
        positive_ok |= ok
        time.sleep(spacing)

    if not positive_ok:
        print("\n  !! No known-good property was accepted. Writes are not working "
              "at all with this API key -- check that the key has write "
              "permission in the Loggamera portal. The sweep below cannot "
              "distinguish anything until this passes.")

    ok, negative_signature, _ = try_write(
        api_key, device_id, BOGUS_NAME, 1, url=url
    )
    print(f"  {'ACCEPTED' if ok else 'rejected':8}  {BOGUS_NAME[:20]:20} "
          f"(deliberately absurd name) -> {negative_signature}")
    if ok:
        print("\n  !! The API accepted a nonsense property name, so 'accepted' "
              "means nothing here. Treat the sweep results with suspicion.")
        return None
    time.sleep(spacing)

    print(f"\n  Signature of an unknown property name: {negative_signature!r}")
    print("  Any candidate below with a DIFFERENT error is a real lead.")
    return negative_signature


def probe_writes(api_key: str, device_id: int, value: int, names: list[str],
                 negative_signature: str | None, spacing: float,
                 url: str = API_SETPROPERTY) -> None:
    """Try each candidate PropertyName and group the outcomes by signature."""
    print(f"\n=== Probing {len(names)} SetProperty names with Value={value} "
          f"({FAN_MODE_NAMES.get(value, '?')}) against {url} ===")
    accepted: list[str] = []
    by_signature: dict[str, list[str]] = {}

    for index, name in enumerate(names):
        if index:
            time.sleep(spacing)
        ok, signature, _body = try_write(api_key, device_id, name, value, url=url)
        by_signature.setdefault(signature, []).append(name)
        flag = ""
        if not ok and negative_signature and signature != negative_signature:
            flag = "  <-- DIFFERENT ERROR, likely a real property"
        print(f"  {'ACCEPTED' if ok else 'rejected':8}  {name:24} "
              f"{signature[:60]:60}{flag}")
        if ok:
            accepted.append(name)

    print("\n--- Summary by response signature ---")
    for signature, group in sorted(by_signature.items(), key=lambda kv: -len(kv[1])):
        marker = ""
        if signature == negative_signature:
            marker = "  (= unknown property)"
        elif signature == "ok":
            marker = "  (= success)"
        else:
            marker = "  <-- WORTH INVESTIGATING"
        print(f"  {len(group):3} x {signature!r}{marker}")
        if signature != negative_signature:
            for name in group:
                print(f"        {name}")

    print()
    if accepted:
        print(f"Property name(s) the API accepted: {', '.join(accepted)}")
        print("Set this as 'fan_speed_property' in the integration options "
              "(or report it so it can become the default).")
    else:
        print("No candidate was accepted. If every candidate produced the same "
              "error as the negative control, the fan property simply is not "
              "exposed on this endpoint -- try --probe-endpoints, and see the "
              "notes in the PR about asking Loggamera support directly.")


def probe_values(api_key: str, device_id: int, prop_name: str,
                 tokens: list, spacing: float, url: str = API_SETPROPERTY,
                 settle: float = 8.0) -> None:
    """Write each candidate value and read the resulting mode back.

    This is the experiment that produces the real mapping: the pump reports
    the mode as an integer in ``Fan state``, so writing a token and re-reading
    that field says exactly which token means which mode -- something no
    amount of guessing at names could establish.

    The pump's original mode is restored at the end.
    """
    values = fetch_values(api_key, device_id)
    original = current_fan_mode(values)
    print(f"\n=== Probing values for {prop_name} against {url} ===")
    if original is None:
        print("  Could not read the pump's current fan mode, so the original "
              "setting cannot be restored afterwards. Aborting -- re-run with "
              "an explicit --value if you want to proceed anyway.")
        return

    print(f"  Current mode: {original} ({FAN_MODE_NAMES[original]}). This will "
          f"be restored when the probe finishes.")
    print(f"  WARNING: this changes the fan mode for real, briefly, {len(tokens)} times.\n")

    accepted: list[tuple[object, int | None]] = []
    for index, token in enumerate(tokens):
        if index:
            time.sleep(spacing)
        ok, signature, _ = try_write(api_key, device_id, prop_name, token, url=url)
        if not ok:
            print(f"  rejected  {token!r:14} -> {signature[:60]}")
            continue

        # Give the platform a moment to propagate before reading back.
        time.sleep(settle)
        mode_after = current_fan_mode(fetch_values(api_key, device_id))
        label = FAN_MODE_NAMES.get(mode_after, "?") if mode_after else "unchanged/unknown"
        print(f"  ACCEPTED  {token!r:14} -> Fan state = {mode_after} ({label})")
        accepted.append((token, mode_after))

    # Restore.
    print(f"\n  Restoring original mode {original} ({FAN_MODE_NAMES[original]})...")
    restored = False
    for token, mode_after in accepted:
        if mode_after == original:
            ok, _, _ = try_write(api_key, device_id, prop_name, token, url=url)
            if ok:
                print(f"  Restored with {token!r}.")
                restored = True
            break
    if not restored:
        print("  !! Could not restore automatically -- no accepted token mapped "
              f"back to mode {original}. Set the fan mode manually in the "
              "Comfortzone app.")

    print("\n--- Value mapping ---")
    if accepted:
        for token, mode_after in accepted:
            print(f"  {token!r:14} => Fan state {mode_after} "
                  f"({FAN_MODE_NAMES.get(mode_after, '?')})")
        print(f"\n'{prop_name}' is the writable property. Set it as "
              "'fan_speed_property' in the integration options and report the "
              "mapping above so it can become the default.")
    else:
        print(f"  No token was accepted for '{prop_name}'. Try another property "
              "name with --value-property, or --extra-names to sweep names again.")


def probe_endpoints(api_key: str, device_id: int, values: list[dict],
                    spacing: float) -> None:
    """Check which endpoints accept a known-good property."""
    print("\n=== Probing endpoints with a known-good property ===")
    control = None
    for prop_name, read_field in CONTROL_PROPERTIES:
        current = value_of(values, read_field)
        if current is not None:
            try:
                parsed = float(current)
                control = (prop_name, int(parsed) if parsed.is_integer() else parsed)
                break
            except (TypeError, ValueError):
                continue
    if control is None:
        print("  Could not find a known-good property to test with.")
        return

    prop_name, value = control
    print(f"  Using {prop_name}={value} (a no-op write) against each endpoint.\n")
    for index, url in enumerate(ENDPOINT_CANDIDATES):
        if index:
            time.sleep(spacing)
        ok, signature, _ = try_write(api_key, device_id, prop_name, value, url=url)
        print(f"  {'WORKS   ' if ok else 'no      '}  {url:52} -> {signature[:60]}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe a Comfortzone pump's Loggamera API for fan-speed support."
    )
    parser.add_argument("--api-key", required=True, help="Loggamera API key")
    parser.add_argument("--device-id", required=True, type=int, help="Loggamera device id")
    parser.add_argument(
        "--probe-write",
        action="store_true",
        help="Run controls, then try candidate SetProperty names (real writes)",
    )
    parser.add_argument(
        "--probe-endpoints",
        action="store_true",
        help="Check which API endpoints accept a known-good property",
    )
    parser.add_argument(
        "--value",
        type=int,
        choices=[1, 2, 3, 4],
        help="Fan mode to write while probing. Defaults to the mode the pump "
             "is already in, so a successful probe changes nothing.",
    )
    parser.add_argument(
        "--extra-names",
        default="",
        help="Comma-separated extra property names to add to the sweep",
    )
    parser.add_argument(
        "--probe-values",
        action="store_true",
        help="Write each candidate VALUE to a single property and read the "
             "mode back, to learn the value vocabulary. Changes the fan mode "
             "for real, then restores it.",
    )
    parser.add_argument(
        "--value-property",
        default=VALUE_PROPERTY_DEFAULT,
        help=f"Property name to use with --probe-values (default {VALUE_PROPERTY_DEFAULT})",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=8.0,
        help="Seconds to wait after a write before reading the mode back "
             "(default 8)",
    )
    parser.add_argument(
        "--names-file",
        help="Path to a file of candidate property names, one per line "
             "(# starts a comment). Replaces the built-in list.",
    )
    parser.add_argument(
        "--endpoint",
        default=API_SETPROPERTY,
        help=f"SetProperty endpoint to sweep against (default {API_SETPROPERTY}). "
             "Use the v2 URL to check whether it exposes more properties.",
    )
    parser.add_argument(
        "--spacing",
        type=float,
        default=DEFAULT_SPACING_SEC,
        help=f"Seconds between writes (default {DEFAULT_SPACING_SEC})",
    )
    args = parser.parse_args()

    values = fetch_values(args.api_key, args.device_id)
    print(f"RawData returned {len(values)} fields.")
    dump_values(values)

    if args.probe_endpoints:
        probe_endpoints(args.api_key, args.device_id, values, args.spacing)

    if args.probe_values:
        probe_values(
            args.api_key, args.device_id, args.value_property, VALUE_TOKENS,
            args.spacing, url=args.endpoint, settle=args.settle,
        )

    if not args.probe_write:
        if not (args.probe_endpoints or args.probe_values):
            print("\nRe-run with --probe-write to identify the writable property name.")
        return

    value = args.value
    if value is None:
        value = current_fan_mode(values)
        if value is None:
            sys.exit(
                "\nCould not detect the current fan mode, so there is no safe "
                "no-op value to write. Re-run with an explicit --value 1|2|3|4 "
                "(2 = normal is the usual default)."
            )
        print(f"\nUsing the pump's current mode ({value} = "
              f"{FAN_MODE_NAMES[value]}) so a successful write is a no-op.")

    negative_signature = run_controls(
        args.api_key, args.device_id, values, args.spacing, url=args.endpoint
    )

    if args.names_file:
        with open(args.names_file, encoding="utf-8") as handle:
            names = [
                line.split("#", 1)[0].strip()
                for line in handle
            ]
            names = [name for name in names if name]
        print(f"\nLoaded {len(names)} candidate names from {args.names_file}")
    else:
        names = list(WRITE_CANDIDATES)

    for extra in args.extra_names.split(","):
        extra = extra.strip()
        if extra and extra not in names:
            names.append(extra)

    probe_writes(
        args.api_key, args.device_id, value, names, negative_signature,
        args.spacing, url=args.endpoint,
    )


if __name__ == "__main__":
    main()
