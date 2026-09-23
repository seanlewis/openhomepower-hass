# OpenHomepower for Home Assistant

**Local, read-only monitoring for Energizer Homepower (Enertek HP-series) home batteries.**

[![hacs][hacs-badge]][hacs]

> ⚠️ Not affiliated with, endorsed by, or supported by Energizer, 8 Star Energy
> or Enertek Holdings. Unofficial software, provided as-is.
> **Read-only by default** — optional control (writing settings) is off unless
> you deliberately enable it. See [Control](#control-optional).

Enertek has effectively abandoned the Homepower: the iOS app was pulled from the
App Store, the product is discontinued, and the Clean Energy Council moved to
suspend its listings. Owners also report the vendor's cloud being **unreliable
and offline for extended periods — reportedly weeks at a time** — during which
they could neither see their data nor change any battery settings.

This integration talks to the battery **directly on your own network**, with no
vendor cloud in the path at all, and gives you back:

- State of charge, pack voltage, battery power
- Solar generation, grid import/export, household load
- **Daily energy counters that feed the Energy Dashboard**

Because nothing here depends on Enertek's servers, it keeps working when their
cloud does not.

Beyond monitoring, it can optionally **control** the battery too — application
mode, reserve limits, and a full weekly charge/discharge schedule. Control is
**off by default** and enabled deliberately; see [Control](#control-optional).

## Install

New to Home Assistant or HACS? Follow this section top to bottom. Already have
HACS? Skip to [step 2](#2-add-this-integration-to-hacs).

**Requirements**

- Home Assistant **2024.6 or newer** — any install type (HA OS, Supervised,
  Container, or Core).
- A free **GitHub account** — HACS uses it to download community integrations.

### 1. Install HACS (skip if the sidebar already shows "HACS")

HACS — the Home Assistant Community Store — is a one-time install that adds (and
later updates) community integrations like this one.

**HA OS / Supervised** — the most common setup, and all done in the UI:

1. **Settings → Add-ons → Add-on Store**, then top-right **⋮ → Repositories**,
   and add `https://github.com/hacs/addons`.
2. Find **Get HACS** in the store, **Install** it, then **Start** it.
3. Restart Home Assistant (**Settings → System → top-right power icon → Restart
   Home Assistant**).
4. **Settings → Devices & Services → + Add Integration**, search **HACS**, tick
   the boxes, then authorise with GitHub — open the link it shows, enter the
   code, approve.
5. **HACS** now appears in your sidebar. Done.

<details>
<summary>Running HA Container or Core instead?</summary>

Those don't have add-ons. Open a shell into your Home Assistant and run:

```bash
wget -O - https://get.hacs.xyz | bash -
```

Then restart Home Assistant and do steps 4–5 above.

</details>

Official guide: <https://hacs.xyz/docs/use/download/download/>.

### 2. Add this integration to HACS

OpenHomepower isn't in the default HACS store yet, so you add it as a **custom
repository** (a one-time step):

1. Open **HACS** from the sidebar.
2. Top-right **⋮ → Custom repositories**.
3. In **Repository**, paste:
   `https://github.com/seanlewis/openhomepower-hass`
4. Set the category to **Integration**, click **Add**, then close the dialog.
5. Search HACS for **OpenHomepower**, open it, and click **Download** (take the
   latest version offered).
6. Restart Home Assistant: **Settings → System → ⋮ → Restart Home Assistant**.

### 3. Check Home Assistant can reach the battery

Setup talks to the battery **directly over your network**, so Home Assistant and
the battery must be able to reach each other. This is the most common reason
setup fails.

- **Same network.** The battery must be on the same network as Home Assistant —
  **not a guest Wi-Fi**. Guest networks block devices on them from being reached
  from your main network.
- **Quick test.** On a computer connected to the *same network as Home
  Assistant*, open `http://<battery IP>` in a browser. You should see an
  **Energizer Homepower** sign-on page. If you can't, Home Assistant can't reach
  it either — fix this before continuing.
- **Finding the IP.** In your router's device list, look for the hostname
  **Homepower**. Some routers mislabel it (UniFi, for example, shows it as a
  "Philips SmartTV"). Reserving that IP in your router stops it changing later.

<details>
<summary>Battery on the wrong network? Moving it to your main Wi-Fi</summary>

From a device on the network the battery is currently on, open its sign-on
page, log in with the manufacturer defaults (`homepower` / `123456`), and change
its Wi-Fi to your main network.

- The battery supports **2.4 GHz only** — your main network needs a 2.4 GHz band.
- Double-check the Wi-Fi password before saving. If it's wrong, the battery drops
  off the network. To recover, join the Wi-Fi network the battery broadcasts
  itself and open `http://10.9.8.1`.

If you'd rather keep the battery isolated, add a router firewall rule allowing
**only your Home Assistant machine** to reach the battery's IP on TCP ports
**80** and **34522**. How to do this depends on your router.

</details>

### 4. Set up the integration

1. **Settings → Devices & Services → + Add Integration**, then search
   **OpenHomepower**.
2. It tries to find your battery automatically. If it does, the address and the
   MQTT broker details are pre-filled for you. If the form says **"none found
   automatically"**, go back to [step 3](#3-check-home-assistant-can-reach-the-battery)
   — this almost always means Home Assistant can't reach the battery.
3. The username and password are pre-filled with the **manufacturer's own
   published defaults** (from Enertek's Wi-Fi setup guide) and work on
   unmodified units — leave them as they are.
4. Leave the **telemetry source** on **Automatic (recommended)**. See below.
5. Submit. Your battery's sensors appear within a few seconds.

### Telemetry source

**Leave it on Automatic.** It reads the battery directly over your network (SSH)
and, if your unit doesn't support that, switches to MQTT for you. Either way you
get exactly the same sensors, Energy Dashboard data and control entities.

The two underlying sources, if you want to pick one yourself:

| | **SSH log** | **MQTT broker** |
| --- | --- | --- |
| Works on | Most units — some gateway builds don't write the log it reads | Every unit |
| Reads from | The battery itself, on your network | The MQTT broker your battery reports to (Enertek's cloud, unless you [run your own](#the-broker-and-cutting-the-cord)) |
| Needs Enertek's cloud? | No | Yes, unless you run your own broker |
| Setup | Just the battery's IP address | Broker details, pre-filled **when auto-discovery finds the battery** |

Automatic tries SSH first because it's fully local, and only uses MQTT when SSH
connects but finds no readings. It records whichever source it chose. You can
switch later without losing your entities or history:
**Settings → Devices & Services → OpenHomepower → Configure**.

### Troubleshooting setup

| What you see | What it usually means |
| --- | --- |
| "none found automatically" | Home Assistant can't reach the battery (guest network, different VLAN, or Home Assistant in Docker without host networking). See [step 3](#3-check-home-assistant-can-reach-the-battery). |
| "Could not reach the battery" | Same as above, or a wrong IP. The gateway also drops off Wi-Fi briefly now and then — try again once. |
| "MQTT source needs the broker host…" | MQTT is selected but auto-discovery didn't fill in the broker details. Fix discovery, or switch to Automatic. |
| "Connected, but no telemetry could be decoded" | With SSH log selected: your unit doesn't write the log SSH reads — switch to Automatic. With Automatic: MQTT didn't answer either; include the log line in an issue. |
| "The username or password was rejected" | The battery's login was changed from the defaults. Enter the current one. |

Still stuck? **Settings → System → Logs**, search for `openhomepower`, and include
that line when you [open an issue](https://github.com/seanlewis/openhomepower-hass/issues).

<details>
<summary><b>Prefer not to use HACS? Manual install</b></summary>

Copy the `custom_components/openhomepower` folder from this repository into your
Home Assistant `config/custom_components/` directory, restart Home Assistant,
then do **steps 3 and 4** above. The trade-off: HACS won't notify you of updates, so
you'd repeat this by hand for each new version.

</details>

## Energy Dashboard

**Settings → Dashboards → Energy**

| Section | Sensor |
| --- | --- |
| Solar production | Daily Solar Generation |
| Grid consumption | Daily Grid Import |
| Return to grid | Daily Grid Export |
| Battery in / out | Daily Battery Charge / Daily Battery Discharge |
| Grid power (optional, "Standard") | Grid Power — signed, + import / − export |

The daily counters reset at device-local midnight; `total_increasing` means
Home Assistant handles that correctly.

→ **[Step-by-step walkthrough](examples/energy-dashboard.md)** — the exact field
in each Energy card, plus the AC-vs-DC and double-counting gotchas.

## How it works

The "Homepower" on your network is a **WeClouds MT7628 gateway running OpenWrt**
that bridges the battery's BMS to WiFi. Its vendor daemon logs every BMS poll to
a file; this integration reads that log over SSH and decodes the register frames.

The monitoring path never writes anything, never touches the serial line, and
never sends your data anywhere. The register map — every field, its scale, and
how confident we are in it — is in
[`registers.yaml`](custom_components/openhomepower/registers.yaml).

## Control (optional)

By default this integration only **monitors**. If you want Home Assistant to
*change* battery settings, enable **control** in the integration's options
(**Settings → Devices & Services → OpenHomepower → Configure → Enable control**).
Control adds:

- **Application mode** (Automatic / Semi-automatic / Manual) — a `select`
- **Maximum state of charge**, **Reserve limit (on/off-grid)** and **Excess
  generation to charge** — `number` sliders
- **`openhomepower.set_schedule`** — a service that writes a full weekly
  charge/discharge schedule (Manual mode) from JSON. It is a **complete
  overwrite** — any day/window you don't list is cleared:

  ```yaml
  service: openhomepower.set_schedule
  data:
    schedule:
      mon:
        grid_charge: [{ start: "02:00", end: "05:00", power: 100 }]
        discharge:   [{ start: "17:00", end: "21:00", power: 100 }]
      sat:
        pv_charge:   [{ start: "09:00", end: "15:00", power: 80 }]
  ```

Ready-to-paste **schedules** are in
[`examples/schedules.md`](examples/schedules.md). And where control really
shines — **automations** (pre-charge before low-solar days from the weather
forecast, grab a free/cheap power window, raise the reserve before a storm) — are
in [`examples/automations.md`](examples/automations.md).

### The broker, and cutting the cord

Control needs an MQTT broker the battery's daemon listens on. Out of the box the
options point at the **vendor broker**, so control works immediately — but
commands travel via Enertek's cloud, so they pause when that cloud is down.

To make control **fully local**, run your own broker — the companion
[**OpenHomepower MQTT Broker**](https://github.com/seanlewis/openhomepower-broker)
is a ready-made, secure one (per-device credentials, isolated topics; installs as
a Home Assistant add-on, Docker, or native). Point the broker host in these
options at it, and repoint the gateway daemon to it. The broker host is the only
switch here — nothing else in the integration changes, and monitoring stays local
regardless. Repointing the gateway is a single, reversible **network redirect**
rule on the gateway, not a firmware flash; the broker repo has the exact commands
and a one-line rollback.

> ⚠️ Control writes real settings to a lithium battery: the reserve limits set a
> discharge floor and the schedule governs charge/discharge. Set them
> deliberately. Firmware-update paths are never touched.

## Will it work with my battery?

Verified against an **HP6 (12.2 kWh)**. Other HP-series units use the same
gateway, so they should work — but the register map is confirmed on one unit.

Every sensor carries a `confidence` attribute (`confirmed`, `candidate` or
`derived`) so you can see which values are verified and which are still educated
guesses. **If a number looks wrong, please open an issue** with what your battery
portal shows — that is how support for other hardware gets added.

## Not a Home Assistant user?

A standalone desktop app is in development for owners who do not run Home
Assistant — no HA, no MQTT, no YAML, just a window showing your battery. It will
be a separate project with its own install; the two share only a published
protocol specification, not code.

## Development

```bash
pip install pytest pyyaml
python -m pytest tests -q
```

Tests run without Home Assistant and without a battery.

## Licence

MIT.

[hacs]: https://github.com/hacs/integration
[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
