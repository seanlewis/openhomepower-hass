# Schedule examples for `openhomepower.set_schedule`

`set_schedule` writes a **complete** weekly schedule — anything you don't list is
cleared. Schedules only take effect while the battery is in **Manual** mode
(set the *Application mode* entity to Manual).

## See the current schedule

The **Schedule** sensor (added when control is enabled) shows what's stored on
the battery. Its state is only a summary, such as `7 days, 14 windows`. The
entity's pop-up doesn't show the windows themselves, so use one of these.

### Developer Tools → States

1. Go to **Developer Tools → States**.
2. Type `schedule` in the **Filter entities** box and find
   `sensor.energizer_homepower_schedule`. Your entity ID may differ if you
   renamed the device.
3. The **Attributes** column shows:
   - `schedule`: every window, in exactly the format `set_schedule` takes.
   - `active`: `true` only when the battery is in **Manual** mode, the only mode
     where it follows the schedule.

**To change one window without retyping the week:** copy the `schedule`
attribute from here, edit it, and paste it under `schedule:` in the action (see
[Format](#format)). The sensor updates within a few seconds of a
`set_schedule` call. To confirm the battery really stored it, run the
`homeassistant.update_entity` action on the Schedule sensor; that re-reads the
battery.

### A dashboard card

Add a **Markdown** card to a dashboard (**Edit dashboard → Add card →
Markdown**) and paste this as its content:

```jinja
{% set e = 'sensor.energizer_homepower_schedule' %}
{% set s = state_attr(e, 'schedule') or {} %}
{% set names = {'grid_charge': 'Grid charge', 'pv_charge': 'Solar charge', 'discharge': 'Discharge'} %}
**{{ states(e) }}**{% if state_attr(e, 'active') == false %} · *not active (the battery isn't in Manual mode)*{% endif %}

{% if s -%}
| Day | Type | Window | Power |
|---|---|---|---|
{% for day in ['mon','tue','wed','thu','fri','sat','sun'] if day in s -%}
{% for cat, wins in s[day].items() -%}
{% for w in wins -%}
| {{ day | capitalize }} | {{ names[cat] }} | {{ w.start }}–{{ w.end }} | {{ w.power }}% |
{% endfor %}{% endfor %}{% endfor %}
{%- else -%}
No windows set.
{%- endif %}
```

It shows a table like this:

| Day | Type | Window | Power |
|---|---|---|---|
| Mon | Grid charge | 02:00–05:00 | 100% |
| Mon | Discharge | 14:00–21:00 | 100% |

If your entity ID differs, change it on the first line.

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
