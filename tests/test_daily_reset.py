"""Re-bucketing the battery's DST-less daily counters to HA's local day.

No Home Assistant: the accounting lives in daily_reset.DailyAccumulator, which
the sensor only drives (reading the source, scheduling the midnight reset).
"""
import pytest

from openhomepower.daily_reset import DailyAccumulator


def test_cold_start_begins_at_zero():
    acc = DailyAccumulator()
    assert acc.update(None) is None          # nothing read yet
    assert acc.update(5.0) == 0.0            # first reading only sets the baseline
    assert acc.update(5.3) == pytest.approx(0.3)


def test_sums_positive_steps():
    acc = DailyAccumulator()
    acc.update(0.0)
    acc.update(1.0)
    acc.update(2.5)
    assert acc.total == pytest.approx(2.5)


def test_source_reset_contributes_nothing():
    # The device zeroes its counter at its own (late) midnight: a drop must not
    # subtract, and must not be read as negative energy.
    acc = DailyAccumulator()
    acc.update(6.0)
    acc.update(6.77)              # +0.77
    assert acc.update(0.0) == pytest.approx(0.77)   # device reset -> ignored
    assert acc.update(0.4) == pytest.approx(1.17)   # keeps summing after it


def test_local_midnight_reset_measures_the_step_across_the_rollover():
    acc = DailyAccumulator()
    acc.update(0.0)
    acc.update(4.6)
    acc.reset()                  # HA local midnight
    assert acc.total == 0.0
    # A still-climbing device counter must not dump its whole value into the new
    # day: the step is measured from the pre-reset reading.
    assert acc.update(4.8) == pytest.approx(0.2)


def test_guy_scenario_hour_before_device_reset_counts_to_new_day():
    # 00:00-01:00 real: HA has already rolled the day while the device counter is
    # still climbing toward its own 01:00 reset. That hour's energy belongs to
    # the new local day, and the device's 01:00 reset is harmless.
    acc = DailyAccumulator()
    acc.update(6.5)              # 23:50, previous local day
    acc.reset()                 # 00:00 local -> new day
    assert acc.update(6.77) == pytest.approx(0.27)   # 00:50, still pre-reset
    assert acc.update(0.0) == pytest.approx(0.27)    # 01:00 device reset -> ignored
    assert acc.update(0.3) == pytest.approx(0.57)    # 01:05 -> +0.3


def test_none_holds_the_total_and_resumes_from_the_last_real_reading():
    acc = DailyAccumulator()
    acc.update(1.0)
    acc.update(2.0)
    assert acc.update(None) == pytest.approx(1.0)    # stale reading holds
    assert acc.update(2.5) == pytest.approx(1.5)     # resumes from 2.0, not None
