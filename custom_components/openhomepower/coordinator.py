"""Polling coordinator for the OpenHomepower integration."""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .protocol import merge
from .registry import Reading, RegisterMap
from .transport import Credentials, Gateway, TransportError

_LOGGER = logging.getLogger(__name__)

# Consecutive failed polls tolerated before entities are marked unavailable.
# At the default 60 s interval this rides out roughly three minutes of WiFi
# trouble, which covers the observed re-association gaps.
FAILURE_GRACE = 3


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
        self.last_success: float | None = None
        self._consecutive_failures = 0

    @property
    def reading_age(self) -> float | None:
        """Seconds since the last successful read, for entity attributes."""
        if self.last_success is None:
            return None
        return round(time.monotonic() - self.last_success, 1)

    async def _async_update_data(self) -> dict[str, Reading]:
        try:
            frames = await self.gateway.read_latest()
        except TransportError as err:
            self._consecutive_failures += 1
            # This gateway drops its WiFi association routinely; a single missed
            # poll is normal, not an outage. Blanking every entity for it would
            # make the dashboard flap constantly. Ride out a few misses on the
            # last good data — which is legitimate, because the device itself
            # only refreshes every few minutes when idle — then surface the
            # failure properly. `reading_age` keeps the staleness visible so
            # old values are never silently presented as fresh.
            if self.data and self._consecutive_failures <= FAILURE_GRACE:
                _LOGGER.debug(
                    "poll %d/%d failed (%s); serving last reading, %ss old",
                    self._consecutive_failures, FAILURE_GRACE, err,
                    self.reading_age,
                )
                return self.data
            raise UpdateFailed(str(err)) from err

        self._consecutive_failures = 0
        self.last_success = time.monotonic()
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
