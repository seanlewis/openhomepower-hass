"""Push coordinator: telemetry arrives from the MQTT reader, not a poll."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .control import BrokerConfig
from .mqtt_reader import MqttReader
from .registry import Reading, RegisterMap
from .transport import Credentials, Gateway

_LOGGER = logging.getLogger(__name__)


class MqttReadCoordinator(DataUpdateCoordinator[dict[str, Reading]]):
    """Same data contract as HomepowerCoordinator, fed by a broker subscription.

    A `gateway` is kept only for SSH entries; in SSH-free MQTT mode it is None
    and control read-back uses the broker.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, regmap: RegisterMap,
                 creds: Credentials | None, broker: BrokerConfig, stale_seconds: int) -> None:
        # A timer that only checks staleness; fresh data arrives via push.
        # Sample several times per staleness window so entities go unavailable
        # close to stale_seconds after telemetry stops, not at ~2x it. The
        # deadline itself stays stale_seconds (see _async_update_data).
        watchdog_interval = max(15, stale_seconds // 3)
        super().__init__(hass, _LOGGER, name="OpenHomepower (MQTT)",
                         update_interval=timedelta(seconds=watchdog_interval))
        self.entry = entry
        self.regmap = regmap
        # SSH-free MQTT mode passes creds=None: no gateway is built and control
        # read-back rides the broker instead.
        self.gateway: Gateway | None = Gateway(creds) if creds is not None else None
        self.device_serial: str | None = None
        self.last_success: float | None = None
        self._stale_seconds = stale_seconds
        self._reader = MqttReader(broker, self._on_update)
        self._first_data = asyncio.Event()

    @property
    def reading_age(self) -> float | None:
        if self.last_success is None:
            return None
        return round(time.monotonic() - self.last_success, 1)

    def _on_update(self, regs: dict[int, int]) -> None:
        """Called from the reader thread; marshal onto the event loop.

        `regs` is the raw input-register map the reader decoded from one telemetry
        reply; the registry turns it into readings.
        """
        readings = self.regmap.decode(regs)
        if not readings:
            return
        self.hass.loop.call_soon_threadsafe(self._apply, readings)

    @callback
    def _apply(self, readings: dict[str, Reading]) -> None:
        self.last_success = time.monotonic()
        if self.device_serial is None:
            serial = readings.get("device_serial")
            if serial is not None:
                self.device_serial = str(serial.value)
        self.async_set_updated_data(readings)
        self._first_data.set()

    async def _async_update_data(self) -> dict[str, Reading]:
        """Staleness watchdog only — real updates come from _apply()."""
        if self.data and self.reading_age is not None \
                and self.reading_age <= self._stale_seconds:
            return self.data
        if self.data:
            raise UpdateFailed(f"no telemetry for {self.reading_age}s")
        raise UpdateFailed("no telemetry received yet")

    async def async_start(self) -> None:
        await self.hass.async_add_executor_job(self._reader.start)

    async def async_await_first_data(self, timeout: float) -> bool:
        """Block until the first telemetry push populates us, or timeout.

        Setup uses this instead of async_config_entry_first_refresh(): a
        just-started reader has no data yet, and the staleness watchdog would
        otherwise fail the first refresh (leaking the reader thread across HA's
        setup retries). Returns True once data has arrived.
        """
        try:
            await asyncio.wait_for(self._first_data.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.hass.async_add_executor_job(self._reader.stop)
        if self.gateway is not None:
            await self.gateway.close()
