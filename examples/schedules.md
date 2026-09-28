# Battery schedules

The battery can follow a weekly schedule of **grid charge**, **solar charge**
and **discharge** windows, but only while it's in **Manual** mode. Control must
be enabled (**Configure → Settings → Enable control**).

## Set the schedule: Configure → Schedule

The easiest way. Go to **Settings → Devices & services → OpenHomepower →
Configure → Schedule**.

- The form opens with the schedule that's on the battery now.
- Each type has **two windows**. Tick **Use this window**, set the start, end
  and power, and tick the days it runs. Most schedules need just one window per
  type, e.g. grid charge 02:00–05:00 every day.
- Use the second window for a different pattern on other days (weekdays vs
  weekends), or a second window on the same day.
- Set **Application mode** to **Manual** for the battery to follow it.
- **Saving replaces the battery's whole schedule.** It's only sent if you
  changed something; changing just the mode leaves the schedule alone.
- A window can't cross midnight. Split it across both windows, e.g.
  23:00–23:59 and 00:00–05:00.

The **Schedule** sensor confirms what the battery stored within a few seconds.

## The battery's clock

The battery runs its schedule by its **own clock**, which was set once and never
changes for daylight saving (and drifts a little over time). After a
daylight-saving change it's an hour out, so without help a 4 am charge would run
at 5 am.

Home Assistant handles this for you:

- It reads the battery's clock and shows how far out it is in the **Battery
  clock offset** sensor (e.g. `-60`, "1 h 0 min behind").
- **Every time you see or enter is real time.** When you save 04:00–07:00 on a
  battery that's an hour behind, Home Assistant stores 03:00–06:00 on it, which
  is 4–7 am by the real clock. The Schedule sensor's `battery_schedule`
  attribute shows what's actually stored.
- **After a daylight-saving change** (or drift of 5 minutes or more), it
  re-writes the schedule so it keeps running at the same real times, and posts
  a notification saying so. It waits for two readings in a row before doing it.
- It **never changes the battery's clock**, and never adjusts anything while it
  can't read the clock.
- **The first time** (after updating to 0.7.3) it doesn't change anything. It
  can't know whether you'd already shifted your schedule by hand, so it shows
  the schedule at the real times it actually runs, and if the clock is out, asks
  you to check it. Fix any times under Configure → Schedule.

A few things to know:

- Shifting can move a window across midnight. It's then stored as two pieces,
  which uses both of that type's windows on those days. If a day would need
  more than two, Home Assistant tells you instead of saving.
- The battery's "today" totals still reset at the *battery's* midnight.
- To manage the offset yourself, turn off **Keep the schedule on real time** in
  **Configure → Settings**. Times are then the battery's own clock times.
- Schedules use Home Assistant's time zone. If the battery is somewhere else,
  set **Time zone for schedules** in the same place (e.g. `Pacific/Auckland`).

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

## Schedules in automations

For automations (a cheap power window, a storm warning), use the
`openhomepower.set_schedule` action. It writes a **complete** weekly schedule:
anything you don't list is cleared. The form above shows schedules set this way;
if one has more than two different windows of a type, the form warns you before
anything is replaced.

### Format

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

### Time-of-use: cheap overnight grid charge, evening discharge

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

### Maximise self-consumption: charge from solar through the day

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

### Weekday vs weekend (two different patterns)

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

### Clear the schedule (no windows)

```yaml
action: openhomepower.set_schedule
data:
  schedule: {}
```

---

> ⚠️ This overwrites the **entire** schedule every time. To keep existing windows
> and add one, include them all in the same call.
