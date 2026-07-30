"""Scalar control settings as number entities (opt-in control).

Max State of Charge, the two reserve limits, and excess-gen. Off-grid reserve and
excess-gen share block 120-123, so off-grid reserve writes the whole block using
the coordinator's current excess value (and vice-versa) to avoid clobbering.
"""
from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import control
from .const import DOMAIN
from .control_coordinator import ControlCoordinator, control_device_info

# key, translation_key, icon, coordinator-data key, frame builder(value, data)
NUMBERS: list[tuple] = [
    ("max_soc", "max_soc", "mdi:battery-charging-90", "max_soc",
     lambda v, d: [control.build_max_soc(v)]),
    ("reserve_on", "reserve_on_grid", "mdi:battery-arrow-down-outline", "reserve_on",
     lambda v, d: [control.build_reserve_on(v)]),
    ("reserve_off", "reserve_off_grid", "mdi:battery-alert-variant-outline", "reserve_off",
     lambda v, d: [control.build_reserve_block(v, (d or {}).get("excess", 100))]),
    ("excess", "excess_gen", "mdi:solar-power-variant-outline", "excess",
     lambda v, d: [control.build_excess(v)]),
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: ControlCoordinator | None = hass.data[DOMAIN][entry.entry_id].get("control")
    if coordinator is None:
        return
    async_add_entities(
        HomepowerNumber(coordinator, entry, *spec) for spec in NUMBERS
    )


class HomepowerNumber(CoordinatorEntity[ControlCoordinator], NumberEntity):
    """A 0-100% battery setting."""

    _attr_has_entity_name = True
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: ControlCoordinator, entry: ConfigEntry,
                 key: str, translation_key: str, icon: str, data_key: str,
                 builder: Callable) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_{key}"
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        self._data_key = data_key
        self._builder = builder
        self._attr_device_info = control_device_info(entry)

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.data or {}).get(self._data_key)

    async def async_set_native_value(self, value: float) -> None:
        v = int(value)
        for frame in self._builder(v, self.coordinator.data):
            await self.hass.async_add_executor_job(self.coordinator.mqtt.publish, frame)
        if self.coordinator.data is not None:
            self.coordinator.data[self._data_key] = v
            self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
