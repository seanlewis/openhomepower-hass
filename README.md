# OpenHomepower for Home Assistant

**Monitor, and optionally control, Energizer Homepower (Enertek HP-series) home
batteries from Home Assistant.**

[![hacs][hacs-badge]][hacs]

> ⚠️ Not affiliated with, endorsed by, or supported by Energizer, 8 Star Energy
> or Enertek Holdings. Unofficial software, provided as-is.

Enertek has effectively abandoned the Homepower: the iOS app was pulled, the
product is discontinued, and owners report its cloud being offline for weeks at
a time. This integration talks to the battery **directly on your network** and
gives you back:

- State of charge, battery power, solar generation, grid import/export, household load
- **Daily energy counters for the Energy Dashboard**
- Optional **control**: application mode, reserve limits and weekly schedules
  (off by default)

On most units, monitoring needs no Enertek cloud at all. Control, and monitoring
on units that can only report over MQTT, go through Enertek's broker until you
[move to your own](#the-broker-and-cutting-the-cord), which takes one click.

## Install

**Requirements:** Home Assistant **2024.6 or newer** (any install type) and
[HACS](https://hacs.xyz).

<details>
<summary>Don't have HACS yet?</summary>

**HA OS / Supervised:**

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add
   `https://github.com/hacs/addons`.
2. Install and **Start** the **Get HACS** add-on, then restart Home Assistant.
3. **Settings → Devices & Services → + Add Integration → HACS**, and authorise
   with a (free) GitHub account.

**Container / Core:** run `wget -O - https://get.hacs.xyz | bash -` in a shell on
your Home Assistant, restart, then do step 3 above.

Official guide: <https://hacs.xyz/docs/use/download/download/>.

</details>

### 1. Add this integration to HACS

1. **HACS → ⋮ (top right) → Custom repositories.**
2. Paste `https://github.com/seanlewis/openhomepower-hass`, set the category to
   **Integration**, click **Add**.
3. Search HACS for **OpenHomepower**, **Download** it, then restart Home
   Assistant.

<details>
<summary>Without HACS (manual install)</summary>

Copy `custom_components/openhomepower` from this repository into your Home
Assistant's `config/custom_components/` folder and restart. You'll need to repeat
this for each new version.

</details>

### 2. Check Home Assistant can reach the battery

Home Assistant talks to the battery **over your network**. This is the most
common reason setup fails.

- **Same network:** the battery must be on the same network as Home Assistant,
  **not a guest Wi-Fi**. Guest networks block this.
- **Quick test:** on a computer on Home Assistant's network, open
  `http://<battery IP>` in a browser. You should see an **Energizer Homepower**
  sign-on page. If you can't, neither can Home Assistant.
- **Finding the IP:** look for the hostname **Homepower** in your router (UniFi
  shows it as a "Philips SmartTV"). Reserve that IP so it doesn't change.

<details>
<summary>Moving the battery to your main Wi-Fi</summary>

From a device on the battery's current network, open its sign-on page, log in
with `homepower` / `123456`, and change its Wi-Fi to your main network. It's
**2.4 GHz only**. If you get the password wrong, it drops off the network: join
the Wi-Fi network the battery broadcasts and open `http://10.9.8.1` to fix it.

To keep the battery isolated instead, allow **only your Home Assistant machine**
to reach it on TCP ports **80** and **34522** with a router firewall rule.

</details>

### 3. Set up the integration

1. **Settings → Devices & Services → + Add Integration → OpenHomepower.**
2. It looks for your battery and fills in the address. If it says **"none found
   automatically"**, go back to step 2.
3. Leave the login (the manufacturer's published defaults) and the **telemetry
   source (Automatic)** as they are, and submit.

Your sensors appear within a few seconds.

### Telemetry source

**Automatic** reads the battery directly over your network (SSH), and switches
to MQTT for units that don't support that. Sensors and control are identical
either way.

| | **SSH log** | **MQTT broker** |
| --- | --- | --- |
| Works on | Most units | Every unit |
| Needs Enertek's cloud? | No | Yes, unless you [run your own broker](#the-broker-and-cutting-the-cord) |

You can switch later without losing entities or history, under **Configure →
Settings**.

### Troubleshooting setup

| What you see | What it usually means |
| --- | --- |
| "none found automatically" | Home Assistant can't reach the battery: guest network, different VLAN, or Home Assistant in Docker without host networking. See step 2. |
| "Could not reach the battery" | The same, or a wrong IP. The gateway also drops off Wi-Fi briefly now and then, so try once more. |
| "MQTT source needs the broker host…" | MQTT is selected but discovery didn't fill in the broker details. Fix discovery, or use Automatic. |
| "Connected, but no telemetry could be decoded" | Your unit doesn't write the log SSH reads. Use Automatic. |
| "The username or password was rejected" | The battery's login was changed from the defaults. Enter the current one. |

Still stuck? **Settings → System → Logs**, search `openhomepower`, and include
those lines in an [issue](https://github.com/seanlewis/openhomepower-hass/issues).

## Changing settings

**Settings → Devices & Services → OpenHomepower → Configure.** On Home Assistant
OS / Supervised this opens a menu: **Settings** (poll interval, telemetry source,
control) and **Move to local broker**. Other installs go straight to the
settings.

## Energy Dashboard

In **Settings → Dashboards → Energy**:

| Section | Sensor |
| --- | --- |
| Solar production | Daily Solar Generation |
| Grid consumption | Daily Grid Import |
| Return to grid | Daily Grid Export |
| Battery in / out | Daily Battery Charge / Daily Battery Discharge |
| Grid power (optional, "Standard") | Grid Power (signed: + import, − export) |

The counters reset at the battery's midnight, and Home Assistant handles that
correctly. There's a [step-by-step walkthrough](examples/energy-dashboard.md),
including the AC-vs-DC and double-counting gotchas.

## Control (optional)

> ⚠️ Control writes real settings to a lithium battery. The reserve limits set a
> discharge floor and the schedule governs charging and discharging, so set them
> deliberately. Firmware updates are never touched.

Turn it on under **Configure → Settings → Enable control**. It adds:

- **Application mode** (Automatic / Semi-automatic / Manual)
- **Maximum state of charge**, **reserve limits (on/off-grid)** and **excess
  generation to charge**
- **`openhomepower.set_schedule`**, which writes a full weekly schedule (Manual
  mode). It **replaces** the whole schedule, so any day or window you leave out
  is cleared:

  ```yaml
  service: openhomepower.set_schedule
  data:
    schedule:
      mon:
        grid_charge: [{ start: "02:00", end: "05:00", power: 100 }]
        discharge:   [{ start: "17:00", end: "21:00", power: 100 }]
  ```

See [ready-made schedules](examples/schedules.md) and
[automation ideas](examples/automations.md): pre-charging before a cloudy day,
using a cheap power window, raising the reserve before a storm.

### The broker, and cutting the cord

Control commands reach the battery through an MQTT broker, which is **Enertek's**
to begin with. When their cloud is down, control stops. To avoid that, move the
battery to your own broker:

1. Install the [**OpenHomepower Secure Broker**](https://github.com/seanlewis/openhomepower-broker)
   add-on, **version 0.2.2 or later**: **Settings → Add-ons → Add-on Store → ⋮ →
   Repositories**, add `https://github.com/seanlewis/openhomepower-broker`, then
   install it. There's no need to configure it.
2. Reserve Home Assistant's IP address in your router, because the battery will
   be pointed at it.
3. **Configure → Move to local broker**, check the two addresses, and submit.

Home Assistant sets up the add-on, points the battery at it and confirms it has
connected. This takes up to 6 minutes. If the battery doesn't connect, the change
is undone automatically. **Move back to Enertek's broker** in the same menu
reverses it.

Once moved, the Enertek app and portal stop showing your battery, because it can
only use one broker. On Docker/Core installs, or if the button reports a problem,
the [broker's guide](https://github.com/seanlewis/openhomepower-broker/blob/main/broker/DOCS.md)
has the same steps to do by hand.

## How it works

The "Homepower" on your network is a small **OpenWrt gateway** that bridges the
battery to Wi-Fi. The integration reads the battery's data either from the log
the gateway keeps (over SSH) or by asking for it over MQTT, and decodes it. The
register map, with a confidence level for every field, is in
[`registers.yaml`](custom_components/openhomepower/registers.yaml).

Monitoring never writes to the battery and never sends your data anywhere
outside your network or its broker.

## Will it work with my battery?

It's verified on an **HP6 (12.2 kWh)** and works on other units in the field.
Every sensor has a `confidence` attribute (`confirmed`, `candidate` or
`derived`). **If a number looks wrong, please open an issue** with what your
battery's portal shows. That's how support for other hardware improves.

## Not a Home Assistant user?

A standalone desktop app for owners without Home Assistant is in development, as
a separate project.

## Development

```bash
pip install pytest pyyaml
python -m pytest tests -q
```

Tests run without Home Assistant or a battery.

## Licence

MIT.

[hacs]: https://github.com/hacs/integration
[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
