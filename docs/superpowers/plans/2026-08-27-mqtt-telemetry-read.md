# MQTT Telemetry Read Path — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicitly-selectable MQTT telemetry read source to the OpenHomepower Home Assistant integration, so gateway builds whose daemon doesn't log frames to disk can still be monitored.

**Architecture:** A persistent background MQTT subscriber pulls the daemon's unprompted `0104` input-register frames off the broker, decodes them with the *existing* protocol/registry code, and pushes readings into a push-style `DataUpdateCoordinator`. The SSH log-scraping path is untouched; the read source is chosen once at config time. Both sources produce the identical `dict[str, Reading]` that the existing entities already consume.

**Tech Stack:** Python 3.12, Home Assistant custom integration, dependency-free raw-socket MQTT (reuses `control.py`), `pytest`.

## Global Constraints

- **No new runtime dependencies.** Reuse `control.py`'s MQTT primitives (`_ms`, `_rlen`, `_next_publish`, `mqtt_payload`, `BrokerConfig`). Only `asyncssh` (existing) is permitted in `requirements`.
- **Decoder is shared and unchanged.** All frame decoding goes through `protocol.parse_frame` / `protocol.merge` / `registry.RegisterMap.decode`. Do not fork decode logic.
- **Disclosure-safe.** No real serials, broker hosts, or credentials in code, tests, comments, or commits. Test frames use devsn `b"0000000000"`. Topics use `Enertek/<serial>/...` with `serial` from config.
- **Read-only telemetry.** The MQTT reader only subscribes; it never publishes. Writes remain the opt-in control path.
- **NZ English** in user-facing strings, to match the repo.
- **min HA `2024.6.0`**, per `hacs.json`.
- **Every task ends CI-green:** `python -m pytest tests -q` passes.

## File Structure

- **Create `custom_components/openhomepower/mqtt_reader.py`** — pure payload→frame extraction, a latest-per-block frame cache, and the threaded persistent subscriber. One responsibility: get decoded telemetry frames off the broker.
- **Create `custom_components/openhomepower/mqtt_coordinator.py`** — `MqttReadCoordinator`, a push-style coordinator that decodes frames from the reader into readings and exposes the same interface as `HomepowerCoordinator`.
- **Create `tests/test_mqtt_reader.py`** — extraction + cache tests (HA-free, like `test_control.py`).
- **Modify `custom_components/openhomepower/const.py`** — read-source constants.
- **Modify `custom_components/openhomepower/config_flow.py`** — read-source selection; require broker fields when MQTT.
- **Modify `custom_components/openhomepower/__init__.py`** — build the coordinator that matches the chosen source; lifecycle.
- **Modify `custom_components/openhomepower/strings.json` + `translations/en.json`** — labels for the new field.
- **Modify `custom_components/openhomepower/manifest.json`** — version bump to `0.3.0`.
- **Modify `README.md`** — document the read-source choice.

---

### Task 1: Payload → telemetry frame extraction (pure)

**Files:**
- Create: `custom_components/openhomepower/mqtt_reader.py`
- Test: `tests/test_mqtt_reader.py`

**Interfaces:**
- Consumes: `protocol.parse_frame(raw: bytes) -> Frame` (raises `FrameError`); `protocol.crc16(data: bytes) -> int` (tests only); `control.mqtt_payload(frame: bytes, seq: int) -> bytes` (tests only).
- Produces: `strip_payload(payload: bytes) -> bytes`; `telemetry_frame(payload: bytes) -> Frame | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mqtt_reader.py
"""MQTT telemetry extraction, verified without Home Assistant.

Imports through the synthetic `openhomepower` package that conftest.py
registers — the same pattern test_entities.py uses — so the modules keep
plain relative imports and need no test-only fallbacks.
"""
import struct

from openhomepower.control import mqtt_payload
from openhomepower.mqtt_reader import telemetry_frame
from openhomepower.protocol import crc16


def make_frame(start: int, regs: list[int], devsn: bytes = b"0000000000") -> bytes:
    """Build a CRC-valid function-04 response frame (secret-free devsn)."""
    body = (b"\x01\x04" + devsn + struct.pack("<H", start)
            + bytes([len(regs) * 2])
            + b"".join(struct.pack("<H", r) for r in regs))
    return body + struct.pack("<H", crc16(body))


def test_telemetry_frame_extracts_from_wrapped_payload():
    frame = make_frame(0, [70, 3520, 118])          # SOC-ish/power-ish sample
    payload = mqtt_payload(frame, seq=3)             # [0x33][0x02] + frame
    out = telemetry_frame(payload)
    assert out is not None
    assert out.start == 0
    assert out.registers == (70, 3520, 118)


def test_telemetry_frame_rejects_non_frame_payload():
    assert telemetry_frame(b"\x33\x02hello") is None       # not a frame
    assert telemetry_frame(b"") is None                    # empty
    bad = bytearray(mqtt_payload(make_frame(0, [1, 2]), 1))
    bad[-1] ^= 0xFF                                         # corrupt CRC
    assert telemetry_frame(bytes(bad)) is None
```

