"""Switch entities for Comfortzone Heat Pump integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
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

SWITCH_ENTITIES_CONFIG: dict[str, dict[str, Any]] = {
    "hot_water_extra": {
        "property_set": "SetHotWaterExtraEnabled",
        "property_read": CLEAR_TEXT_NAMES["HW_EXTRA_MODE"],
        "name": "Hot water extra",
        "icon": "mdi:water-plus",
        "api_on": 1,
        "api_off": 0,
        "read_on_value": "1",
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Comfortzone switch entities."""
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
        ComfortzoneSwitchEntity(coordinator, api_client, entry, suffix, config)
        for suffix, config in SWITCH_ENTITIES_CONFIG.items()
    ]
    async_add_entities(entities)


class ComfortzoneSwitchEntity(CoordinatorEntity, SwitchEntity):
    """Representation of a Comfortzone Switch entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        api_client: ComfortzoneApiClient,
        entry: ConfigEntry,
        entity_suffix: str,
        config: dict,
    ) -> None:
        """Initialize the switch entity."""
        super().__init__(coordinator)
        self._client = api_client
        self._config = config
        self.entry = entry
        self._entity_suffix = entity_suffix
        self._attr_unique_id = f"{device_unique_id(entry)}_{entity_suffix}"
        self._attr_name = config["name"]
        self._attr_icon = config.get("icon")
        self._attr_device_info = build_device_info(entry)
        self._api_on_value = config["api_on"]
        self._api_off_value = config["api_off"]
        self._read_on_value = config["read_on_value"]
        self._attr_is_on = None
        self._attr_available = self.coordinator.last_update_success

        if self.coordinator.data:
            self._update_state_from_coordinator()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        current_availability = self._attr_available
        current_is_on = self._attr_is_on
        self._update_state_from_coordinator()
        if (
            self._attr_is_on != current_is_on
            or self._attr_available != current_availability
        ):
            if self.hass:
                self.async_write_ha_state()

    def _update_state_from_coordinator(self) -> None:
        """Update the entity state from coordinator data."""
        new_state = self._attr_is_on
        new_availability = True

        data_block = (self.coordinator.data or {}).get("Data") or {}
        values_list = data_block.get("Values")
        if not self.coordinator.last_update_success or not isinstance(values_list, list):
            new_availability = False
        else:
            value_str = find_value_from_raw_data(
                values_list, self._config["property_read"], "ClearTextName"
            )
            if value_str is None:
                new_availability = False
            else:
                new_state = value_str == self._read_on_value

        self._attr_available = new_availability
        self._attr_is_on = new_state if new_availability else None

    async def _delayed_refresh(self, _now) -> None:
        """Request coordinator refresh after a delay."""
        if self.coordinator and self.hass:
            await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        await self._async_set_state(self._api_on_value, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        await self._async_set_state(self._api_off_value, False)

    async def _async_set_state(self, api_value: Any, expected_state: bool) -> None:
        """Send the command to set the state."""
        prop_set = self._config["property_set"]
        try:
            success = await self._client.async_set_property(prop_set, api_value)
            if success:
                self._attr_is_on = expected_state
                self.async_write_ha_state()
                async_call_later(self.hass, DELAY_REFRESH_AFTER_SET, self._delayed_refresh)
            else:
                _LOGGER.error("Failed to set %s via API", self.name)
        except (ComfortzoneApiCommandError, ComfortzoneApiClientError) as err:
            _LOGGER.error("API error setting %s: %s", self.name, err)
