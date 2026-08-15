#!/usr/bin/env python3
"""Pull URLs, routes and candidate property names out of an Android APK.

Two questions about the ComfortZone app are answerable without rooting the
phone, patching the app or decrypting any traffic, because the answers are
plain string constants compiled into the package:

* **Which URL does the WebView load?** The desktop portal shows
  ``/Start/Index/<device-id>``, which looks nothing like the app, so the app
  must open a different route.
* **Which PropertyName does it send to write the fan?** If the app talks to
  ``platform.loggamera.se`` at all, the name is a literal in the code.

Getting the APK off a Galaxy S24 (no root needed)::

    adb shell pm path se.loggamera.comfortzoneonline2
    adb pull /data/app/.../base.apk

Modern apps ship split APKs; pull every path ``pm path`` prints and pass them
all. ``.apks`` / ``.xapk`` bundles are unpacked automatically.

    python3 scripts/extract_app_strings.py base.apk split_config.*.apk

Nothing here modifies the APK or the device -- it only reads strings, the way
``strings(1)`` would, from a file you already own.
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import zipfile
from collections import defaultdict

# Members worth scanning. Compiled code and the resource table hold the string
# constants; assets often hold a WebView's bundled HTML/JS.
INTERESTING_MEMBERS = re.compile(
    r"""(^classes\d*\.dex$)
      | (^resources\.arsc$)
      | (^AndroidManifest\.xml$)
      | (^assets/)
      | (^res/.*\.(xml|json)$)
      | (\.(js|html|json)$)
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Printable runs, the same idea as strings(1). 4 chars is short enough to catch
# route fragments like "/App" without drowning in noise.
PRINTABLE_RUN = re.compile(rb"[\x20-\x7e]{4,}")

PATTERNS: dict[str, re.Pattern] = {
    # Full URLs -- the WebView's entry point should be among these.
    "urls": re.compile(r"https?://[^\s\"'<>\\)\]}]+"),
    # Anything naming the vendor, in case a host is assembled at runtime.
    "loggamera": re.compile(r"[^\s\"']*loggamera[^\s\"']*", re.IGNORECASE),
    # API route fragments.
    "api_routes": re.compile(r"/?Api/v\d+/[A-Za-z]+"),
    # PascalCase verbs matching the API's naming convention. This is the one
    # that could end the fan hunt outright.
    "property_names": re.compile(
        r"\b(?:Set|Get|Reset|Acknowledge|Execute|Change|Update)"
        r"[A-Z][A-Za-z]{2,30}\b"
    ),
    # MVC-style routes like /Start/Index or /Mobile/Device.
    "mvc_routes": re.compile(r"/[A-Z][A-Za-z]{2,20}/[A-Z][A-Za-z]{2,20}"),
    # Anything fan/ventilation flavoured, whatever shape it takes.
    "fan_words": re.compile(
        r"[A-Za-z_./]*(?:fan|ventilation|flakt|fläkt)[A-Za-z_./]*", re.IGNORECASE
    ),
}

# Framework noise that would otherwise swamp the property-name and route hits.
NOISE = re.compile(
    r"^(?:Set|Get)(?:Text|Color|Value|Name|Type|Size|State$|Id$|Index$|Item|View|"
    r"Data$|Time$|Date$|Bounds|Layout|Margin|Padding|Width|Height|Visible|"
    r"Enabled|Alpha|Scale|Rotation|Translation|Background|Foreground|Image|"
    r"Bitmap|Drawable|Adapter|Listener|Callback|Handler|Property|Attribute|"
    r"Instance|Default|Current|Selected|Checked|Content|Title$|Message|Error|"
    r"Result$|Status$|Count$|Length|Position|Offset|Duration|Animation)",
)

NOISE_ROUTES = re.compile(
    r"^/(?:Android|Java|Kotlin|System|Widget|Support|Material|Google|Firebase|"
    r"Gms|Crashlytics|Analytics)/", re.IGNORECASE
)


