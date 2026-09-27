"""Convert between the battery's weekly schedule and the Configure → Schedule form.

The battery stores up to two windows per category per day, each day independent.
The form is simpler: per category, two windows, each with one start/end/power and
the days it applies to. Most real schedules ("charge 2-5 am every day") fit that
exactly. One that doesn't (more than two distinct windows in a category, set via
the action) is flagged so the form can warn before replacing it.

No Home Assistant imports, so this is unit-tested directly.
"""
from __future__ import annotations

from collections import Counter

CATEGORIES = ("grid_charge", "pv_charge", "discharge")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SLOTS = (1, 2)
LABELS = {"grid_charge": "Grid charge", "pv_charge": "Solar charge",
          "discharge": "Discharge"}


def field(cat: str, slot: int) -> str:
    """Section key for one window, e.g. 'grid_charge_1'."""
    return f"{cat}_{slot}"


def window_label(key: str) -> str:
    """'grid_charge_1' -> 'Grid charge window 1'."""
    cat, slot = key.rsplit("_", 1)
    return f"{LABELS[cat]} window {slot}"


def _hhmm(value: str) -> str:
    """'02:00' or '02:00:00' (HA's time selector) -> '02:00'."""
    hour, minute = value.split(":")[:2]
    return f"{int(hour):02d}:{int(minute):02d}"


def _minutes(value: str) -> int:
    hour, minute = _hhmm(value).split(":")
    return int(hour) * 60 + int(minute)


def schedule_to_form(sched: dict) -> tuple[dict, bool]:
    """Schedule JSON -> (form values per window, fits_exactly).

    Distinct (start, end, power) windows in a category become the form's two
    windows, most-used first, each with the days it runs on. fits_exactly is
    False when a category has more than two distinct windows; the form then
    shows the two most-used, and saving would drop the rest.
    """
    values: dict = {}
    fits = True
    for cat in CATEGORIES:
        days_for: dict[tuple, list[str]] = {}
        for day in DAYS:
            for win in (sched.get(day) or {}).get(cat) or []:
                key = (_hhmm(win["start"]), _hhmm(win["end"]), int(win["power"]))
                days_for.setdefault(key, []).append(day)
        ranked = sorted(days_for.items(),
                        key=lambda kv: (-len(kv[1]), DAYS.index(kv[1][0]), kv[0]))
        if len(ranked) > len(SLOTS):
            fits = False
        for slot in SLOTS:
            if slot <= len(ranked):
                (start, end, power), days = ranked[slot - 1]
                values[field(cat, slot)] = {
                    "enabled": True, "start": start, "end": end,
                    "power": power, "days": list(days)}
            else:
                values[field(cat, slot)] = {
                    "enabled": False, "start": "00:00", "end": "00:00",
                    "power": 100, "days": list(DAYS)}
    return values, fits


def form_to_schedule(values: dict) -> tuple[dict, dict[str, str]]:
    """Form values -> (schedule JSON, errors keyed by section).

    Errors: 'end_before_start' (includes windows crossing midnight, which must
    be split in two), 'no_days', 'overlap' (both windows of a category on the
    same day, overlapping).
    """
    sched: dict = {}
    errors: dict[str, str] = {}
    for cat in CATEGORIES:
        spans: dict[str, list[tuple[int, int]]] = {}
        for slot in SLOTS:
            key = field(cat, slot)
            win = values.get(key) or {}
            if not win.get("enabled"):
                continue
            start, end = _hhmm(win["start"]), _hhmm(win["end"])
            s, e = _minutes(start), _minutes(end)
            days = [d for d in DAYS if d in (win.get("days") or [])]
            if e <= s:
                errors[key] = "end_before_start"
                continue
            if not days:
                errors[key] = "no_days"
                continue
            for day in days:
                if any(s < oe and os < e for os, oe in spans.get(day, [])):
                    errors[key] = "overlap"
                    break
                spans.setdefault(day, []).append((s, e))
            else:
                for day in days:
                    sched.setdefault(day, {}).setdefault(cat, []).append(
                        {"start": start, "end": end, "power": int(win["power"])})
    # Keep each day's windows in time order, as the battery shows them.
    for cats in sched.values():
        for wins in cats.values():
            wins.sort(key=lambda w: _minutes(w["start"]))
    return sched, errors


def normalise(sched: dict) -> dict:
    """Canonical form for comparing two schedules (order-insensitive)."""
    out: dict = {}
    for day in DAYS:
        for cat in CATEGORIES:
            wins = (sched.get(day) or {}).get(cat) or []
            if wins:
                out.setdefault(day, {})[cat] = sorted(
                    ((_hhmm(w["start"]), _hhmm(w["end"]), int(w["power"])) for w in wins))
    return out


def mixed_categories(sched: dict) -> list[str]:
    """Categories with more than two distinct windows (can't be shown exactly)."""
    out = []
    for cat in CATEGORIES:
        distinct = Counter(
            (_hhmm(w["start"]), _hhmm(w["end"]), int(w["power"]))
            for day in DAYS for w in (sched.get(day) or {}).get(cat) or [])
        if len(distinct) > len(SLOTS):
            out.append(cat)
    return out
