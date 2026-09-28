"""Work out how far the battery's own clock is from real time.

The battery reports its clock (the Device clock sensor) as local wall time, but
nothing ever adjusts it for daylight saving or drift. Each telemetry reading
gives one sample of "battery clock minus UTC"; measuring against UTC (not local
time) keeps samples valid across a daylight-saving change, because it's only the
local offset that jumps, never the battery's clock.

A reading can be stale: on SSH setups the gateway only reads the battery every
few minutes, so a sample taken later makes the clock look behind by the age of
the reading. The freshest sample in a window is therefore the best one, so the
estimate is the MAXIMUM over recent samples.

No Home Assistant imports, so this is unit-tested directly.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, tzinfo

WINDOW_SECONDS = 30 * 60        # samples considered for the estimate
MIN_SAMPLES = 3                 # before trusting an estimate
MAX_OFFSET_MINUTES = 24 * 60    # beyond this the clock was never set; don't shift


def parse_device_time(value: object) -> datetime | None:
    """'2026-09-28 08:03:54' -> naive datetime (battery wall time), or None."""
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def battery_minus_utc(battery: datetime, utc_now: datetime) -> float:
    """Minutes by which the battery's wall clock is ahead of UTC."""
    return (battery - utc_now.replace(tzinfo=None)).total_seconds() / 60


def local_offset(battery_vs_utc: float, tz: tzinfo, utc_now: datetime) -> int | None:
    """Minutes the battery clock is ahead (+) or behind (-) real local time,
    rounded to the minute; None if implausibly large (clock never set)."""
    local_vs_utc = tz.utcoffset(utc_now.replace(tzinfo=None)) or timedelta()
    minutes = round(battery_vs_utc - local_vs_utc.total_seconds() / 60)
    return minutes if abs(minutes) <= MAX_OFFSET_MINUTES else None


def describe(minutes: int | None) -> str:
    """Human wording, e.g. '1 h 0 min behind', '3 min ahead', 'on time'."""
    if minutes is None:
        return "unknown"
    if minutes == 0:
        return "on time"
    size = abs(minutes)
    text = f"{size // 60} h {size % 60} min" if size >= 60 else f"{size} min"
    return f"{text} {'ahead' if minutes > 0 else 'behind'}"


class OffsetEstimator:
    """Keeps recent 'battery minus UTC' samples and estimates the true value."""

    def __init__(self, window: float = WINDOW_SECONDS, min_samples: int = MIN_SAMPLES):
        self._window = window
        self._min = min_samples
        self._samples: deque[tuple[float, float]] = deque()
        self._last_value: object = None

    def add(self, device_time: object, utc_now: datetime, now: float) -> None:
        """Record a reading. A repeat of the last clock value is the same stale
        reading served again, so it's ignored rather than counted."""
        if device_time == self._last_value:
            return
        battery = parse_device_time(device_time)
        if battery is None:
            return
        self._last_value = device_time
        self._samples.append((now, battery_minus_utc(battery, utc_now)))
        self._trim(now)

    def _trim(self, now: float) -> None:
        while self._samples and now - self._samples[0][0] > self._window:
            self._samples.popleft()

    def estimate(self, now: float) -> float | None:
        """Battery-minus-UTC minutes, or None until there are enough samples."""
        self._trim(now)
        if len(self._samples) < self._min:
            return None
        return max(value for _, value in self._samples)
