"""Coordinator for the control entities.

Reads the writable config (mode / max-SoC / reserve / excess) from the same
LOCAL SSH log the sensors use — so control *state* stays visible even when the
vendor broker is down. Only *writes* go over MQTT (`self.mqtt`). Config changes
rarely, so this polls slowly (CONTROL_SCAN_INTERVAL)."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import control
from .const import CONTROL_SCAN_INTERVAL, DOMAIN, MANUFACTURER, MODEL
from .control import MqttControl
from .transport import Gateway, TransportError

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
    """Reads config over SSH (local, resilient); writes go via MQTT."""

    def __init__(self, hass: HomeAssistant, gateway: Gateway, mqtt: MqttControl) -> None:
        super().__init__(
            hass, _LOGGER, name="OpenHomepower control",
            update_interval=CONTROL_SCAN_INTERVAL,
        )
        self.gateway = gateway
        self.mqtt = mqtt

    async def _async_update_data(self) -> dict:
        try:
            tokens = await self.gateway.read_holding()
        except TransportError as err:
            raise UpdateFailed(f"control read failed: {err}") from err
        regs = control.parse_holding_frames(tokens)
        # Keep the last-known value for any register not in this batch — config
        # only changes when someone writes it, so a stale-but-unchanged value is
        # still the correct value.
        state = dict(self.data or {})
        for key, value in control.control_state_from_regs(regs).items():
            if value is not None:
                state[key] = value
        return state
