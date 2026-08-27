"""Push coordinator: telemetry arrives from the MQTT reader, not a poll."""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .control import BrokerConfig
from .mqtt_reader import MqttReader, readings_from_frames
from .registry import Reading, RegisterMap
from .transport import Credentials, Gateway

_LOGGER = logging.getLogger(__name__)


class MqttReadCoordinator(DataUpdateCoordinator[dict[str, Reading]]):
    """Same data contract as HomepowerCoordinator, fed by a broker subscription.

    A `gateway` is kept (for the opt-in control path and unload symmetry) but
    telemetry never uses it — it comes from the reader.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, regmap: RegisterMap,
                 creds: Credentials, broker: BrokerConfig, stale_seconds: int) -> None:
        # A timer that only checks staleness; fresh data arrives via push.
        super().__init__(hass, _LOGGER, name="OpenHomepower (MQTT)",
                         update_interval=timedelta(seconds=stale_seconds))
        self.entry = entry
        self.regmap = regmap
        self.gateway = Gateway(creds)
        self.device_serial: str | None = None
        self.last_success: float | None = None
        self._stale_seconds = stale_seconds
        self._reader = MqttReader(broker, self._on_update)

    @property
    def reading_age(self) -> float | None:
        if self.last_success is None:
            return None
        return round(time.monotonic() - self.last_success, 1)

    def _on_update(self, frames) -> None:
        """Called from the reader thread; marshal onto the event loop."""
        readings = readings_from_frames(self.regmap, frames)
        if not readings:
            return
        self.hass.loop.call_soon_threadsafe(self._apply, readings, frames)

    @callback
    def _apply(self, readings: dict[str, Reading], frames) -> None:
        self.last_success = time.monotonic()
        if self.device_serial is None:
            serial = readings.get("device_serial")
            if serial is not None:
                self.device_serial = str(serial.value)
            elif frames:
                self.device_serial = frames[0].devsn
        self.async_set_updated_data(readings)

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

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.hass.async_add_executor_job(self._reader.stop)
        await self.gateway.close()
