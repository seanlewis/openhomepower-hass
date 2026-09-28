"""Real time <-> battery clock shifting, and the clock-offset estimator."""
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from openhomepower import clock
from openhomepower import schedule_form as sf
from openhomepower.timeshift import DAYS, shift_schedule

NZ = ZoneInfo("Pacific/Auckland")


def _w(start, end, power=100):
    return {"start": start, "end": end, "power": power}


def _every_day(**cats):
    return {d: dict(cats) for d in DAYS}


# --- shifting ----------------------------------------------------------------
def test_zero_shift_is_identity():
    sched = _every_day(grid_charge=[_w("02:00", "05:00")], discharge=[_w("14:00", "21:00")])
    assert shift_schedule(sched, 0) == (sched, [])


def test_guy_battery_one_hour_behind():
    # Real 04:00-07:00 on a battery whose clock is 60 min behind -> 03:00-06:00.
    real = _every_day(grid_charge=[_w("04:00", "07:00")])
    battery, problems = shift_schedule(real, -60)
    assert not problems
    assert battery["mon"]["grid_charge"] == [_w("03:00", "06:00")]
    assert shift_schedule(battery, 60)[0] == real


def test_split_across_midnight_and_back():
    real = {"tue": {"grid_charge": [_w("00:30", "02:00")]}}
    battery, problems = shift_schedule(real, -60)
    assert not problems
    assert battery == {"mon": {"grid_charge": [_w("23:30", "23:59")]},
                       "tue": {"grid_charge": [_w("00:00", "01:00")]}}
    assert shift_schedule(battery, 60)[0] == real


def test_week_wraps_sunday_to_monday():
    real = {"sun": {"discharge": [_w("23:30", "23:59")]}}
    battery, _ = shift_schedule(real, 60)
    assert battery == {"mon": {"discharge": [_w("00:30", "00:59")]}}
    assert shift_schedule(battery, -60)[0] == real
    real = {"mon": {"pv_charge": [_w("00:00", "01:00")]}}
    assert shift_schedule(real, -60)[0] == {"sun": {"pv_charge": [_w("23:00", "23:59")]}}


def test_midnight_split_windows_merge_when_shifted():
    # A real 22:00-02:00 charge, entered as two windows either side of midnight,
    # becomes one battery window when the shift lines it up inside one day.
    real = {"mon": {"grid_charge": [_w("22:00", "23:59")]},
            "tue": {"grid_charge": [_w("00:00", "02:00")]}}
    battery, problems = shift_schedule(real, 120)
    assert not problems
    assert battery == {"tue": {"grid_charge": [_w("00:00", "04:00")]}}
    assert shift_schedule(battery, -120)[0] == real


def test_different_power_pieces_do_not_merge():
    real = {"mon": {"discharge": [_w("16:00", "18:00", 100), _w("18:00", "20:00", 50)]}}
    battery, _ = shift_schedule(real, 30)
    assert battery["mon"]["discharge"] == [_w("16:30", "18:30", 100), _w("18:30", "20:30", 50)]


def test_too_many_windows_after_shift_is_reported():
    # Two windows on Tuesday plus one near Monday midnight: shifting puts three
    # pieces on Tuesday.
    real = {"mon": {"grid_charge": [_w("23:00", "23:59")]},
            "tue": {"grid_charge": [_w("03:00", "04:00"), _w("06:00", "07:00")]}}
    _, problems = shift_schedule(real, 30)
    assert problems == ["tue grid_charge"]


def test_minute_drift():
    real = {"wed": {"discharge": [_w("17:00", "21:00")]}}
    assert shift_schedule(real, 7)[0] == {"wed": {"discharge": [_w("17:07", "21:07")]}}


