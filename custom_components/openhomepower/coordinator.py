"""Polling coordinator for the OpenHomepower integration."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .protocol import merge
from .registry import Reading, RegisterMap
from .transport import Credentials, Gateway, TransportError

_LOGGER = logging.getLogger(__name__)


class HomepowerCoordinator(DataUpdateCoordinator[dict[str, Reading]]):
    """Fetches and decodes one register bank per interval.

    Read-only: the underlying transport can only tail the vendor daemon's log.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry,
                 regmap: RegisterMap, creds: Credentials,
                 poll_seconds: int) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="OpenHomepower",
            update_interval=timedelta(seconds=poll_seconds),
        )
        self.entry = entry
        self.regmap = regmap
        self.gateway = Gateway(creds)
        self.device_serial: str | None = None

    async def _async_update_data(self) -> dict[str, Reading]:
        try:
            frames = await self.gateway.read_latest()
        except TransportError as err:
            # The gateway periodically re-associates to its AP, which kills any
            # in-flight session. The transport already retries; if it still
            # failed, report it and let HA mark entities unavailable.
            raise UpdateFailed(str(err)) from err

        readings = self.regmap.decode(merge(frames))
        if not readings:
            raise UpdateFailed("no readings decoded from the register bank")

        if self.device_serial is None:
            serial = readings.get("device_serial")
            if serial is not None:
                self.device_serial = str(serial.value)
            elif frames:
                self.device_serial = frames[0].devsn
        return readings

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.gateway.close()
