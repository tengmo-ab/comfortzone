"""Shared helpers for the Comfortzone integration."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import dt as dt_util

from .const import CONF_DEVICE_ID, CONF_MODEL, DOMAIN

# How long an optimistic post-write state hides stale API readings.
# After this many seconds we trust whatever the API reports, even if it
# disagrees with what we just wrote — this prevents the entity from
# getting permanently "stuck" if the device silently rejected the change.
OPTIMISTIC_TIMEOUT_SECONDS = 90


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return a consistent DeviceInfo for all platforms in this integration."""
    device_id = entry.data.get(CONF_DEVICE_ID)
    model = entry.data.get(CONF_MODEL, "RX95")
    return DeviceInfo(
        identifiers={(DOMAIN, str(device_id) if device_id is not None else entry.entry_id)},
        manufacturer="Comfortzone",
        model=model,
        name=f"Comfortzone {model}",
        # The "Visit" button on the device page should send users to the
        # human-facing Loggamera portal, not the API host.
        configuration_url="https://portal.loggamera.se",
    )


def device_unique_id(entry: ConfigEntry) -> str:
    """Return the stable unique-id base for entities under this entry."""
    device_id = entry.data.get(CONF_DEVICE_ID)
    return str(device_id) if device_id is not None else entry.entry_id


_SENTINEL = object()


def _values_match(a: Any, b: Any) -> bool:
    """Return True if ``a`` and ``b`` represent the same scalar value.

    Handles the awkward fact that we typically write ``int``/``float``
    via ``async_set_property`` but read back string values from the API.
    A value of ``1`` should match ``"1"``, ``"1.0"`` and ``1.0``.
    """
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


class OptimisticConfirmedMixin:
    """Mixin that lets a writable entity ignore stale reads after a write.

    Pattern:

    1. After ``async_set_property`` succeeds, the entity calls
       :meth:`_record_optimistic` with the value it just wrote.
    2. On every coordinator refresh the entity calls
       :meth:`_consume_optimistic` with the value it just parsed from the
       API. If the API value matches the optimistic value (or the timeout
       has passed) the optimistic state is cleared and the API value
       wins. Otherwise the entity keeps showing the optimistic value and
       discards the (stale) API reading.

    This avoids the "set the switch on, see it flicker off, then back on"
    UX that happens when the next coordinator poll lands before the API
    has actually processed the write.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._optimistic_value: Any = _SENTINEL
        self._optimistic_until: Optional[datetime] = None

    def _record_optimistic(
        self, value: Any, timeout_seconds: int = OPTIMISTIC_TIMEOUT_SECONDS
    ) -> None:
        """Mark ``value`` as the expected post-write reading from the API."""
        self._optimistic_value = value
        self._optimistic_until = dt_util.utcnow() + timedelta(seconds=timeout_seconds)

    def _has_pending_optimistic(self) -> bool:
        """True if we are still inside an optimistic window."""
        if self._optimistic_value is _SENTINEL:
            return False
        if self._optimistic_until is None:
            return False
        return dt_util.utcnow() < self._optimistic_until

    def _consume_optimistic(self, api_value: Any) -> bool:
        """Decide whether ``api_value`` should override the optimistic state.

        Returns ``True`` if the caller should use ``api_value`` for its
        attribute (the optimistic state is cleared in that case).
        Returns ``False`` to keep showing the previously written value.
        """
        if self._optimistic_value is _SENTINEL:
            return True
        if self._optimistic_until is None or dt_util.utcnow() >= self._optimistic_until:
            # Timeout reached — accept whatever the API says, even if mismatched.
            self._optimistic_value = _SENTINEL
            self._optimistic_until = None
            return True
        if _values_match(self._optimistic_value, api_value):
            # Confirmed — clear and accept.
            self._optimistic_value = _SENTINEL
            self._optimistic_until = None
            return True
        # Mismatch and still within the window — keep optimistic value.
        return False
