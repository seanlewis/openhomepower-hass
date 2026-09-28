"""Tracks the battery clock's offset from real time, from telemetry readings.

Feeds every Device clock reading into clock.OffsetEstimator and turns the
estimate into "minutes ahead (+) / behind (-) real local time" using the
configured time zone (Home Assistant's unless overridden).
"""
from __future__ import annotations

import time

from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import clock


class ClockTracker:
    def __init__(self, coordinator: DataUpdateCoordinator, tz_name: str = "") -> None:
        self._coordinator = coordinator
        self._tz_name = (tz_name or "").strip()
        self.estimator = clock.OffsetEstimator()
        self._unsub = coordinator.async_add_listener(self._on_update)
        self._on_update()

    @property
    def tz(self):
        return (dt_util.get_time_zone(self._tz_name) if self._tz_name else None) \
            or dt_util.get_default_time_zone()

    @callback
    def _on_update(self) -> None:
        reading = (self._coordinator.data or {}).get("device_time")
        if reading is not None:
            self.estimator.add(reading.value, dt_util.utcnow(), time.monotonic())

    def offset(self) -> int | None:
        """Minutes the battery clock is ahead (+) or behind (-), or None if
        there aren't enough readings yet (or the clock was never set)."""
        vs_utc = self.estimator.estimate(time.monotonic())
        if vs_utc is None:
            return None
        return clock.local_offset(vs_utc, self.tz, dt_util.utcnow())

    def close(self) -> None:
        self._unsub()