def test_random_round_trips():
    rng = random.Random(7)
    for _ in range(500):
        sched = {}
        for day in DAYS:
            for cat in ("grid_charge", "pv_charge", "discharge"):
                if rng.random() < 0.4:
                    s = rng.randrange(0, 22 * 60)
                    e = rng.randrange(s + 1, 24 * 60)
                    sched.setdefault(day, {})[cat] = [_w(f"{s // 60:02d}:{s % 60:02d}",
                                                         f"{e // 60:02d}:{e % 60:02d}",
                                                         rng.choice([50, 100]))]
        offset = rng.choice([-60, 60, -7, 13, 0, -120])
        battery, problems = shift_schedule(sched, offset)
        if problems:
            continue
        back, _ = shift_schedule(battery, -offset)
        assert _close(sf.normalise(back), sf.normalise(sched)), (sched, offset)


def _close(a, b):
    """Equal, except an end may be one minute short: a shifted end that lands
    exactly on midnight is stored as 23:59 (the battery can't hold 24:00)."""
    if a.keys() != b.keys():
        return False
    for day in a:
        if a[day].keys() != b[day].keys():
            return False
        for cat in a[day]:
            for (s1, e1, p1), (s2, e2, p2) in zip(a[day][cat], b[day][cat]):
                m = lambda t: int(t[:2]) * 60 + int(t[3:])
                if (s1, p1) != (s2, p2) or m(e2) - m(e1) not in (0, 1):
                    return False
    return True


def test_end_landing_on_midnight_loses_one_minute_only():
    real = {"mon": {"discharge": [_w("20:00", "23:47")]}}
    battery, _ = shift_schedule(real, 13)
    assert battery == {"mon": {"discharge": [_w("20:13", "23:59")]}}
    assert shift_schedule(battery, -13)[0] == {"mon": {"discharge": [_w("20:00", "23:46")]}}


# --- clock offset --------------------------------------------------------------
def test_offset_one_hour_behind_in_nz_daylight_time():
    utc = datetime(2026, 9, 28, 4, 30, tzinfo=timezone.utc)      # 17:30 NZDT
    battery = datetime(2026, 9, 28, 16, 30)                        # still NZST
    vs_utc = clock.battery_minus_utc(battery, utc)
    assert clock.local_offset(vs_utc, NZ, utc) == -60
    # the same battery clock is on time once daylight saving ends
    later = datetime(2027, 4, 5, 4, 30, tzinfo=timezone.utc)
    assert clock.local_offset(clock.battery_minus_utc(
        later.replace(tzinfo=None) + timedelta(hours=12), later), NZ, later) == 0


def test_unset_clock_is_rejected():
    utc = datetime(2026, 9, 28, 4, 30, tzinfo=timezone.utc)
    vs_utc = clock.battery_minus_utc(datetime(2000, 1, 1), utc)
    assert clock.local_offset(vs_utc, NZ, utc) is None


def test_describe():
    assert clock.describe(-60) == "1 h 0 min behind"
    assert clock.describe(3) == "3 min ahead"
    assert clock.describe(0) == "on time"
    assert clock.describe(None) == "unknown"


def test_estimator_uses_freshest_and_needs_samples():
    est = clock.OffsetEstimator()
    base = datetime(2026, 9, 28, 4, 0, tzinfo=timezone.utc)
    # battery 60 min behind NZDT = NZST = UTC+12 -> +720 vs UTC; stale readings
    # look further behind by their age.
    for i, age in enumerate([200, 0, 90]):
        utc = base + timedelta(minutes=5 * i)
        seen = utc.replace(tzinfo=None) + timedelta(hours=12) - timedelta(seconds=age)
        est.add(seen.strftime("%Y-%m-%d %H:%M:%S"), utc, now=300.0 * i)
        if i < 2:
            assert est.estimate(300.0 * i) is None
    assert round(est.estimate(600.0)) == 720


def test_estimator_ignores_repeats_and_old_samples():
    est = clock.OffsetEstimator(window=100, min_samples=1)
    utc = datetime(2026, 9, 28, 4, 0, tzinfo=timezone.utc)
    est.add("2026-09-28 16:00:00", utc, now=0)
    est.add("2026-09-28 16:00:00", utc + timedelta(minutes=5), now=50)   # same stale reading
    assert round(est.estimate(50)) == 720
    assert est.estimate(500) is None                                      # aged out
    est.add("garbage", utc, now=500)
    assert est.estimate(500) is None
