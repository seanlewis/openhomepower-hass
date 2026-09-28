"""Shift a weekly schedule between real (local) time and the battery's own clock.

The battery runs its schedule by an internal clock that was set once and never
adjusts for daylight saving or drift. Home Assistant measures how far that clock
is off (see clock.py) and shifts every window by that many minutes on the way to
the battery, and back again on the way out, so people only ever see real times.

Shifting can push a window across midnight or across the week boundary (Sunday
into Monday); such a window is split at midnight, and pieces that meet end-to-end
with the same power are merged. The battery can't store "until midnight", so a
split piece ends at 23:59; an end of 23:59 is read as midnight only when the same
type of window, at the same power, carries on at 00:00 the next day. That keeps
the round trip exact, with one exception: an end that lands exactly on
midnight after shifting is stored as 23:59, so it comes back a minute short.

No Home Assistant imports, so this is unit-tested directly.
"""
from __future__ import annotations

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CATEGORIES = ("grid_charge", "pv_charge", "discharge")
DAY = 24 * 60
WEEK = 7 * DAY
MAX_WINDOWS = 2


def _minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")[:2]
    return int(hour) * 60 + int(minute)


def _continues(sched: dict, day_index: int, cat: str, power: int) -> bool:
    """Does the next day start with the same window type and power at 00:00?"""
    nxt = (sched.get(DAYS[(day_index + 1) % 7]) or {}).get(cat) or []
    return any(_minutes(w["start"]) == 0 and int(w["power"]) == power for w in nxt)


def _end_minutes(sched: dict, day_index: int, cat: str, win: dict) -> int:
    value = _minutes(win["end"])
    if value == DAY - 1 and _continues(sched, day_index, cat, int(win["power"])):
        return DAY                                # a midnight split: "until midnight"
    return value


def _fmt(minutes: int) -> str:
    minutes = min(minutes, DAY - 1)               # midnight end -> 23:59
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def shift_schedule(sched: dict, minutes: int) -> tuple[dict, list[str]]:
    """Move every window by `minutes` (positive = later). Returns (schedule,
    problems); problems name each day/type that would need more than two
    windows, in which case the schedule can't be stored as-is."""
    minutes = int(minutes)
    # (cat, day index) -> [[start, end, power], ...] in minutes within the day
    pieces: dict[tuple[str, int], list[list[int]]] = {}
    for di, day in enumerate(DAYS):
        for cat in CATEGORIES:
            for win in (sched.get(day) or {}).get(cat) or []:
                start = di * DAY + _minutes(win["start"])
                end = di * DAY + _end_minutes(sched, di, cat, win)
                if end <= start:
                    continue
                length = end - start
                start = (start + minutes) % WEEK
                end = start + length
                while start < end:                 # split at each midnight
                    d = start // DAY
                    seg_end = min(end, (d + 1) * DAY)
                    pieces.setdefault((cat, d % 7), []).append(
                        [start - d * DAY, seg_end - d * DAY, int(win["power"])])
                    start = seg_end

    out: dict = {}
    problems: list[str] = []
    for di, day in enumerate(DAYS):
        for cat in CATEGORIES:
            segs = sorted(pieces.get((cat, di), []))
            merged: list[list[int]] = []
            for seg in segs:
                if merged and merged[-1][1] == seg[0] and merged[-1][2] == seg[2]:
                    merged[-1][1] = seg[1]
                else:
                    merged.append(list(seg))
            if not merged:
                continue
            if len(merged) > MAX_WINDOWS:
                problems.append(f"{day} {cat}")
            out.setdefault(day, {})[cat] = [
                {"start": _fmt(s), "end": _fmt(e), "power": p} for s, e, p in merged]
    return out, problems
