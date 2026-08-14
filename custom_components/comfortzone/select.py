"""Select entities for the Comfortzone Heat Pump integration.

Currently this platform exposes the fan speed / ventilation mode, which the
Android app can change but which is absent from every public description of
the Loggamera API. See ``const.FAN_MODE_OPTIONS`` for the value mapping and
``ComfortzoneFanSpeedSelect`` for how the property name is discovered.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.components.select import SelectEntity
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
from .const import (
    CONF_FAN_SPEED_PROPERTY,
    DELAY_REFRESH_AFTER_SET,
    DELAY_REFRESH_FOLLOWUP,
    DOMAIN,
    FAN_MODE_OPTIONS,
    FAN_MODE_READ_CANDIDATES,
    FAN_MODE_SCHEDULE,
    FAN_MODE_VALUE_TO_OPTION,
    FAN_MODE_WRITE_VALUES,
    FAN_SPEED_PROPERTY_CANDIDATES,
)
from .entity import OptimisticConfirmedMixin, build_device_info, device_unique_id

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Comfortzone select entities."""
    if DOMAIN not in hass.data or entry.entry_id not in hass.data[DOMAIN]:
        _LOGGER.error("Comfortzone data missing for entry %s", entry.entry_id)
        return
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DataUpdateCoordinator = data.get("coordinator")
    api_client: ComfortzoneApiClient = data.get("client")
    if not coordinator or not api_client:
        _LOGGER.error("Coordinator or API client missing for %s", entry.entry_id)
        return

    async_add_entities([ComfortzoneFanSpeedSelect(coordinator, api_client, entry)])


def _read_fan_mode(values: Optional[list[Any]]) -> tuple[Optional[int], Optional[str]]:
    """Return ``(mode, clear_text_name)`` for the configured fan mode.

    Walks :data:`FAN_MODE_READ_CANDIDATES` and returns the first field that
    parses to an integer in the valid 1-4 range. Anything outside that range
    is a different quantity that happens to share a similar name (for example
    "Fan speed (current)", which is a duty-cycle percentage), so it is
    skipped rather than misinterpreted.
    """
    if not values:
        return None, None
    for clear_text_name in FAN_MODE_READ_CANDIDATES:
        raw = find_value_from_raw_data(values, clear_text_name)
        if raw is None:
            continue
        try:
            # The API reports integers as "2" on some firmwares and "2.0" on
            # others, so parse as float first and then narrow.
            mode = int(float(raw))
        except (TypeError, ValueError):
            continue
        if mode in FAN_MODE_VALUE_TO_OPTION:
            return mode, clear_text_name
    return None, None


