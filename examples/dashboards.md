# Dashboard cards

Ready-made cards for the OpenHomepower entities. They use only Home
Assistant's built-in cards, so there's nothing extra to install (except the one
marked **HACS** at the end).

## Adding a card

1. Open a dashboard, then **⋮ → Edit dashboard**.
2. **+ Add card**, scroll to the bottom and pick **Manual**.
3. Paste the YAML and **Save**.

To add the lot at once, use the [complete dashboard](#a-complete-dashboard) at
the end.

## Before you start

- **Entity IDs.** The cards assume the device is called *Energizer Homepower*,
  so its entities are `sensor.energizer_homepower_…`. If you renamed the device,
  your IDs are different. Find yours under **Settings → Devices & services →
  OpenHomepower → (the device)**, or in **Developer Tools → States**, then
  replace `energizer_homepower` in the YAML with your prefix (a find-and-replace
  in any text editor does it in one go).
- **Signs.** *Battery power* is positive while **discharging** and negative while
  **charging**. *Grid power* is positive while **importing** and negative while
  **exporting**. *Solar* can read a few watts negative at night; that's the solar
  inverter's standby draw. If you'd rather not think about signs, the
  [Live power](#live-power) card uses the separate charge/discharge and
  import/export sensors, which are never negative.
- **Control cards.** Cards marked *needs control* use entities that only exist
  once control is enabled (**Configure → Settings → Enable control**).
- **Candidate values.** Battery temperature, inverter temperature and battery
  health are still marked `confidence: candidate` (see each sensor's
  attributes): they look right but haven't been confirmed against a known
  reading.

## Cards

### At a glance

Five tiles: battery level, battery power, solar, grid and house load. A tidy summary at the top of a dashboard. Tap any tile for its history.

```yaml
type: grid
columns: 3
square: false
cards:
  - type: tile
    entity: sensor.energizer_homepower_state_of_charge
    name: Battery
  - type: tile
    entity: sensor.energizer_homepower_battery_power
    name: Battery power
  - type: tile
    entity: sensor.energizer_homepower_solar_pv_power
    name: Solar
  - type: tile
    entity: sensor.energizer_homepower_grid_power
    name: Grid
  - type: tile
    entity: sensor.energizer_homepower_house_load
    name: House
```

### Battery gauge

A dial for the state of charge, green above 50%, amber from 20% and red below. When the battery level is the one number you care about.

```yaml
type: gauge
entity: sensor.energizer_homepower_state_of_charge
name: Battery
min: 0
max: 100
needle: true
severity:
  green: 50
  yellow: 20
  red: 0
```

### Live power

Where the power is going right now, with charge, discharge, import and export as separate lines. If the signed battery and grid figures are confusing. Each line here is always zero or positive.

```yaml
type: entities
title: Power now
entities:
  - entity: sensor.energizer_homepower_solar_pv_power
    name: Solar
  - entity: sensor.energizer_homepower_house_load
    name: House
  - entity: sensor.energizer_homepower_charge_power
    name: Battery charging
  - entity: sensor.energizer_homepower_discharge_power
    name: Battery discharging
  - entity: sensor.energizer_homepower_grid_import
    name: Importing from grid
  - entity: sensor.energizer_homepower_grid_export
    name: Exporting to grid
```

### Power over the day

A 24-hour line graph of solar, house load, battery and grid. To see when the battery charged and discharged, and how that lined up with solar and your usage.

```yaml
type: history-graph
title: Power (24 h)
hours_to_show: 24
entities:
  - entity: sensor.energizer_homepower_solar_pv_power
    name: Solar
  - entity: sensor.energizer_homepower_house_load
    name: House
  - entity: sensor.energizer_homepower_battery_power
    name: Battery (+ discharging)
  - entity: sensor.energizer_homepower_grid_power
    name: Grid (+ importing)
```

### Today's totals

Today's energy counters in one row. A quick daily tally. The counters reset at the battery's midnight.

```yaml
type: glance
title: Today
columns: 5
entities:
  - entity: sensor.energizer_homepower_solar_generated_today
    name: Solar
  - entity: sensor.energizer_homepower_battery_charged_today
    name: Charged
  - entity: sensor.energizer_homepower_battery_discharged_today
    name: Discharged
  - entity: sensor.energizer_homepower_grid_imported_today
    name: Imported
  - entity: sensor.energizer_homepower_grid_exported_today
    name: Exported
```

### Last 7 days

A bar per day for solar, battery charge and discharge, and grid import and export. To compare days, e.g. how much a cloudy day cost you in grid import.

```yaml
type: statistics-graph
title: Daily energy (7 days)
chart_type: bar
period: day
days_to_show: 7
stat_types:
  - change
entities:
  - entity: sensor.energizer_homepower_solar_generated_today
    name: Solar
  - entity: sensor.energizer_homepower_battery_charged_today
    name: Charged
  - entity: sensor.energizer_homepower_battery_discharged_today
    name: Discharged
  - entity: sensor.energizer_homepower_grid_imported_today
    name: Imported
  - entity: sensor.energizer_homepower_grid_exported_today
    name: Exported
```

### Control panel (needs control)

Application mode, maximum charge, the two reserve limits and excess generation, all editable in place, plus the schedule summary. To change settings without opening the device page. The schedule itself is changed under [Configure → Schedule](schedules.md#set-the-schedule-configure--schedule).

```yaml
type: entities
title: Battery control
entities:
  - entity: select.energizer_homepower_application_mode
    name: Mode
  - entity: number.energizer_homepower_maximum_state_of_charge
    name: Maximum charge
  - entity: number.energizer_homepower_reserve_limit_on_grid
    name: Reserve (on-grid)
  - entity: number.energizer_homepower_reserve_limit_off_grid
    name: Reserve (off-grid)
  - entity: number.energizer_homepower_excess_generation_to_charge
    name: Excess generation to charge
  - entity: sensor.energizer_homepower_schedule
    name: Schedule
```

### Schedule table (needs control)

Every window stored on the battery, as a table. Same card as in the [schedule guide](schedules.md#a-dashboard-card). To check the schedule at a glance.

```yaml
type: markdown
title: Schedule
content: |
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

### Battery health

Temperatures, voltages, grid frequency, battery health and the battery's own clock. For keeping an eye on the hardware, or when something looks wrong.

```yaml
type: entities
title: Battery health
entities:
  - entity: sensor.energizer_homepower_battery_temperature
    name: Battery temperature
  - entity: sensor.energizer_homepower_inverter_temperature
    name: Inverter temperature
  - entity: sensor.energizer_homepower_pack_voltage
    name: Pack voltage
  - entity: sensor.energizer_homepower_battery_health
    name: Battery health
  - entity: sensor.energizer_homepower_ac_voltage
    name: AC voltage
  - entity: sensor.energizer_homepower_grid_frequency
    name: Grid frequency
  - entity: sensor.energizer_homepower_operating_mode
    name: Operating mode
  - entity: sensor.energizer_homepower_device_clock
    name: Battery clock
```

### Phone summary

A battery tile over a single row of solar, house, battery and grid. On a phone, where space is tight.

```yaml
type: vertical-stack
cards:
  - type: tile
    entity: sensor.energizer_homepower_state_of_charge
    name: Battery
  - type: glance
    show_name: true
    entities:
      - entity: sensor.energizer_homepower_solar_pv_power
        name: Solar
      - entity: sensor.energizer_homepower_house_load
        name: House
      - entity: sensor.energizer_homepower_battery_power
        name: Battery
      - entity: sensor.energizer_homepower_grid_power
        name: Grid
```

## HACS: animated power flow

Needs the [Power Flow Card Plus](https://github.com/flixlix/power-flow-card-plus)
card installed from HACS (**HACS → search "Power Flow Card Plus" → Download**,
then reload the browser). It animates power moving between solar, grid,
battery and house.

It uses the separate charge/discharge and import/export sensors, so no sign
settings are needed.

```yaml
type: custom:power-flow-card-plus
entities:
  battery:
    entity:
      consumption: sensor.energizer_homepower_charge_power
      production: sensor.energizer_homepower_discharge_power
    state_of_charge: sensor.energizer_homepower_state_of_charge
  grid:
    entity:
      consumption: sensor.energizer_homepower_grid_import
      production: sensor.energizer_homepower_grid_export
  solar:
    entity: sensor.energizer_homepower_solar_pv_power
  home:
    entity: sensor.energizer_homepower_house_load
clickable_entities: true
```

## A complete dashboard

[`dashboard.yaml`](dashboard.yaml) is a whole dashboard built from the cards
above, in four sections: *Now*, *Today*, *Control* and *Health*. It needs Home
Assistant 2024.10 or later (for the section headings).

1. **Settings → Dashboards → + Add dashboard → New dashboard from scratch**, name
   it (e.g. *Battery*) and **Create**.
2. Open the new dashboard, then **⋮ → Edit dashboard → ⋮ → Raw configuration
   editor**.
3. Select everything, paste in the whole of `dashboard.yaml`, and **Save**.

If control isn't enabled, delete the *Control* section first (the lines from
`# Control` down to just before the next `- type: grid`), or its cards will show
missing entities.

To add it to an existing dashboard instead, copy the part from `- title:
Homepower` down, and paste it under that dashboard's `views:` with each line
indented to match the other views.
