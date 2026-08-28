# Energy Dashboard setup

The Homepower reports five **daily energy counters** — solar, grid in/out, and
battery in/out — all measured by the battery's own CT clamps. They're
`total_increasing` kWh sensors, which is exactly what Home Assistant's **Energy
dashboard** wants, so the battery alone gives you a complete whole-home energy
picture with no extra hardware.

**Settings → Dashboards → Energy**, then fill in these four cards.

> The sensor names below are the suffixes. Your entities are prefixed with the
> device name, so they show as e.g. *"Energizer Homepower Battery — Solar
> generated today"*. If you renamed the device, match on the suffix. Find the
> exact entity IDs in **Developer Tools → States** (search `daily`).

## What goes where

| Energy card | Button | Field | Sensor (suffix) |
| --- | --- | --- | --- |
| **Electricity grid** | Add grid connection | Grid consumption | Grid imported today |
| | | Return to grid | Grid exported today |
| **Solar panels** | Add solar production | Solar production | Solar generated today |
| **Home Battery Storage** | Add battery system | Energy going in | Battery charged today |
| | | Energy coming out | Battery discharged today |

That's it — one grid connection, one solar production, one battery system.

## Step by step

1. **Electricity grid → Add grid connection.**
   - *Grid consumption* → **Grid imported today**
   - *Return to grid* → **Grid exported today**
2. **Solar panels → Add solar production.**
   - *Solar production* → **Solar generated today**
3. **Home Battery Storage → Add battery system.**
   - *Energy going into the battery* → **Battery charged today**
   - *Energy coming out of the battery* → **Battery discharged today**
4. Save. Home Assistant notes new data can take **up to ~2 hours** to appear on
   the dashboard graphs — that's normal, the config itself is live immediately.

## Notes that save you a headache

- **Use the plain sensors, not the "(DC)" ones.** The map also exposes *Battery
  charged/discharged today (DC)* — those are the cell-side (DC) figures. The
  Energy dashboard balances everything at the AC/household level, so the plain
  **Battery charged today** / **Battery discharged today** are the ones that
  reconcile with grid + solar. The DC pair will look close but won't add up.
- **Pick one source per slot.** These figures come from the Homepower's CTs. If
  you *also* run another integration that reports grid or solar (e.g. a GoodWe
  inverter), don't add both to the same slot — you'll double-count. Choose whichever
  source you trust for that measurement and use it consistently.
- **The daily reset is handled.** Each counter resets to 0 at the battery's
  local midnight; because they're `total_increasing`, Home Assistant treats the
  reset as a new day rather than negative usage. Nothing to configure.
- **Real-time flow is separate.** The Energy dashboard runs on these daily kWh
  counters. For a live power-flow card (Sankey, power-flow), use the instantaneous
  W sensors instead — *Solar PV power*, *Grid power* (signed: + import / − export),
  *Battery power* (signed: + discharge / − charge).