def iter_apk_members(path: str):
    """Yield ``(label, bytes)`` for scannable members, unwrapping bundles."""
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, FileNotFoundError, IsADirectoryError) as err:
        print(f"  !! cannot open {path}: {err}", file=sys.stderr)
        return

    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename

            # .apks / .xapk bundles contain nested APKs.
            if name.lower().endswith(".apk"):
                try:
                    nested = archive.read(info)
                except (KeyError, RuntimeError):
                    continue
                yield from _iter_zip_bytes(f"{path}!{name}", nested)
                continue

            if not INTERESTING_MEMBERS.search(name):
                continue
            try:
                yield f"{path}!{name}", archive.read(info)
            except (KeyError, RuntimeError, NotImplementedError) as err:
                print(f"  !! skipping {name}: {err}", file=sys.stderr)


def _iter_zip_bytes(label: str, blob: bytes):
    """Same as :func:`iter_apk_members` for an in-memory nested APK."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        return
    with archive:
        for info in archive.infolist():
            if info.is_dir() or not INTERESTING_MEMBERS.search(info.filename):
                continue
            try:
                yield f"{label}!{info.filename}", archive.read(info)
            except (KeyError, RuntimeError, NotImplementedError):
                continue


def extract_strings(blob: bytes) -> list[str]:
    """Return printable runs, decoded leniently."""
    return [m.group().decode("ascii", "replace") for m in PRINTABLE_RUN.finditer(blob)]


def scan(paths: list[str]) -> dict[str, dict[str, set]]:
    """Collect pattern hits across every APK, remembering where each came from."""
    hits: dict[str, dict[str, set]] = {key: defaultdict(set) for key in PATTERNS}
    for path in paths:
        for label, blob in iter_apk_members(path):
            member = label.split("!", 1)[-1]
            for text in extract_strings(blob):
                for key, pattern in PATTERNS.items():
                    for match in pattern.findall(text):
                        value = match if isinstance(match, str) else match[0]
                        if key == "property_names" and NOISE.match(value):
                            continue
                        if key == "mvc_routes" and NOISE_ROUTES.match(value):
                            continue
                        hits[key][value].add(member)
    return hits


def report(hits: dict[str, dict[str, set]]) -> None:
    """Print the findings, most useful category first."""
    order = [
        ("urls", "URLs -- the WebView entry point should be here"),
        ("loggamera", "Anything naming Loggamera"),
        ("fan_words", "Fan / ventilation flavoured strings"),
        ("property_names", "Candidate PropertyName constants"),
        ("api_routes", "API route fragments"),
        ("mvc_routes", "MVC-style portal routes"),
    ]
    for key, heading in order:
        found = hits.get(key) or {}
        print(f"\n=== {heading} ({len(found)}) ===")
        if not found:
            print("  (none)")
            continue
        for value in sorted(found, key=lambda v: (v.lower(), v)):
            members = sorted(found[value])
            where = members[0] if len(members) == 1 else f"{members[0]} +{len(members)-1}"
            print(f"  {value}")
            print(f"      in {where}")

    print("\n--- What to look for ---")
    print("  * A portal URL that is NOT /Start/Index/<id> -- that is the route")
    print("    the app's WebView opens, and where the fan control lives.")
    print("  * Any SetXxx name under 'Candidate PropertyName constants' that we")
    print("    have not already swept. Feed it straight back in with:")
    print("      probe_loggamera_properties.py --probe-write --extra-names <name>")
    print("  * If no fan-related SetXxx appears anywhere, that is strong evidence")
    print("    the app does not write the fan through the public API at all.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract URLs, routes and property-name constants from APKs."
    )
    parser.add_argument("apks", nargs="+", help="APK / .apks / .xapk files to scan")
    args = parser.parse_args()

    print(f"Scanning {len(args.apks)} file(s)...")
    hits = scan(args.apks)
    if not any(hits.values()):
        print("\nNothing matched. Check the paths are really APKs -- 'adb shell "
              "pm path <package>' lists every split for an installed app.")
        return
    report(hits)


if __name__ == "__main__":
    main()
