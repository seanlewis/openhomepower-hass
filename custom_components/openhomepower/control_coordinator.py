"""Coordinator for the control entities.

Reads the writable config (mode / max-SoC / reserve / excess) via a
source-specific reader: SSH entries scrape the gateway's log, MQTT entries do an
fn-03 read over the broker. Only *writes* go over MQTT (`self.mqtt`). Config
changes rarely, so this polls slowly (CONTROL_SCAN_INTERVAL)."""
from __future__ import annotations

import logging
import struct

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


class SshConfigReader:
    """Read control config from the gateway's SSH log (fn-03 holding frames)."""

    def __init__(self, gateway: Gateway) -> None:
        self._gateway = gateway

    async def read_regs(self) -> dict[int, int]:
        tokens = await self._gateway.read_holding()
        return control.parse_holding_frames(tokens)


class MqttConfigReader:
    """Read control config over MQTT (fn-03 request/response on the broker)."""

    def __init__(self, hass: HomeAssistant, mqtt: MqttControl) -> None:
        self._hass = hass
        self._mqtt = mqtt

    async def read_regs(self) -> dict[int, int]:
        return await self._hass.async_add_executor_job(self._mqtt.read_config)


class ControlCoordinator(DataUpdateCoordinator[dict]):
    """Reads config via a source-specific reader; writes go via MQTT."""

    def __init__(self, hass: HomeAssistant,
                 reader: SshConfigReader | MqttConfigReader,
                 mqtt: MqttControl) -> None:
        super().__init__(
            hass, _LOGGER, name="OpenHomepower control",
            update_interval=CONTROL_SCAN_INTERVAL,
        )
        self._reader = reader
        self.mqtt = mqtt

    async def _async_update_data(self) -> dict:
        try:
            regs = await self._reader.read_regs()
        except (TransportError, OSError, IndexError, struct.error) as err:
            # TransportError = SSH; OSError covers the MQTT reader's
            # TimeoutError / ConnectionError (both OSError subclasses).
            # IndexError/struct.error catch a malformed or short frame (e.g. a
            # nb=0 fn-03 reply, or a truncated read) from the MQTT reader.
            raise UpdateFailed(f"control read failed: {err}") from err
        # Keep the last-known value for any register not in this batch — config
        # only changes when someone writes it, so a stale-but-unchanged value is
        # still the correct value.
        state = dict(self.data or {})
        for key, value in control.control_state_from_regs(regs).items():
            if value is not None:
                state[key] = value
        return state
