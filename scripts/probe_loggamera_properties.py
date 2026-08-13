#!/usr/bin/env python3
"""Probe the Loggamera API for a Comfortzone pump's fan-speed property.

Loggamera does not publish the list of writable properties, and the fan speed
is not mentioned anywhere in the public API description even though the
Comfortzone Android app can change it. This script finds out what your own
pump answers to, so the integration can be pinned to the right names.

It has no dependencies beyond the Python standard library, so it can be run
straight on the Home Assistant host:

    # 1. Dump everything the pump reports, highlighting fan-related fields.
    python3 scripts/probe_loggamera_properties.py --api-key KEY --device-id 12345

    # 2. Find the SetProperty name that writes the fan speed. This performs
    #    real writes, so it re-applies the mode the pump is already in --
    #    a successful probe is a no-op for the pump.
    python3 scripts/probe_loggamera_properties.py --api-key KEY --device-id 12345 --probe-write

Fan speed values, from the reverse-engineered control protocol
(github.com/qix67/comfortzone_heatpump):

    1 = low, 2 = normal, 3 = fast/boost, 4 = scheduled (protocol 1.8+)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

API_RAWDATA = "https://platform.loggamera.se/Api/v1/RawData"
API_SETPROPERTY = "https://platform.loggamera.se/Api/v1/SetProperty"

# Names that might be the writable fan property. Extend freely -- an unknown
# name is rejected by the API without touching the pump.
WRITE_CANDIDATES = [
    "SetFanSpeed",
    "SetFanMode",
    "SetVentilation",
    "SetVentilationMode",
    "SetFanLevel",
    "SetFanSpeedMode",
    "SetFanState",
]

FAN_MODE_NAMES = {1: "low", 2: "normal", 3: "boost", 4: "scheduled"}

# Substrings that mark a RawData field as worth a closer look.
INTERESTING = ("fan", "fläkt", "flakt", "ventil", "boost", "speed")

# Writes are spaced out so the probe never hammers the API.
WRITE_SPACING_SEC = 6.0


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
        if str(item.get("Value", "")).strip() in {"1", "2", "3", "4", "1.0", "2.0", "3.0", "4.0"}:
            mode = FAN_MODE_NAMES.get(int(float(item["Value"])))
            print(f"      ^ in the 1-4 range -- could be the mode ({mode})")

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
        try:
            mode = int(float(item.get("Value")))
        except (TypeError, ValueError):
            continue
        if mode in FAN_MODE_NAMES:
            return mode
    return None


def probe_writes(api_key: str, device_id: int, value: int) -> None:
    """Try each candidate PropertyName and report which one the API accepts."""
    print(f"\n=== Probing SetProperty names with Value={value} "
          f"({FAN_MODE_NAMES.get(value, '?')}) ===")
    accepted: list[str] = []

    for index, name in enumerate(WRITE_CANDIDATES):
        if index:
            time.sleep(WRITE_SPACING_SEC)
        status, body = post(
            API_SETPROPERTY,
            {
                "ApiKey": api_key,
                "DeviceId": device_id,
                "PropertyName": name,
                "Value": value,
            },
        )
        summary = json.dumps(body) if not isinstance(body, str) else body
        ok = status == 200 and not (
            isinstance(body, dict)
            and (body.get("Error") or (body.get("Data") or {}).get("Result") is False)
        )
        print(f"  {'ACCEPTED' if ok else 'rejected'}  {name:20} "
              f"HTTP {status} {summary[:220]}")
        if ok:
            accepted.append(name)

    print()
    if accepted:
        print(f"Property name(s) the API accepted: {', '.join(accepted)}")
        print("Set this as 'fan_speed_property' in the integration options "
              "(or report it so it can become the default).")
    else:
        print("No candidate was accepted. Please share the output above -- the "
              "rejection messages usually hint at the expected name.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe a Comfortzone pump's Loggamera API for fan-speed support."
    )
    parser.add_argument("--api-key", required=True, help="Loggamera API key")
    parser.add_argument("--device-id", required=True, type=int, help="Loggamera device id")
    parser.add_argument(
        "--probe-write",
        action="store_true",
        help="Try candidate SetProperty names (performs real writes)",
    )
    parser.add_argument(
        "--value",
        type=int,
        choices=[1, 2, 3, 4],
        help="Fan mode to write while probing. Defaults to the mode the pump "
             "is already in, so a successful probe changes nothing.",
    )
    args = parser.parse_args()

    values = fetch_values(args.api_key, args.device_id)
    print(f"RawData returned {len(values)} fields.")
    dump_values(values)

    if not args.probe_write:
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

    probe_writes(args.api_key, args.device_id, value)


if __name__ == "__main__":
    main()
