# MQTT control read-back / SSH-free MQTT mode — design spec

**Repo:** openhomepower-hass
**Date:** 2026-08-27
**Status:** approved design, pre-implementation
**Sequenced after:** MQTT telemetry read path (v0.3.0, merged)

## 1. Overview

Make the **control** (write) feature read its current config over **MQTT** on
MQTT-source entries, so the whole integration — telemetry, writes, and control
read-back — runs with **no SSH at all** when the read source is MQTT. Control's
read source follows the entry's read source: SSH entries keep SSH read-back
(unchanged); MQTT entries read config over MQTT. Writes stay MQTT for both.
MQTT also becomes the default read source for new installs.

## 2. Motivation

Control currently reads the writable config (mode / max-SoC / reserve / excess)
over **SSH** by scraping fn-03 holding frames from the gateway's log
(`ControlCoordinator` → `gateway.read_holding()`). On a gateway build whose
daemon doesn't log frames to disk — the same class of unit the MQTT *telemetry*
read path was built for — that read returns nothing, so:

- the control entities are marked **unavailable** (their coordinator never gets
  a successful read), and
- the off-grid-reserve write is **unsafe**: it is a whole-block write of
  registers 120–123 that reuses the current `excess` value
  (`number.py` builder `build_reserve_block(v, data["excess"])`); with no
  read-back it falls back to `excess = 100` and would clobber the real setting.

The daemon exposes holding registers over MQTT via an fn-03 request/response on
`Enertek/<serial>/DataTransmission/Input` → `.../Output` — **confirmed live** on
the reference unit (it returned the real config: mode auto, max-SoC 100, reserve
on 5 / off 6, excess 100). `MqttControl.read(reg, count)` already implements that
request/response. This spec wires it into control read-back and removes the SSH
dependency from the MQTT path.

## 3. Goals / non-goals

**Goals**
- Read control state (mode, max-SoC, reserve-on, reserve-off, excess) over MQTT
  on MQTT-source entries, producing the identical state dict the SSH path yields.
- Make an MQTT-source entry fully **SSH-free**: no SSH `Gateway` constructed, no
  host / SSH credentials required.
- Make the previously-unsafe off-grid-reserve write **safe** on MQTT entries by
  supplying a real `excess` value from the MQTT read-back.
- Default new installs to the MQTT read source; keep SSH as an independent,
  selectable option.
- Reuse the existing decode (`control_state_from_regs`) and the existing MQTT
  transport (`MqttControl.read`). No new runtime dependencies.

**Non-goals (this spec)**
- Removing the SSH read source. SSH stays fully functional and selectable; only
  the *default* changes.
- Reading the schedule block for entities (schedule is write-only via the
  `set_schedule` service; the control entities do not display it).
- Using the whole-config `Read_All_Hold_Registers` topic — the proven per-block
  fn-03 read on `DataTransmission` is used; the bulk topic is a possible later
  optimisation.

## 4. Architecture

`ControlCoordinator` becomes **source-agnostic**. It is handed a **config
reader** that returns a `dict[int, int]` of holding registers; everything
downstream (`control_state_from_regs` → entities) is unchanged. Two readers:

- **SSH config reader** (existing behaviour): `parse_holding_frames(await
  gateway.read_holding())`.
- **MQTT config reader** (new): `await hass.async_add_executor_job(
  mqtt.read_config)`, where `MqttControl.read_config()` fetches the needed
  registers over the broker.

The entry's read source (`CONF_READ_SOURCE`) selects which reader is wired in
`__init__.py`. Writes are unchanged and always use `MqttControl.publish`, so the
control entities continue to call `coordinator.mqtt.publish(...)`.

On an MQTT-source entry, **no SSH `Gateway` is created** and host / SSH
credentials are not required at setup.

## 5. Registers read

`control_state_from_regs` consumes exactly five holding registers:

| Field | Register |
| --- | --- |
| mode | 231 |
| max_soc | 67 |
| reserve_on | 105 |
| reserve_off | 121 (within block 120–123) |
| excess | 123 (within block 120–123) |

`MqttControl.read_config()` fetches them as the minimal set of fn-03 reads —
`67×1`, `105×1`, `120×4` (covers 121 + 123), `231×1` — and assembles a
`{reg: value}` dict. It reuses `MqttControl.read()`'s request/response internals;
sequential reads share one broker connection where the daemon supports it (see
§9), falling back to one connection per read otherwise.

## 6. Components

- **`control.py`** — add `MqttControl.read_config() -> dict[int, int]`. Issues
  the fn-03 reads above and returns the merged register map. Read-only (fn-03);
  never publishes a write frame.
