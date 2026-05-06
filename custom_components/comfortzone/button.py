"""Button entities for Comfortzone Heat Pump integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .api import (
    ComfortzoneApiClient,
    ComfortzoneApiClientError,
    ComfortzoneApiCommandError,
    find_value_from_raw_data,
)
from .const import CLEAR_TEXT_NAMES, DELAY_REFRESH_AFTER_SET, DOMAIN
from .entity import build_device_info, device_unique_id

_LOGGER = logging.getLogger(__name__)

BUTTON_ENTITIES_CONFIG: dict[str, dict[str, Any]] = {
    "reset_filter_alarm": {
        "property_set": "ResetFilterAlarm",
        "name": "Reset filter alarm",
        "icon": "mdi:filter-remove",
        "api_value": True,
        "availability_property": CLEAR_TEXT_NAMES["FILTER_ALARM"],
        "availability_active_value": "1",
    },
    "acknowledge_alarm": {
        "property_set": "AcknowledgeAlarm",
        "name": "Acknowledge alarm",
        "icon": "mdi:bell-cancel",
        "api_value": True,
        "availability_property": CLEAR_TEXT_NAMES["ALARM_TEXT"],
        "availability_active_value": None,
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Comfortzone button entities."""
    if DOMAIN not in hass.data or entry.entry_id not in hass.data[DOMAIN]:
        _LOGGER.error("Comfortzone data missing for entry %s", entry.entry_id)
        return
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DataUpdateCoordinator = data.get("coordinator")
    api_client: ComfortzoneApiClient = data.get("client")
    if not coordinator or not api_client:
        _LOGGER.error("Coordinator or API client missing for %s", entry.entry_id)
        return

    entities = [
        ComfortzoneButtonEntity(coordinator, api_client, entry, suffix, config)
        for suffix, config in BUTTON_ENTITIES_CONFIG.items()
    ]
    async_add_entities(entities)


class ComfortzoneButtonEntity(CoordinatorEntity, ButtonEntity):
    """Representation of a Comfortzone Button entity."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        api_client: ComfortzoneApiClient,
        entry: ConfigEntry,
        entity_suffix: str,
        config: dict,
    ) -> None:
        """Initialize the button entity."""
        super().__init__(coordinator)
        self._client = api_client
        self._config = config
        self.entry = entry
        self._entity_suffix = entity_suffix
        self._attr_unique_id = f"{device_unique_id(entry)}_{entity_suffix}"
        self._attr_name = config["name"]
        self._attr_icon = config.get("icon")
        self._attr_device_info = build_device_info(entry)
        self._api_value = config["api_value"]
        self._availability_property = config.get("availability_property")
        self._availability_active_value = config.get("availability_active_value")
        self._update_availability()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator to update availability."""
        current_availability = self._attr_available
        self._update_availability()
        if self._attr_available != current_availability:
            if self.hass:
                self.async_write_ha_state()

    def _update_availability(self) -> None:
        """Update entity availability based on coordinator data."""
        new_availability = True

        if self._availability_property:
            data_block = (self.coordinator.data or {}).get("Data") or {}
            values_list = data_block.get("Values")
            if not self.coordinator.last_update_success or not isinstance(values_list, list):
                new_availability = False
            else:
                availability_value_str = find_value_from_raw_data(
                    values_list, self._availability_property, "ClearTextName"
                )
                if availability_value_str is None:
                    new_availability = False
                elif self._availability_property == CLEAR_TEXT_NAMES["ALARM_TEXT"]:
                    new_availability = availability_value_str != ""
                else:
                    new_availability = availability_value_str == self._availability_active_value
        else:
            new_availability = self.coordinator.last_update_success

        self._attr_available = new_availability

    async def _delayed_refresh(self, _now) -> None:
        """Request coordinator refresh after a delay."""
        if self.coordinator and self.hass:
            await self.coordinator.async_request_refresh()

    async def async_press(self) -> None:
        """Handle the button press."""
        if not self._attr_available:
            _LOGGER.warning("Attempted to press unavailable button: %s", self.name)
            return
        prop_set = self._config["property_set"]
        try:
            success = await self._client.async_set_property(prop_set, self._api_value)
            if success:
                async_call_later(self.hass, DELAY_REFRESH_AFTER_SET, self._delayed_refresh)
            else:
                _LOGGER.error("Failed to activate action %s via API", self.name)
        except (ComfortzoneApiCommandError, ComfortzoneApiClientError) as err:
            _LOGGER.error("API error activating %s: %s", self.name, err)
