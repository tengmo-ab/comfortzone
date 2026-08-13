"""Diagnostics support for Comfortzone Heat Pump."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DEVICE_ID,
    DOMAIN,
    FAN_MODE_READ_CANDIDATES,
    FAN_SPEED_PROPERTY_CANDIDATES,
)

TO_REDACT = {CONF_API_KEY, CONF_DEVICE_ID, "ApiKey", "DeviceId"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = data.get("coordinator")
    client = data.get("client")

    # Which fan-speed field/property this pump actually answered to. The names
    # are probed at runtime, so capturing the outcome here is what makes a
    # user's diagnostics dump useful for confirming the mapping.
    values = ((coordinator.data or {}).get("Data") or {}).get("Values") if coordinator else None
    fan_fields = (
        {
            item.get("ClearTextName"): item.get("Value")
            for item in values
            if isinstance(item, dict) and item.get("ClearTextName") in FAN_MODE_READ_CANDIDATES
        }
        if isinstance(values, list)
        else {}
    )

    return {
        "fan_speed": {
            "read_candidates_present": fan_fields,
            "resolved_write_property": client.resolved_property(
                FAN_SPEED_PROPERTY_CANDIDATES
            )
            if client
            else None,
        },
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "coordinator": {
            "last_update_success": getattr(coordinator, "last_update_success", None),
            "data": async_redact_data(coordinator.data, TO_REDACT)
            if coordinator and coordinator.data
            else None,
        },
    }
