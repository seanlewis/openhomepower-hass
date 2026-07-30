"""Reads the writable configuration registers via MQTT so the control entities
can show the battery's current settings. Config changes rarely, so this polls
slowly (see CONTROL_SCAN_INTERVAL)."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import control
from .const import CONTROL_SCAN_INTERVAL, DOMAIN, MANUFACTURER, MODEL
from .control import MqttControl

_LOGGER = logging.getLogger(__name__)


def control_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Same device the sensors attach to, so control lands on the one card."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        name="Energizer Homepower",
        manufacturer=MANUFACTURER,
        model=MODEL,
    )


class ControlCoordinator(DataUpdateCoordinator[dict]):
    """Polls mode / max-SoC / reserve / excess over the control MQTT path."""

    def __init__(self, hass: HomeAssistant, mqtt: MqttControl) -> None:
        super().__init__(
            hass, _LOGGER, name="OpenHomepower control",
            update_interval=CONTROL_SCAN_INTERVAL,
        )
        self.mqtt = mqtt

    async def _async_update_data(self) -> dict:
        def _read() -> dict:
            mode = self.mqtt.read(control.REG_MODE, 1)[0]
            block = self.mqtt.read(control.REG_RESERVE_BLOCK, 4)   # [100, off, 2, excess]
            return {
                "mode": control.MODES_INV.get(mode, "auto"),
                "max_soc": self.mqtt.read(control.REG_MAX_SOC, 1)[0],
                "reserve_on": self.mqtt.read(control.REG_RESERVE_ON, 1)[0],
                "reserve_off": block[1],
                "excess": block[3],
            }

        try:
            return await self.hass.async_add_executor_job(_read)
        except Exception as err:  # noqa: BLE001 - report as a coordinator failure
            raise UpdateFailed(f"control read failed: {err}") from err
