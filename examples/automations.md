# Automation examples

A fixed weekly schedule is fine, but the real power of control is **automation**:
Home Assistant can change the battery's behaviour based on the **weather
forecast, electricity prices, time of day** — anything HA knows. The control
entities (`select`, `number`) and the `openhomepower.set_schedule` action are
just building blocks; these examples show what you can do with them.

> Change the placeholder entity IDs (`weather.forecast_home`,
> `select.energizer_homepower_application_mode`,
> `number.energizer_homepower_reserve_limit_off_grid`) to match your setup —
> find them in **Developer Tools → States**. The examples are independent ideas;
> don't run all three unmodified (they'd fight over the mode).

## How reversion works — and not getting stuck

The battery **holds whatever mode/schedule was last written** — there is no
auto-revert and no default fallback. So *reversion is the automation's job*. Two
habits keep you out of trouble:

1. **Keep a daily "reconciler"** that always sets the correct state for the day.
   Example 1 is one — it runs every evening and picks Auto or Manual. Even if a
   short-lived override never reverts (HA restarted, a trigger missed), the next
   daily run puts things right. A minimal version if you don't use the forecast one:

   ```yaml
   alias: Battery – daily reset to Automatic
   triggers:
     - trigger: time
       at: "07:00:00"
   actions:
     - action: select.select_option
       target:
         entity_id: select.energizer_homepower_application_mode
       data:
         option: auto
   ```

2. **Make any temporary Manual schedule self-sufficient** — always include a
   `discharge` window, so if a revert is missed the battery still covers your
   load instead of sitting idle. (Examples 1 and 2 do this.)

---

## 1. Pre-charge before a low-solar day

Each evening, if tomorrow looks rainy/overcast (solar won't fill the battery),
switch to Manual and grid-charge overnight on the cheap rate so the battery is
full for the expensive daytime. Otherwise leave it in Automatic and let solar
fill it.

```yaml
alias: Battery – pre-charge before low-solar days
triggers:
  - trigger: time
    at: "20:00:00"
actions:
  - action: weather.get_forecasts
    target:
      entity_id: weather.forecast_home
    data:
      type: daily
    response_variable: fc
  - variables:
      tomorrow: "{{ fc['weather.forecast_home'].forecast[1] }}"
      poor_solar: >-
        {{ (tomorrow.precipitation_probability | default(0)) >= 60
           or (tomorrow.cloud_coverage | default(0)) >= 70
           or tomorrow.condition in
              ['rainy','pouring','snowy','snowy-rainy','hail','lightning-rainy','cloudy','fog'] }}
  - choose:
      - conditions: "{{ poor_solar }}"
        sequence:
          - action: select.select_option
            target:
              entity_id: select.energizer_homepower_application_mode
            data:
              option: manual
          - action: openhomepower.set_schedule
            data:
              schedule:
                mon: &low
                  grid_charge: [{ start: "00:00", end: "06:00", power: 100 }]
                  discharge:   [{ start: "06:00", end: "22:00", power: 100 }]
                tue: *low
                wed: *low
                thu: *low
                fri: *low
                sat: *low
                sun: *low
    default:
      - action: select.select_option
        target:
          entity_id: select.energizer_homepower_application_mode
        data:
          option: auto
```

### Better: trigger on forecast *kWh*, not weather words

Add the free **Forecast.Solar** integration (enter your panel kW / tilt /
azimuth) and you get an estimated-production sensor — so the trigger becomes
precise instead of a guess:

```yaml
  - variables:
      poor_solar: "{{ states('sensor.energy_production_tomorrow') | float(0) < 8 }}"  # kWh
```

"Charge if tomorrow's expected solar is under *X* kWh" beats "the app said
cloudy." Start the threshold around your battery's usable capacity and tune it.

---

## 2. Grab a free / cheap power window

Grid-charge hard during a known free or off-peak window (e.g. a booked Genesis
**Power Shout**, or your cheapest night hours), then hand back to Automatic. Set
the times to your window.

```yaml
alias: Battery – grid-charge during a free/cheap window
triggers:
  - trigger: time
    at: "05:00:00"        # start of your free/cheap window
    id: start
  - trigger: time
    at: "06:00:00"        # end of it
    id: end
actions:
  - choose:
      - conditions: "{{ trigger.id == 'start' }}"
        sequence:
          - action: select.select_option
            target:
              entity_id: select.energizer_homepower_application_mode
            data:
              option: manual
          - action: openhomepower.set_schedule
            data:
              schedule:
                mon: &charge
                  grid_charge: [{ start: "05:00", end: "06:00", power: 100 }]
                  discharge:   [{ start: "06:00", end: "22:00", power: 100 }]  # so a missed revert isn't harmful
                tue: *charge
                wed: *charge
                thu: *charge
                fri: *charge
                sat: *charge
                sun: *charge
    default:
      - action: select.select_option
        target:
          entity_id: select.energizer_homepower_application_mode
        data:
          option: auto
```

> If you have a spot-price integration (Amber, a price sensor, etc.), trigger on
> **price below a threshold** instead of a fixed time — that's genuinely dynamic
> "charge when power is cheap" behaviour a static schedule can never do.

---

## 3. Keep more in reserve before a storm

Raise the off-grid reserve limit when a storm/high wind is forecast, so the
battery holds enough for a possible outage — then drop it back when it clears.

```yaml
alias: Battery – storm reserve
triggers:
  - trigger: state
    entity_id: weather.forecast_home
    to: ["windy", "windy-variant", "lightning", "lightning-rainy", "exceptional"]
    id: storm
  - trigger: state
    entity_id: weather.forecast_home
    from: ["windy", "windy-variant", "lightning", "lightning-rainy", "exceptional"]
    id: clear
actions:
  - choose:
      - conditions: "{{ trigger.id == 'storm' }}"
        sequence:
          - action: number.set_value
            target:
              entity_id: number.energizer_homepower_reserve_limit_off_grid
            data:
              value: 50           # hold 50% back for backup
    default:
      - action: number.set_value
        target:
          entity_id: number.energizer_homepower_reserve_limit_off_grid
        data:
          value: 6                # normal reserve
```

---

These barely scratch the surface. Because control is just HA entities and an
action, **anything Home Assistant can trigger on** — spot prices, an EV
charging, presence, indoor temperature, a calendar — can drive your battery.
That's the difference between a schedule and an automated home.