Import new symbols at the top of the test file as later tasks add them
(`FrameCache`, `merge`, `readings_from_frames`) — keep the plain
`from openhomepower.… import …` form; never reintroduce an `importlib`/`_load`
helper.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mqtt_reader.py -q`
Expected: FAIL — `mqtt_reader` has no attribute `telemetry_frame` (module doesn't exist yet).

- [ ] **Step 3: Write minimal implementation**

```python
# custom_components/openhomepower/mqtt_reader.py
"""Read Homepower telemetry off an MQTT broker.

The gateway daemon publishes its input-register frames to the broker unprompted
(`Enertek/<serial>/Realtime` and `.../Read_All_Input_Registers/Output`). This
module subscribes, pulls the `0104` frames out of the payloads, and hands them
to a callback — the same frames the SSH path scrapes from the log, so the shared
decoder in protocol.py/registry.py does the rest.

READ-ONLY: this module only subscribes; it never publishes.
"""
from __future__ import annotations

from .protocol import Frame, FrameError, parse_frame  # plain relative import — see conftest


def strip_payload(payload: bytes) -> bytes:
    """Remove the daemon's `[seq-digit][0x02]` wrapper if present."""
    if len(payload) > 2 and payload[1] == 0x02:
        return payload[2:]
    return payload


def telemetry_frame(payload: bytes) -> Frame | None:
    """Parse a wrapped MQTT payload into a telemetry Frame, or None if it isn't one.

    Non-frame payloads and CRC failures return None (never raise), so a stray
    publish can't take the subscriber down.
    """
    try:
        return parse_frame(strip_payload(payload))
    except FrameError:
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mqtt_reader.py -q`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add tests/test_mqtt_reader.py custom_components/openhomepower/mqtt_reader.py
git commit -m "feat(mqtt-read): extract telemetry frames from MQTT payloads"
```

---

### Task 2: Latest-per-block frame cache (pure)

**Files:**
- Modify: `custom_components/openhomepower/mqtt_reader.py`
- Test: `tests/test_mqtt_reader.py`

