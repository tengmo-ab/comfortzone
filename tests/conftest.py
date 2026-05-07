"""Shared pytest configuration for the Comfortzone integration tests.

The integration ships under ``custom_components/comfortzone/``. Importing it
the normal way pulls in Home Assistant via ``__init__.py``, which we don't
want as a test dependency. We therefore:

1. Add the project root to ``sys.path`` so tests can ``import
   custom_components.comfortzone.<sub>``.
2. Pre-register lightweight ``MagicMock`` stand-ins for every Home Assistant
   module the package's ``__init__.py`` touches at import time. The pure
   submodules under test (``calculations``, ``api``, ``const``) don't use
   any of these stubs themselves; the stubs only exist to keep the
   parent-package import from blowing up.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


_HA_MODULES = [
    "homeassistant",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.update_coordinator",
]
for _name in _HA_MODULES:
    sys.modules.setdefault(_name, MagicMock())