- **`control_coordinator.py`** — replace the hard-coded `gateway.read_holding()`
  with an injected config reader that yields `dict[int, int]`. Keep the
  last-known-value merge and the `UpdateFailed` semantics. `self.mqtt` (writes)
  stays. A `read failed` raises `UpdateFailed` exactly as the SSH `TransportError`
  path does today.
- **`__init__.py`** — wire control by read source. SSH entry: build the SSH
  `Gateway` and the SSH config reader (as today). MQTT entry: build **no**
  gateway, don't require SSH creds, and give `ControlCoordinator` the MQTT config
  reader backed by the same `MqttControl` used for writes.
- **`mqtt_coordinator.py`** — make the SSH `gateway` optional (`None` in SSH-free
  mode); `async_shutdown` tolerates a missing gateway (stop the reader, skip the
  gateway close).
- **`config_flow.py`** — the MQTT branch no longer requires host / SSH
  credentials. Flip the default read source to MQTT.
- **`const.py`** — `DEFAULT_READ_SOURCE = READ_SOURCE_MQTT`.
- **`manifest.json`** — version `0.4.0`.
- **`README.md`** — document SSH-free MQTT operation and the default change.

## 7. Data flow (MQTT control read-back)

Coordinator poll (`CONTROL_SCAN_INTERVAL`, slow — config changes only on a write)
→ `mqtt.read_config()` off the loop via executor: connect to the broker,
subscribe `Enertek/<serial>/DataTransmission/Output`, publish fn-03 read requests
to `.../Input`, collect the fn-03 replies (same frame shape as the SSH log
frames), merge into `{reg: value}` → `control_state_from_regs` → mode / max-SoC /
reserve / excess entities.

**Reserve write, now safe on MQTT.** The off-grid-reserve number reconciles the
shared 120–123 block using `coordinator.data["excess"]`. With the MQTT read-back
populating `excess`, the read-modify-write preserves the sibling register instead
of falling back to 100.

## 8. Error handling & availability

- MQTT read timeout / connection failure → `UpdateFailed` → entities unavailable,
  mirroring the SSH `TransportError` path exactly.
- Last-known values are retained across a failed poll (the coordinator already
  merges onto prior data; config is stable between writes).
- Reads run off the event loop via executor (blocking sockets).
- Control MQTT client-id stays `openhomepower-ha-<serial>` — distinct from the
  telemetry reader (`openhomepower-ha-read-<serial>`) and any deployed bridge, so
  no client-id eviction at the broker.

## 9. Open items / risks

- **Sequential reads on one connection.** `read_config` prefers one broker
  connection for all fn-03 reads. `MqttControl.read()` today opens a connection
  per read; whether the daemon reliably answers back-to-back fn-03 requests on a
  single connection is to be confirmed live. If not, fall back to one connection
  per register block — still only ~4 short connects per slow poll, which is
  acceptable.
- **Whole-config read.** `Read_All_Hold_Registers/Input`→`/Output` may return all
  holding registers in one round trip; not used here (per-block fn-03 is proven),
  noted as a later optimisation.
- **Schedule read-back.** Out of scope — the entities don't show schedule. A
  future "read current schedule into the service/UI" feature would add a
  126–230 read.

## 10. Migration

None required. Existing v0.3.0 MQTT entries keep working: leftover `host` /
SSH-credential keys in `entry.data` are ignored, and their control read-back
silently upgrades from (broken) SSH to MQTT. No config-entry version bump.

## 11. Config / security notes

- MQTT control read-back uses the same per-device broker credentials the control
  write path already uses; the device→broker leg stays plaintext on the trusted
  LAN (or via VPN), unchanged from today. The fn-03 read is **read-only** — it
  never writes a register.
- This document uses placeholders only — no real serials, broker hosts, or
  credentials. Test frames use devsn `b"0000000000"`.

## 12. Testing

- **Unit (HA-free):** feed canned register dicts through `control_state_from_regs`
  and assert the entity state; test `read_config`'s reg-set / assembly logic with
  a fake `read`; assert `DEFAULT_READ_SOURCE == "mqtt"`. The blocking socket
  request/response itself (like the telemetry reader) is validated live, not in a
  unit test.
- **Regression:** SSH-path control tests unchanged and still pass.
- **Live checklist (MQTT entry, control enabled):**
  1. Entities populate and match the portal (mode / max-SoC / reserve / excess).
  2. Change a setting (vendor app or HA) → read-back reflects it within a poll.
  3. Drive the off-grid-reserve slider → confirm `excess` is **not** clobbered.
  4. Confirm no SSH connection is made by the MQTT entry.
