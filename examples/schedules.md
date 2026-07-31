# Schedule examples for `openhomepower.set_schedule`

`set_schedule` writes a **complete** weekly schedule — anything you don't list is
cleared. Schedules only take effect while the battery is in **Manual** mode
(set the *Application mode* entity to Manual).

## Format

```yaml
action: openhomepower.set_schedule
data:
  schedule:
    <day>:              # mon, tue, wed, thu, fri, sat, sun
      <category>:       # grid_charge, pv_charge, discharge
        - start: "HH:MM"
          end: "HH:MM"
          power: 100    # 0-100 (%)
        # up to TWO windows per category per day
```

- **Categories:** `grid_charge` (charge from the grid), `pv_charge` (charge from
  solar), `discharge` (discharge to your loads).
- Times are 24-hour. `power` is a percentage.
- Up to two windows per category per day.
- YAML anchors (`&day` / `*day`) keep the repeated-day examples short — expand
  them by hand if you prefer.

---

## Time-of-use: cheap overnight grid charge, evening discharge

```yaml
action: openhomepower.set_schedule
data:
  schedule:
    mon: &tou
      grid_charge:
        - { start: "02:00", end: "05:00", power: 100 }
      discharge:
        - { start: "17:00", end: "21:00", power: 100 }
    tue: *tou
    wed: *tou
    thu: *tou
    fri: *tou
    sat: *tou
    sun: *tou
```

## Maximise self-consumption: charge from solar through the day

```yaml
action: openhomepower.set_schedule
data:
  schedule:
    mon: &pv
      pv_charge:
        - { start: "07:00", end: "17:00", power: 100 }
    tue: *pv
    wed: *pv
    thu: *pv
    fri: *pv
    sat: *pv
    sun: *pv
```

## Weekday vs weekend (two different patterns)

```yaml
action: openhomepower.set_schedule
data:
  schedule:
    mon: &wd
      grid_charge:
        - { start: "01:00", end: "05:00", power: 100 }
      discharge:
        - { start: "16:00", end: "22:00", power: 100 }
    tue: *wd
    wed: *wd
    thu: *wd
    fri: *wd
    sat: &we
      pv_charge:
        - { start: "08:00", end: "16:00", power: 100 }
    sun: *we
```

## Clear the schedule (no windows)

```yaml
action: openhomepower.set_schedule
data:
  schedule: {}
```

---

> ⚠️ This overwrites the **entire** schedule every time. To keep existing windows
> and add one, include them all in the same call.
