# OpenHomepower for Home Assistant

**Local, read-only monitoring for Energizer Homepower (Enertek HP-series) home batteries.**

[![hacs][hacs-badge]][hacs]

> ⚠️ Not affiliated with, endorsed by, or supported by Energizer, 8 Star Energy
> or Enertek Holdings. Unofficial software, provided as-is.
> **It never writes to your battery.**

Enertek abandoned the Homepower — the iOS app was pulled from the App Store, the
product is discontinued, and the web portal's daily energy summary now reports
0 kWh for everything. This integration talks to the battery **directly on your
own network**, with no vendor cloud involved, and gives you back:

- State of charge, pack voltage, battery power
- Solar generation, grid import/export, household load
- **Daily energy counters that feed the Energy Dashboard** — including data the
  vendor portal can no longer show you

## Install

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories**
2. Add this repository, category **Integration**
3. Install **OpenHomepower**, restart Home Assistant
4. **Settings → Devices & Services → Add Integration → OpenHomepower**

### Manual

Copy `custom_components/openhomepower` into your `config/custom_components/`
directory and restart Home Assistant.

## Setup

The integration tries to find your battery automatically — it advertises the
DHCP hostname `Homepower`, and its web page carries an identifiable title. If
autodiscovery finds it, the address is pre-filled; otherwise enter the IP.

The username and password defaults are the **manufacturer's own published
values** (from Enertek's Wi-Fi setup guide) and work on unmodified units.

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

It never writes anything, never touches the serial line, and never sends your
data anywhere. The protocol and register map are documented in
[`PROTOCOL.md`](https://github.com/seanlewis/openhomepower-app/blob/main/PROTOCOL.md).

## Will it work with my battery?

Verified against an **HP6 (12.2 kWh)**. Other HP-series units use the same
gateway, so they should work — but the register map is confirmed on one unit.

Every sensor carries a `confidence` attribute (`confirmed`, `candidate` or
`derived`) so you can see which values are verified and which are still educated
guesses. **If a number looks wrong, please open an issue** with what your battery
portal shows — that is how support for other hardware gets added.

## Not a Home Assistant user?

There is a standalone desktop app that needs no Home Assistant, no MQTT and no
YAML: **[OpenHomepower app](https://github.com/seanlewis/openhomepower-app)**.

The two are entirely separate projects — separate installs, no shared
dependency. They only share a published protocol specification.

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
