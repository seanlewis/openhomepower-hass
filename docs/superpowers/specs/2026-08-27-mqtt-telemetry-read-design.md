# MQTT telemetry reading — design spec

**Repo:** openhomepower-hass
**Date:** 2026-08-27
**Status:** approved design, pre-implementation

## 1. Overview

Add a second telemetry read path to the OpenHomepower Home Assistant
integration: reading the battery's live data from the gateway daemon's **MQTT
stream**, as an alternative to the existing **SSH log-scraping** path. The read
source is chosen explicitly at config time in v1.

## 2. Motivation

The existing read path scrapes hex Modbus frames from `/tmp/wemonitor.log` over
SSH. This only works on gateway builds whose daemon debug-dumps frames to that
log. A second field unit was found whose daemon logs only INFO-level text — no
frames on disk — so log-scraping cannot read it. That daemon still publishes
telemetry over MQTT (unprompted `.../Realtime` publishes). MQTT is therefore the
**universal** telemetry source, present on every unit regardless of log
verbosity.

v1 adds MQTT reading as an explicitly selectable alternative. The longer-term
direction (out of scope here) is to make MQTT the default, since it works on
every unit; SSH stays as the no-broker option.

## 3. Goals / non-goals

**Goals**
- Read SOC, power flows, and daily energy counters from the MQTT telemetry stream.
- Produce the **identical** readings dict the SSH path produces, so all existing
  sensors, Energy Dashboard wiring, and decoding are unchanged.
- Reuse the broker configuration the control path already uses.
- Dependency-free: reuse the existing raw-socket MQTT primitives; no new Python deps.

**Non-goals (v1)**
- Auto-detection of read source / automatic fallback.
- Making MQTT the default (a later one-line change).
- Active request/response polling of `/Read` — the unprompted `/Realtime` stream suffices.

## 4. Architecture

One read abstraction, two sources, both producing the same `readings` dict →
same `DataUpdateCoordinator` → same entities.

- **SSH source (existing):** the coordinator polls `transport.read_latest()`
  every N seconds.
- **MQTT source (new):** a persistent background subscriber receives telemetry
  publishes and pushes fresh readings into the coordinator via
  `async_set_updated_data()`.

The choice is made once at config time via `CONF_READ_SOURCE` (`ssh` | `mqtt`),
default `ssh`.

## 5. Data flow (MQTT)

Confirmed from captured payloads on the reference unit:
- Telemetry publishes arrive on `Enertek/<serial>/Realtime` and
  `Enertek/<serial>/Read_All_Input_Registers/Output`.
- Each MQTT payload is wrapped `[seq-digit byte][0x02][raw Modbus frame]`.
- The frames are function-04 (input register) responses — the same `0104…`
  frames the SSH path scrapes.

Pipeline: subscribe to the device topic scope → parse each PUBLISH
(`_next_publish`) → strip the `[digit][0x02]` prefix → keep function-04 frames →
`protocol.merge()` across register blocks → `registry.decode()` → readings dict →
`async_set_updated_data()` → entities.

## 6. Components

- **`mqtt_reader.py` (new):** persistent, dependency-free MQTT subscriber.
  Reuses `control.py`'s MQTT socket primitives (`_next_publish`, the payload
  strip, `BrokerConfig`). Runs on a supervised background thread: connect,
  subscribe, receive loop with keepalive PINGREQ, extract + decode frames, invoke
  a thread-safe callback into the coordinator, and reconnect with backoff.
- **`config_flow.py`:** add a `CONF_READ_SOURCE` select (default `ssh`). When
  `mqtt`, require/reuse `CONF_BROKER_HOST/PORT/USERNAME/PASSWORD` +
  `CONF_TOPIC_SERIAL` (already present for control). When `ssh`, require SSH creds
  (existing).
- **`__init__.py`:** instantiate the coordinator against the selected source. The
  MQTT source starts the subscriber thread on setup and stops it on unload.
- **`const.py`:** `CONF_READ_SOURCE` and the source constants.
- **Decoder (`protocol.py`, `registry.py`):** unchanged, shared by both sources.

## 7. Error handling & availability

- **Reconnect:** the subscriber retries with capped exponential backoff on
  connect/socket failure.
- **Availability:** track the last-telemetry timestamp; if no telemetry arrives
  for longer than a threshold (default ~180 s, ≈3× the observed publish cadence),
  mark entities unavailable rather than serving stale values.
- **SSH path:** unchanged.
- **Setup validation:** on MQTT setup, briefly connect and await one telemetry
  message (bounded timeout) to confirm broker + topic + decode before creating the
  entry; surface a clear error (broker unreachable / auth failed / no telemetry
  seen), reusing the error-logging added in v0.2.2.

## 8. Testing

- **Unit tests:** feed captured `/Realtime` and `/Read_All_Input_Registers/Output`
  payloads through `_next_publish` + strip + decode; assert the expected readings.
  No hardware, no broker.
- **Regression:** the SSH path tests are unchanged and still pass.
- **Live:** validate end-to-end against the reference unit (real broker, real
  telemetry) — confirm MQTT readings match the SSH readings and the portal.

## 9. Open items / risks

- **Cross-unit payload compatibility:** the second unit's exact `/Realtime`
  payload bytes are unconfirmed (log description only). To be verified via an
  on-gateway `strace` capture, or naturally when that unit points a daemon at a
  broker. v1 is proven on the reference unit and expected to generalise; the
  decoder is tolerant (CRC-validates, skips non-matching frames).
- **Full register set vs split:** whether `/Realtime` alone carries the full
  input-register set or it is split across `/Realtime` +
  `/Read_All_Input_Registers/Output` — handled by subscribing to both and merging
  all function-04 frames, so either layout works.

## 10. Config / security notes

- MQTT reading requires the broker running and the daemon repointed to it (same
  prerequisite as control). The plaintext device→broker leg stays on the trusted
  LAN; remote access via VPN. No new secrets; broker credentials are the
  per-device ones the broker package issues.
- This document uses placeholders only — no real serials, broker hosts, or
  credentials.
