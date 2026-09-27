"""Schedule <-> Configure form conversion (no Home Assistant needed)."""
from openhomepower import schedule_form as sf

ALL = list(sf.DAYS)
EVERY_DAY = {d: {"grid_charge": [{"start": "02:00", "end": "05:00", "power": 100}],
                 "discharge": [{"start": "14:00", "end": "21:00", "power": 100}]}
             for d in ALL}


def _win(start, end, power=100, days=ALL, enabled=True):
    return {"enabled": enabled, "start": start, "end": end, "power": power,
            "days": list(days)}


def _off():
    return _win("00:00", "00:00", enabled=False)


def _form(**windows):
    values = {sf.field(c, s): _off() for c in sf.CATEGORIES for s in sf.SLOTS}
    values.update(windows)
    return values


def test_every_day_schedule_fits_one_window_per_type():
    values, fits = sf.schedule_to_form(EVERY_DAY)
    assert fits
    assert values["grid_charge_1"] == _win("02:00", "05:00")
    assert values["discharge_1"] == _win("14:00", "21:00")
    assert not values["grid_charge_2"]["enabled"]
    assert not values["pv_charge_1"]["enabled"]


def test_round_trip_is_exact():
    values, _ = sf.schedule_to_form(EVERY_DAY)
    sched, errors = sf.form_to_schedule(values)
    assert not errors
    assert sf.normalise(sched) == sf.normalise(EVERY_DAY)


def test_weekday_weekend_split_uses_two_windows():
    weekday = {"start": "16:00", "end": "22:00", "power": 100}
    weekend = {"start": "17:00", "end": "20:00", "power": 80}
    sched = {d: {"discharge": [weekday if d in ALL[:5] else weekend]} for d in ALL}
    values, fits = sf.schedule_to_form(sched)
    assert fits
    assert values["discharge_1"] == _win("16:00", "22:00", days=ALL[:5])
    assert values["discharge_2"] == _win("17:00", "20:00", 80, days=ALL[5:])
    back, errors = sf.form_to_schedule(values)
    assert not errors and sf.normalise(back) == sf.normalise(sched)


def test_two_windows_same_day_sorted_by_time():
    values = _form(pv_charge_1=_win("13:00", "15:00", days=["sat"]),
                   pv_charge_2=_win("09:00", "12:00", days=["sat"]))
    sched, errors = sf.form_to_schedule(values)
    assert not errors
    assert [w["start"] for w in sched["sat"]["pv_charge"]] == ["09:00", "13:00"]


def test_more_than_two_distinct_windows_is_flagged():
    sched = {"mon": {"discharge": [{"start": "06:00", "end": "07:00", "power": 100}]},
             "tue": {"discharge": [{"start": "17:00", "end": "21:00", "power": 100}]},
             "wed": {"discharge": [{"start": "18:00", "end": "20:00", "power": 50}]}}
    _, fits = sf.schedule_to_form(sched)
    assert not fits
    assert sf.mixed_categories(sched) == ["discharge"]
    assert sf.mixed_categories(EVERY_DAY) == []


def test_empty_schedule():
    values, fits = sf.schedule_to_form({})
    assert fits and not any(v["enabled"] for v in values.values())
    assert sf.form_to_schedule(values) == ({}, {})


def test_ha_time_selector_seconds_are_dropped():
    values = _form(grid_charge_1=_win("02:00:00", "05:30:00"))
    sched, errors = sf.form_to_schedule(values)
    assert not errors
    assert sched["mon"]["grid_charge"] == [{"start": "02:00", "end": "05:30", "power": 100}]


def test_end_before_start_rejected_including_midnight():
    for start, end in (("05:00", "02:00"), ("23:00", "05:00"), ("10:00", "10:00")):
        _, errors = sf.form_to_schedule(_form(grid_charge_1=_win(start, end)))
        assert errors == {"grid_charge_1": "end_before_start"}


def test_no_days_rejected():
    _, errors = sf.form_to_schedule(_form(discharge_2=_win("17:00", "21:00", days=[])))
    assert errors == {"discharge_2": "no_days"}


def test_overlap_rejected_only_on_shared_days():
    ok = _form(discharge_1=_win("16:00", "20:00", days=["mon"]),
               discharge_2=_win("18:00", "22:00", days=["tue"]))
    assert sf.form_to_schedule(ok)[1] == {}
    clash = _form(discharge_1=_win("16:00", "20:00", days=["mon", "tue"]),
                  discharge_2=_win("18:00", "22:00", days=["tue"]))
    assert sf.form_to_schedule(clash)[1] == {"discharge_2": "overlap"}
    # back-to-back is fine
    touch = _form(discharge_1=_win("16:00", "18:00"), discharge_2=_win("18:00", "22:00"))
    assert sf.form_to_schedule(touch)[1] == {}


def test_disabled_windows_are_ignored_even_if_invalid():
    values = _form(grid_charge_2=_win("05:00", "02:00", days=[], enabled=False))
    assert sf.form_to_schedule(values) == ({}, {})


def test_window_label():
    assert sf.window_label("grid_charge_1") == "Grid charge window 1"
    assert sf.window_label("pv_charge_2") == "Solar charge window 2"
