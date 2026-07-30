"""Application-mode select (opt-in control)."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import control
from .const import DOMAIN
from .control_coordinator import ControlCoordinator, control_device_info


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: ControlCoordinator | None = hass.data[DOMAIN][entry.entry_id].get("control")
    if coordinator is None:      # control not enabled — nothing to add
        return
    async_add_entities([HomepowerModeSelect(coordinator, entry)])


class HomepowerModeSelect(CoordinatorEntity[ControlCoordinator], SelectEntity):
    """Automatic / Semi-automatic / Manual."""

    _attr_has_entity_name = True
    _attr_translation_key = "application_mode"
    _attr_icon = "mdi:home-lightning-bolt-outline"
    _attr_options = ["auto", "semi", "manual"]

    def __init__(self, coordinator: ControlCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_mode"
        self._attr_device_info = control_device_info(entry)

    @property
    def current_option(self) -> str | None:
        return (self.coordinator.data or {}).get("mode")

    async def async_select_option(self, option: str) -> None:
        await self.hass.async_add_executor_job(
            self.coordinator.mqtt.publish, control.build_mode(option)
        )
        if self.coordinator.data is not None:      # optimistic, then confirm
            self.coordinator.data["mode"] = option
            self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