**Interfaces:**
- Produces: `class FrameCache` with `update(frame: Frame) -> None` and `frames() -> list[Frame]`. Keeps the newest frame per `frame.start`, so `merge()` of `frames()` yields the full register map once every block has been seen once.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mqtt_reader.py — add to the imports at the top:
#   from openhomepower.mqtt_reader import FrameCache
#   from openhomepower.protocol import merge
# then append:
def test_frame_cache_keeps_latest_per_block():
    cache = FrameCache()
    cache.update(make_frame(0, [1, 2]))
    cache.update(make_frame(76, [9]))
    cache.update(make_frame(0, [1, 3]))          # newer block-0 frame wins
    merged = merge(cache.frames())
    assert merged[0] == 1 and merged[1] == 3     # block 0 updated
    assert merged[76] == 9                        # block 76 retained
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mqtt_reader.py::test_frame_cache_keeps_latest_per_block -q`
Expected: FAIL — `FrameCache` not defined.

- [ ] **Step 3: Write minimal implementation**

```python
# add to mqtt_reader.py
class FrameCache:
    """Newest frame per register block, so a full read is a merge of all blocks."""

    def __init__(self) -> None:
        self._by_start: dict[int, Frame] = {}

    def update(self, frame: Frame) -> None:
        self._by_start[frame.start] = frame

    def frames(self) -> list[Frame]:
        return list(self._by_start.values())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mqtt_reader.py -q`
Expected: PASS (all three tests).

- [ ] **Step 5: Commit**

```bash
git add tests/test_mqtt_reader.py custom_components/openhomepower/mqtt_reader.py
git commit -m "feat(mqtt-read): cache latest frame per register block"
```

---

### Task 3: Persistent MQTT subscriber thread

**Files:**
- Modify: `custom_components/openhomepower/mqtt_reader.py`

**Interfaces:**
- Consumes: `control._ms`, `control._rlen`, `control._next_publish`, `control.BrokerConfig`.
- Produces: `class MqttReader` — `__init__(self, cfg: BrokerConfig, on_update: Callable[[list[Frame]], None], topics: list[str] | None = None)`; `start() -> None`; `stop() -> None`. Calls `on_update(frames)` (from the reader thread) after each telemetry frame, passing the current merged block set.

This task is transport/threading with no pure unit under test; its logic (extraction, caching) is already covered by Tasks 1–2. Verify it in the live checklist (Task 7). Keep the socket handling a faithful mirror of `MqttControl._connect`/`read`.

- [ ] **Step 1: Implement `MqttReader`**

```python
# add to mqtt_reader.py — new imports at top:
#   import logging, socket, struct, threading, time
#   from collections.abc import Callable
#   from . import control
#   from .control import BrokerConfig
_LOGGER = logging.getLogger(__name__)

_KEEPALIVE = 20            # PINGREQ cadence (< the 30 s CONNECT keepalive)
_BACKOFF_MAX = 60