class ComfortzoneFanSpeedSelect(OptimisticConfirmedMixin, CoordinatorEntity, SelectEntity):
    """Fan speed selector: low / normal / boost / scheduled.

    Two things about this entity are discovered at runtime rather than known
    up front, because Loggamera documents neither:

    * **Which ClearTextName carries the setting.** Resolved by
      :func:`_read_fan_mode` against a candidate list.
    * **Which SetProperty name writes it, and in what value vocabulary.**
      Both are resolved together on the first write by
      :meth:`ComfortzoneApiClient.async_set_first_supported_combination`,
      since the API rejects a bad value and a bad name identically. The name
      can be pinned with the ``fan_speed_property`` option.

    Both resolutions are surfaced as state attributes so a user can report
    what their pump actually answered to.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:fan"
    # Name and option labels come from the entity translations so the Swedish
    # UI reads "Fläkthastighet" / "Schemalagd (automatik)".
    _attr_translation_key = "fan_speed"
    _attr_options = list(FAN_MODE_OPTIONS)

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        api_client: ComfortzoneApiClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the fan speed select entity."""
        super().__init__(coordinator)
        self._client = api_client
        self.entry = entry
        self._attr_unique_id = f"{device_unique_id(entry)}_fan_speed"
        self._attr_device_info = build_device_info(entry)
        self._attr_current_option = None
        self._read_field: Optional[str] = None
        # A pump on the older 1.6 protocol rejects the scheduled mode. We only
        # learn that by trying, so remember it and stop offering the option.
        self._schedule_supported = True
        self._attr_available = self.coordinator.last_update_success
        if self.coordinator.data:
            self._update_state_from_coordinator()

    @property
    def options(self) -> list[str]:
        """Return the selectable modes, hiding scheduled mode if unsupported."""
        if self._schedule_supported:
            return list(FAN_MODE_OPTIONS)
        return [
            option
            for option, value in FAN_MODE_OPTIONS.items()
            if value != FAN_MODE_SCHEDULE
        ]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose what the runtime probing resolved, for troubleshooting."""
        configured = self.entry.options.get(CONF_FAN_SPEED_PROPERTY)
        names = (configured,) if configured else FAN_SPEED_PROPERTY_CANDIDATES
        resolved = self._client.resolved_property(names)
        style = self._client.resolved_value_style(names)
        return {
            "read_field": self._read_field,
            "write_property": resolved,
            "write_property_source": "option" if configured else "auto-detected",
            "write_supported": bool(resolved),
            "write_value_form": (
                None if style is None else ("string" if style == 0 else "integer")
            ),
            "candidates_exhausted": self._client.probe_exhausted(names),
            "scheduled_mode_supported": self._schedule_supported,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        previous_option = self._attr_current_option
        previous_availability = self._attr_available
        self._update_state_from_coordinator()
        if (
            self._attr_current_option != previous_option
            or self._attr_available != previous_availability
        ):
            if self.hass:
                self.async_write_ha_state()

    def _update_state_from_coordinator(self) -> None:
        """Update the current option from coordinator data."""
        data_block = (self.coordinator.data or {}).get("Data") or {}
        values_list = data_block.get("Values")
        if not self.coordinator.last_update_success or not isinstance(values_list, list):
            self._attr_available = False
            self._attr_current_option = None
            return

        mode, read_field = _read_fan_mode(values_list)
        self._read_field = read_field

        if mode is None:
            # The pump doesn't report the setting back. Keep whatever the user
            # last wrote so the control still works as a write-only selector
            # rather than collapsing to "unavailable".
            self._attr_available = True
            return

        if self._consume_optimistic(mode):
            self._attr_current_option = FAN_MODE_VALUE_TO_OPTION.get(mode)
        self._attr_available = True

    async def _delayed_refresh(self, _now) -> None:
        """Request coordinator refresh after a delay."""
        if self.coordinator and self.hass:
            await self.coordinator.async_request_refresh()

    async def async_select_option(self, option: str) -> None:
        """Write the selected fan mode to the pump."""
        value = FAN_MODE_OPTIONS.get(option)
        if value is None:
            _LOGGER.error("Unknown fan speed option '%s'", option)
            return

        # Loggamera support says the property is SetFanState and takes string
        # values (Low / Normal / High), while the pump reports the mode back
        # as the integer 1-4. Try the documented string first, then the
        # integer, since neither can be inferred from a rejection.
        write_values = FAN_MODE_WRITE_VALUES.get(option, (value,))
        configured = self.entry.options.get(CONF_FAN_SPEED_PROPERTY)
        names = (configured,) if configured else FAN_SPEED_PROPERTY_CANDIDATES
        try:
            accepted = await self._client.async_set_first_supported_combination(
                names, write_values
            )
        except (ComfortzoneApiCommandError, ComfortzoneApiClientError) as err:
            _LOGGER.error("API error setting fan speed: %s", err)
            return

        if accepted is None:
            # A rejection only says "pre-1.8 pump" if we already know the
            # property name is good — otherwise the write failed because the
            # name is wrong, which says nothing about the pump's protocol.
            name_is_known = bool(
                self._client.resolved_property(names)
            )
            if name_is_known and value == FAN_MODE_SCHEDULE and self._schedule_supported:
                # Every other mode is accepted by both protocol generations, so
                # a rejection that only affects mode 4 points at a pre-1.8 pump.
                _LOGGER.warning(
                    "Scheduled fan mode was rejected while other modes work — "
                    "this pump most likely runs the older 1.6 control protocol, "
                    "which only supports fan speeds 1-3. Hiding the option."
                )
                self._schedule_supported = False
                self.async_write_ha_state()
            else:
                _LOGGER.error(
                    "Failed to set fan speed to '%s'. Tried %s with values %s. "
                    "Run scripts/probe_loggamera_properties.py --probe-values to "
                    "find what your pump accepts, then set the name via the '%s' "
                    "option. Reading the current mode continues to work.",
                    option,
                    ", ".join(names),
                    ", ".join(repr(v) for v in write_values),
                    CONF_FAN_SPEED_PROPERTY,
                )
            return

        self._attr_current_option = option
        self._record_optimistic(value)
        self.async_write_ha_state()
        async_call_later(self.hass, DELAY_REFRESH_AFTER_SET, self._delayed_refresh)
        async_call_later(self.hass, DELAY_REFRESH_FOLLOWUP, self._delayed_refresh)
