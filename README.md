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

### 3. Set up the integration

1. **Settings → Devices & Services → + Add Integration**, then search
   **OpenHomepower**.
2. It tries to find your battery automatically — it looks for the DHCP hostname
   `Homepower`. If found, the address is pre-filled; otherwise enter the
   battery's IP address.
3. The username and password are pre-filled with the **manufacturer's own
   published defaults** (from Enertek's Wi-Fi setup guide) and work on
   unmodified units — just continue.
4. Finish. Your battery's sensors appear within a few seconds.

<details>
<summary><b>Prefer not to use HACS? Manual install</b></summary>

Copy the `custom_components/openhomepower` folder from this repository into your
Home Assistant `config/custom_components/` directory, restart Home Assistant,
then do **step 3** above. The trade-off: HACS won't notify you of updates, so
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
regardless. Repointing the gateway is a **config change** (one `uci set
we2.mqtt.host=…`), not a firmware flash; the broker repo has the exact commands
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