class MqttReader:
    """Subscribe to the daemon's telemetry topics and emit decoded frames.

    Runs a daemon thread. Reuses control.py's raw-socket MQTT helpers so there is
    no new dependency. Reconnects with backoff until stop() is called.
    """

    def __init__(self, cfg: BrokerConfig,
                 on_update: Callable[[list[Frame]], None],
                 topics: list[str] | None = None) -> None:
        self.cfg = cfg
        self._on_update = on_update
        self._topics = topics or [
            f"Enertek/{cfg.serial}/Realtime",
            f"Enertek/{cfg.serial}/Read_All_Input_Registers/Output",
        ]
        self._cache = FrameCache()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="openhomepower-mqtt-reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 1
            except (OSError, ConnectionError) as err:
                if self._stop.is_set():
                    break
                _LOGGER.debug("mqtt reader session ended (%s); retry in %ds", err, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    def _session(self) -> None:
        s = socket.create_connection((self.cfg.host, self.cfg.port), timeout=10)
        self._sock = s
        try:
            s.settimeout(3)
            body = (control._ms(b"MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 30)
                    + control._ms(self.cfg.client_id.encode())
                    + control._ms(self.cfg.username.encode())
                    + control._ms(self.cfg.password.encode()))
            s.send(bytes([0x10]) + control._rlen(len(body)) + body)
            time.sleep(0.4)
            ca = s.recv(4)
            if not (len(ca) >= 4 and ca[0] == 0x20 and ca[3] == 0):
                raise ConnectionError(f"CONNACK failed: {ca!r}")
            for i, topic in enumerate(self._topics):
                sub = struct.pack("!H", i + 1) + control._ms(topic.encode()) + bytes([0])
                s.send(bytes([0x82]) + control._rlen(len(sub)) + sub)
                time.sleep(0.2)
                s.recv(64)                       # drain SUBACK
            self._recv_loop(s)
        finally:
            self._sock = None
            s.close()

    def _recv_loop(self, s: socket.socket) -> None:
        buf = b""
        last_ping = time.time()
        while not self._stop.is_set():
            if time.time() - last_ping > _KEEPALIVE:
                s.send(b"\xc0\x00")              # PINGREQ
                last_ping = time.time()
            try:
                data = s.recv(4096)
            except socket.timeout:
                continue
            if not data:
                raise ConnectionError("broker closed the connection")
            buf += data
            while True:
                topic, payload, buf = control._next_publish(buf)
                if topic is None:
                    break
                frame = telemetry_frame(payload)
                if frame is not None:
                    self._cache.update(frame)
                    self._on_update(self._cache.frames())
```

- [ ] **Step 2: Static check it imports cleanly**

Import it through a synthetic `openhomepower` package (the same trick
`conftest.py` uses), so the module's plain relative imports resolve without
dragging in Home Assistant:

Run: `python -c "import sys,types; p=types.ModuleType('openhomepower'); p.__path__=['custom_components/openhomepower']; sys.modules['openhomepower']=p; import openhomepower.mqtt_reader; print('ok')"`
Expected: `ok` (module parses and imports; `control` and `protocol` resolve).

- [ ] **Step 3: Run the suite (Tasks 1–2 still green)**

Run: `python -m pytest tests -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add custom_components/openhomepower/mqtt_reader.py
git commit -m "feat(mqtt-read): persistent broker subscriber thread"
```

---

### Task 4: Read-source config (const + config flow)

**Files:**
- Modify: `custom_components/openhomepower/const.py`
- Modify: `custom_components/openhomepower/config_flow.py`
- Modify: `custom_components/openhomepower/strings.json`, `custom_components/openhomepower/translations/en.json`

**Interfaces:**
- Produces (const): `CONF_READ_SOURCE = "read_source"`, `READ_SOURCE_SSH = "ssh"`, `READ_SOURCE_MQTT = "mqtt"`, `DEFAULT_READ_SOURCE = READ_SOURCE_SSH`, `DEFAULT_STALE_SECONDS = 180`, `CONF_STALE_SECONDS = "stale_seconds"`.
- Config flow stores `CONF_READ_SOURCE` plus, for MQTT, `CONF_BROKER_HOST/PORT/USER/PASSWORD` + `CONF_TOPIC_SERIAL` in `entry.data`.

- [ ] **Step 1: Add constants**

```python
# add to const.py (after DEFAULT_BROKER_PORT block)
CONF_READ_SOURCE = "read_source"
READ_SOURCE_SSH = "ssh"
READ_SOURCE_MQTT = "mqtt"
DEFAULT_READ_SOURCE = READ_SOURCE_SSH

# Mark telemetry stale (entities unavailable) after this long with no publish.
# ~3x the daemon's slow idle cadence; overridable per entry.
CONF_STALE_SECONDS = "stale_seconds"
DEFAULT_STALE_SECONDS = 180
```

- [ ] **Step 2: Add the read-source field and MQTT branch to `async_step_user`**

In `config_flow.py`, extend the setup form so the user picks a source; when `mqtt`, collect broker fields and skip the SSH probe (validate the broker instead). Replace the schema block and probe branch:

```python
# config_flow.py — imports
from .const import (
    CONF_READ_SOURCE, READ_SOURCE_SSH, READ_SOURCE_MQTT, DEFAULT_READ_SOURCE,
    CONF_BROKER_HOST, CONF_BROKER_PORT, CONF_BROKER_USER, CONF_BROKER_PASSWORD,
    CONF_TOPIC_SERIAL, DEFAULT_BROKER_PORT,
    # ...existing imports kept...
)
import voluptuous as vol
from homeassistant.helpers.selector import (
    SelectSelector, SelectSelectorConfig, SelectOptionDict, SelectSelectorMode,
)
```

```python
# inside async_step_user, when user_input is not None:
source = user_input.get(CONF_READ_SOURCE, DEFAULT_READ_SOURCE)
if source == READ_SOURCE_MQTT:
    serial, error = await self._async_probe_mqtt(user_input)
    if error:
        errors["base"] = error
    else:
        await self.async_set_unique_id(serial or user_input[CONF_TOPIC_SERIAL])
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Energizer Homepower",
            data={
                CONF_READ_SOURCE: READ_SOURCE_MQTT,
                CONF_HOST: user_input.get(CONF_HOST, "").strip(),
                CONF_BROKER_HOST: user_input[CONF_BROKER_HOST].strip(),
                CONF_BROKER_PORT: user_input.get(CONF_BROKER_PORT, DEFAULT_BROKER_PORT),
                CONF_BROKER_USER: user_input[CONF_BROKER_USER].strip(),
                CONF_BROKER_PASSWORD: user_input[CONF_BROKER_PASSWORD],
                CONF_TOPIC_SERIAL: user_input[CONF_TOPIC_SERIAL].strip(),
            },
        )
    # fall through to re-show the form with errors
else:
    # existing SSH path unchanged (probe + create entry), but stamp the source:
    #   data={CONF_READ_SOURCE: READ_SOURCE_SSH, CONF_HOST: host, ...existing...}
    ...
```

Add the source selector to the form schema (shown in both branches):

```python
_SOURCE_FIELD = {
    vol.Required(CONF_READ_SOURCE, default=DEFAULT_READ_SOURCE): SelectSelector(
        SelectSelectorConfig(
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="read_source",
            options=[
                SelectOptionDict(value=READ_SOURCE_SSH, label="SSH log (default)"),
                SelectOptionDict(value=READ_SOURCE_MQTT, label="MQTT broker"),
            ],
        )
    ),
    vol.Optional(CONF_BROKER_HOST, default=""): str,
    vol.Optional(CONF_BROKER_PORT, default=DEFAULT_BROKER_PORT): int,
    vol.Optional(CONF_BROKER_USER, default=""): str,
    vol.Optional(CONF_BROKER_PASSWORD, default=""): str,
    vol.Optional(CONF_TOPIC_SERIAL, default=""): str,
}
# merge _SOURCE_FIELD into the existing vol.Schema({...}) dict for the user step.
```

Add the MQTT probe (starts a short-lived reader, waits for one telemetry frame):

```python
async def _async_probe_mqtt(self, data) -> tuple[str | None, str | None]:
    """Confirm the broker yields decodable telemetry. Returns (serial, error_key)."""
    from .control import BrokerConfig
    from .mqtt_reader import MqttReader
    import asyncio

    missing = [k for k in (CONF_BROKER_HOST, CONF_BROKER_USER,
                           CONF_BROKER_PASSWORD, CONF_TOPIC_SERIAL)
               if not str(data.get(k, "")).strip()]
    if missing:
        return None, "mqtt_fields_missing"

    serial = str(data[CONF_TOPIC_SERIAL]).strip()
    cfg = BrokerConfig(
        host=str(data[CONF_BROKER_HOST]).strip(),
        port=int(data.get(CONF_BROKER_PORT, DEFAULT_BROKER_PORT)),
        username=str(data[CONF_BROKER_USER]).strip(),
        password=str(data[CONF_BROKER_PASSWORD]),
        serial=serial,
        client_id=f"openhomepower-ha-probe-{serial}",
    )
    got = asyncio.Event()
    frames_seen: list = []

    def _on_update(frames):
        frames_seen[:] = frames
        self.hass.loop.call_soon_threadsafe(got.set)

    reader = MqttReader(cfg, _on_update)
    reader.start()
    try:
        await asyncio.wait_for(got.wait(), timeout=25)
    except asyncio.TimeoutError:
        return None, "cannot_connect"
    finally:
        await self.hass.async_add_executor_job(reader.stop)

    readings = self.regmap.decode(merge(frames_seen)) if frames_seen else {}
    if not readings:
        return None, "no_data"
    dev = readings.get("device_serial")
    return (str(dev.value) if dev else serial), None
```

(`self.regmap` — load it once in `async_step_user` as the SSH branch already does via `RegisterMap.load`; reuse it. `merge` is already imported from `.protocol`.)

- [ ] **Step 3: Add strings for the new field and errors**

```json
// strings.json + translations/en.json — under config.step.user.data:
"read_source": "Telemetry source",
"broker_host": "Broker host (MQTT source)",
"broker_port": "Broker port",
"broker_user": "Broker username",
"broker_password": "Broker password",
"topic_serial": "MQTT topic serial"
// and under config.error:
"mqtt_fields_missing": "MQTT source needs the broker host, username, password and topic serial.",
"no_data": "Connected, but no decodable telemetry arrived."
```

- [ ] **Step 4: Run the suite + hassfest-style JSON check**

Run: `python -m pytest tests -q && python -c "import json; json.load(open('custom_components/openhomepower/strings.json')); json.load(open('custom_components/openhomepower/translations/en.json'))"`
Expected: tests PASS; JSON loads without error. (No literal `{...}` in any description string — hassfest reads those as placeholders.)

- [ ] **Step 5: Commit**

```bash
git add custom_components/openhomepower/const.py custom_components/openhomepower/config_flow.py custom_components/openhomepower/strings.json custom_components/openhomepower/translations/en.json
git commit -m "feat(mqtt-read): read-source selection in config flow"
```

---

### Task 5: Push coordinator

**Files:**
- Create: `custom_components/openhomepower/mqtt_coordinator.py`
- Test: `tests/test_mqtt_reader.py` (decode-path unit; HA-free portion)

**Interfaces:**
- Consumes: `MqttReader`, `BrokerConfig`, `Credentials`, `Gateway`, `RegisterMap`, `protocol.merge`.
- Produces: `class MqttReadCoordinator(DataUpdateCoordinator[dict[str, Reading]])` exposing `.data`, `.regmap`, `.device_serial`, `.reading_age`, `.gateway`, and `async_start()` / `async_shutdown()`. Interface-compatible with `HomepowerCoordinator` for the entities.

- [ ] **Step 1: Write the failing decode-path test**

The event-loop wiring needs HA, but the pure decode step (frames → readings dict) is testable. Add a module-level helper and test it:

```python
# tests/test_mqtt_reader.py — add to the imports at the top:
#   from openhomepower.mqtt_reader import readings_from_frames
# then append:
def test_readings_from_frames_decodes_via_regmap():
    # A minimal fake regmap proves the coordinator delegates to merge()+decode().
    class FakeRegmap:
        def decode(self, regs):
            return {"raw_reg0": regs.get(0)}
    frames = [make_frame(0, [42, 7])]
    readings = readings_from_frames(FakeRegmap(), frames)
    assert readings == {"raw_reg0": 42}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mqtt_reader.py::test_readings_from_frames_decodes_via_regmap -q`
Expected: FAIL — `readings_from_frames` not defined.

- [ ] **Step 3: Add the shared decode helper to `mqtt_reader.py`**

```python
# add to mqtt_reader.py
from .protocol import merge

def readings_from_frames(regmap, frames: list[Frame]):
    """Merge frames into a register map and decode — the shared telemetry step."""
    return regmap.decode(merge(frames))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mqtt_reader.py -q`
Expected: PASS.

- [ ] **Step 5: Implement the coordinator**

```python
# custom_components/openhomepower/mqtt_coordinator.py
"""Push coordinator: telemetry arrives from the MQTT reader, not a poll."""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .control import BrokerConfig
from .mqtt_reader import MqttReader, readings_from_frames
from .registry import Reading, RegisterMap
from .transport import Credentials, Gateway

_LOGGER = logging.getLogger(__name__)


class MqttReadCoordinator(DataUpdateCoordinator[dict[str, Reading]]):
    """Same data contract as HomepowerCoordinator, fed by a broker subscription.

    A `gateway` is kept (for the opt-in control path and unload symmetry) but
    telemetry never uses it — it comes from the reader.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, regmap: RegisterMap,
                 creds: Credentials, broker: BrokerConfig, stale_seconds: int) -> None:
        # A timer that only checks staleness; fresh data arrives via push.
        super().__init__(hass, _LOGGER, name="OpenHomepower (MQTT)",
                         update_interval=timedelta(seconds=stale_seconds))
        self.entry = entry
        self.regmap = regmap
        self.gateway = Gateway(creds)
        self.device_serial: str | None = None
        self.last_success: float | None = None
        self._stale_seconds = stale_seconds
        self._reader = MqttReader(broker, self._on_update)

    @property
    def reading_age(self) -> float | None:
        if self.last_success is None:
            return None
        return round(time.monotonic() - self.last_success, 1)

    def _on_update(self, frames) -> None:
        """Called from the reader thread; marshal onto the event loop."""
        readings = readings_from_frames(self.regmap, frames)
        if not readings:
            return
        self.hass.loop.call_soon_threadsafe(self._apply, readings, frames)

    @callback
    def _apply(self, readings: dict[str, Reading], frames) -> None:
        self.last_success = time.monotonic()
        if self.device_serial is None:
            serial = readings.get("device_serial")
            if serial is not None:
                self.device_serial = str(serial.value)
            elif frames:
                self.device_serial = frames[0].devsn
        self.async_set_updated_data(readings)

    async def _async_update_data(self) -> dict[str, Reading]:
        """Staleness watchdog only — real updates come from _apply()."""
        if self.data and self.reading_age is not None \
                and self.reading_age <= self._stale_seconds:
            return self.data
        if self.data:
            raise UpdateFailed(f"no telemetry for {self.reading_age}s")
        raise UpdateFailed("no telemetry received yet")

    async def async_start(self) -> None:
        await self.hass.async_add_executor_job(self._reader.start)

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.hass.async_add_executor_job(self._reader.stop)
        await self.gateway.close()
```

- [ ] **Step 6: Run the suite**

Run: `python -m pytest tests -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add custom_components/openhomepower/mqtt_coordinator.py custom_components/openhomepower/mqtt_reader.py tests/test_mqtt_reader.py
git commit -m "feat(mqtt-read): push coordinator fed by the broker subscriber"
```

---

### Task 6: Wire the coordinator selection into setup

**Files:**
- Modify: `custom_components/openhomepower/__init__.py`
- Modify: `custom_components/openhomepower/manifest.json`

**Interfaces:**
- Consumes: `MqttReadCoordinator`, `_broker_config` (existing), `CONF_READ_SOURCE`, `CONF_STALE_SECONDS`, `DEFAULT_STALE_SECONDS`.

- [ ] **Step 1: Build the matching coordinator in `async_setup_entry`**

Replace the coordinator-construction block. The MQTT branch reuses `_broker_config`'s validation shape but reads broker settings from `entry.data` (they were collected at setup, not options):

```python
# __init__.py — imports
from .const import (
    CONF_READ_SOURCE, READ_SOURCE_MQTT, DEFAULT_READ_SOURCE,
    CONF_STALE_SECONDS, DEFAULT_STALE_SECONDS,
    # ...existing...
)
from .mqtt_coordinator import MqttReadCoordinator
```

```python
# in async_setup_entry, after loading regmap + building creds:
source = entry.data.get(CONF_READ_SOURCE, DEFAULT_READ_SOURCE)

if source == READ_SOURCE_MQTT:
    broker = BrokerConfig(
        host=str(entry.data[CONF_BROKER_HOST]).strip(),
        port=int(entry.data.get(CONF_BROKER_PORT, DEFAULT_BROKER_PORT)),
        username=str(entry.data[CONF_BROKER_USER]).strip(),
        password=str(entry.data[CONF_BROKER_PASSWORD]),
        serial=str(entry.data[CONF_TOPIC_SERIAL]).strip(),
        client_id=f"openhomepower-ha-read-{str(entry.data[CONF_TOPIC_SERIAL]).strip()}",
    )
    coordinator = MqttReadCoordinator(
        hass, entry, regmap, creds, broker,
        entry.options.get(CONF_STALE_SECONDS,
                          entry.data.get(CONF_STALE_SECONDS, DEFAULT_STALE_SECONDS)),
    )
    await coordinator.async_start()
    await coordinator.async_config_entry_first_refresh()
else:
    coordinator = HomepowerCoordinator(
        hass, entry, regmap, creds,
        entry.options.get(CONF_POLL_SECONDS,
                          entry.data.get(CONF_POLL_SECONDS, DEFAULT_POLL_SECONDS)),
    )
    await coordinator.async_config_entry_first_refresh()
```

Note: the first-refresh watchdog fails if no telemetry has arrived yet. `async_start()` begins the subscription before `async_config_entry_first_refresh()`; the reader delivers a frame within a publish cycle. If setup is flaky on a slow idle battery, widen the first-refresh tolerance by seeding `last_success` in `async_start` only after the first `_apply`. Keep as-is for v1 and confirm in the live checklist.

- [ ] **Step 2: Ensure unload/shutdown handles both coordinators**

`async_unload_entry` currently calls `store["coordinator"].gateway.close()`. Both coordinators expose `.gateway`, so this still works. Add reader shutdown for the MQTT case:

```python
# in async_unload_entry, replace the close line:
coordinator = store["coordinator"]
if hasattr(coordinator, "async_shutdown"):
    await coordinator.async_shutdown()      # stops the reader (MQTT) and closes the gateway
else:
    await coordinator.gateway.close()
```

(`HomepowerCoordinator.async_shutdown` already closes its gateway, so this is uniform.)

- [ ] **Step 3: Bump the version**

```json
// manifest.json
"version": "0.3.0"
```

- [ ] **Step 4: Run the suite**

Run: `python -m pytest tests -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/openhomepower/__init__.py custom_components/openhomepower/manifest.json
git commit -m "feat(mqtt-read): select coordinator by read source; bump to 0.3.0"
```

---

### Task 7: Docs + validation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document the read-source choice in the README**

Under **Setup**, add a short subsection:

```markdown
### Telemetry source: SSH or MQTT

Most units work over **SSH** (the default) — no broker needed. Some gateway
builds don't log the raw data to disk; those can't be read over SSH, so pick
**MQTT** at setup instead. MQTT reading needs the broker running and the daemon
repointed to it (see the openhomepower-broker project), and you enter the broker
host, credentials, and topic serial. Everything downstream — sensors, the Energy
Dashboard — is identical either way.
```

- [ ] **Step 2: Full suite + JSON validation**

Run: `python -m pytest tests -q && python -c "import json,glob; [json.load(open(f)) for f in glob.glob('custom_components/openhomepower/**/*.json', recursive=True)]"`
Expected: PASS; all JSON valid.

- [ ] **Step 3: Secret scan before pushing**

Run: `git grep -nE "2103200212|0512017220|in\.tm\.enertek|testen|0401910433" -- . ':!docs' || echo "clean"`
Expected: `clean` (no real serials, vendor host, or fleet creds in code/tests).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs(mqtt-read): document SSH vs MQTT telemetry source"
```

- [ ] **Step 5: Live validation (manual, against the reference unit)**

1. Stand up the local broker (openhomepower-broker add-on), provision the device, repoint the daemon.
2. Add the integration a second time with **read source = MQTT**, pointing at the broker + topic serial.
3. Confirm sensors populate within one publish cycle and match the SSH entry and the portal (SOC, discharge/charge power, grid import/export).
4. Confirm the Energy Dashboard daily counters advance.
5. Stop the broker briefly; confirm entities go unavailable after ~`stale_seconds`, then recover when it returns.

---

## Self-Review

**Spec coverage** (spec §→task):
- §4 architecture (two sources, one data contract) → Tasks 5–6 (coordinator + selection).
- §5 data flow (subscribe → strip → 0104 → merge → decode) → Tasks 1–3, 5.
- §6 components: `mqtt_reader.py` → Tasks 1–3, 5; `config_flow.py` → Task 4; `__init__.py` → Task 6; `const.py` → Task 4; decoder unchanged → reused throughout.
- §7 error handling (reconnect/backoff, availability watchdog, setup validation) → Task 3 (backoff), Task 5 (`_async_update_data` staleness), Task 4 (`_async_probe_mqtt`).
- §8 testing (captured/constructed payloads, no hardware; live) → Tasks 1–2, 5 (unit), Task 7 (live).
- §9 open items (cross-unit payload; block split) → block split handled by subscribing to both topics + `FrameCache` merge (Task 3); cross-unit confirmation is the flagged out-of-band strace item.
- §10 security → Global Constraints + Task 7 secret scan.

**Placeholder scan:** none — every step has concrete code or an exact command.

**Type consistency:** `telemetry_frame`/`strip_payload`/`FrameCache`/`readings_from_frames`/`MqttReader` names are used identically across Tasks 1–6; `MqttReader(cfg, on_update, topics=None)` and `on_update(frames: list[Frame])` match between reader (Task 3), probe (Task 4), and coordinator (Task 5); coordinator exposes the same `.data/.regmap/.device_serial/.reading_age/.gateway` the entities read (`sensor.py`).
