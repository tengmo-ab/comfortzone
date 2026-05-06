"""Shared helpers for the Comfortzone integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONF_DEVICE_ID, CONF_MODEL, DOMAIN


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return a consistent DeviceInfo for all platforms in this integration."""
    device_id = entry.data.get(CONF_DEVICE_ID)
    model = entry.data.get(CONF_MODEL, "RX95")
    return DeviceInfo(
        identifiers={(DOMAIN, str(device_id) if device_id is not None else entry.entry_id)},
        manufacturer="Comfortzone",
        model=model,
        name=f"Comfortzone {model}",
        configuration_url="https://platform.loggamera.se",
    )


def device_unique_id(entry: ConfigEntry) -> str:
    """Return the stable unique-id base for entities under this entry."""
    device_id = entry.data.get(CONF_DEVICE_ID)
    return str(device_id) if device_id is not None else entry.entry_id
