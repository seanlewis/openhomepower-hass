"""Re-bucketing a device daily counter to Home Assistant's local day.

The battery zeroes its own "...today" counters on its gateway clock, which never
adjusts for daylight saving, so through NZ daylight time they reset an hour late
(the same clock skew the Battery clock offset sensor reports). This re-buckets a
counter to HA's own local midnight instead, by summing the counter's increases
and starting a fresh sum at each local-day rollover.

No Home Assistant imports, so the accounting is unit-tested directly — the same
pattern as clock.py and timeshift.py.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DailyAccumulator:
    """Sums the positive steps of a resetting source counter within one day.

    `total` is energy accumulated since the last local-day reset; `last_source`
    is the previous raw counter reading, held so the next step is measured from
    it. A drop in the source (the device zeroing at its own late midnight, or a
    gateway reboot) is read as a meter reset and contributes nothing — exactly
    how Home Assistant's own utility_meter treats a source that goes backwards.
    """

    total: float = 0.0
    last_source: float | None = None

    def update(self, source: float | None) -> float | None:
        """Fold one raw reading in and return the running daily total."""
        if source is None:
            # No fresh reading: hold the total (None only before the first one).
            return self.total if self.last_source is not None else None
        if self.last_source is None:
            # First reading since start: nothing to measure a step against, so
            # begin the day's sum from here. A mid-day cold start therefore
            # undercounts the part before HA was up, as utility_meter does.
            self.last_source = source
            return self.total
        step = source - self.last_source
        self.last_source = source
        if step > 0:
            self.total += step
        return self.total

    def reset(self) -> None:
        """Start a new local day, keeping `last_source` so the step across the
        rollover is measured from the current reading, not counted from zero."""
        self.total = 0.0
