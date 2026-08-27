# MQTT Control Read-back / SSH-free MQTT Mode — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make control read its config over MQTT on MQTT-source entries, so the whole integration runs with no SSH at all when the read source is MQTT, and default new installs to MQTT.

**Architecture:** `ControlCoordinator` becomes source-agnostic — it takes a *config reader* (`SshConfigReader` or `MqttConfigReader`) that returns `dict[int, int]`, decoded by the unchanged `control_state_from_regs`. The MQTT reader calls a new `MqttControl.read_config()` (reusing the existing `MqttControl.read()` fn-03 request/response). In MQTT mode no SSH `Gateway` is constructed. MQTT becomes the default read source.

**Tech Stack:** Python 3.12, Home Assistant custom integration, dependency-free raw-socket MQTT (reuses `control.py`), `pytest`.

## Global Constraints

- **No new runtime dependencies.** Reuse `MqttControl.read`; only `asyncssh` (existing) is permitted in `requirements`.
- **Decoder shared and unchanged.** All decode goes through `control.parse_holding_frames` / `control.control_state_from_regs`. Do not fork them.
- **Read-only.** `read_config` issues only fn-03 read frames; it never publishes a write frame. Writes stay the opt-in control path.
- **Control read source follows the entry read source.** SSH entries keep SSH read-back (unchanged); MQTT entries read over MQTT. Writes are MQTT for both.
- **Disclosure-safe.** No real serials, broker hosts, or credentials in code, tests, comments, or commits. Test frames use devsn `b"0000000000"`; the topic serial in tests is `"0000000000"`.
- **NZ English** in user-facing strings.
- **min HA `2024.6.0`.**
- **Version `0.4.0`** in `manifest.json`.
- **Every task ends CI-green:** `python3 -m pytest tests -q` passes (currently 26 tests).

## File Structure

- **Modify `custom_components/openhomepower/control.py`** — add `MqttControl.read_config()`.
- **Modify `custom_components/openhomepower/control_coordinator.py`** — add `SshConfigReader` / `MqttConfigReader`; make `ControlCoordinator` consume a reader instead of a gateway.
- **Modify `custom_components/openhomepower/__init__.py`** — build the config reader by read source; construct the SSH `Credentials`/gateway only in SSH mode.
- **Modify `custom_components/openhomepower/mqtt_coordinator.py`** — make the SSH `gateway` optional (`None` in SSH-free mode).
- **Modify `custom_components/openhomepower/config_flow.py`** — host optional; MQTT entries stored SSH-free; SSH-free option derivation; default label.
- **Modify `custom_components/openhomepower/const.py`** — `DEFAULT_READ_SOURCE = READ_SOURCE_MQTT`.
- **Modify `custom_components/openhomepower/strings.json` + `translations/en.json`** — `host_required` error.
- **Modify `custom_components/openhomepower/manifest.json`** — version `0.4.0`.
- **Modify `README.md`** — document SSH-free MQTT and the default change.
- **Create `tests/test_const.py`** — assert the default read source.
- **Modify `tests/test_control.py`** — `read_config` test.

---

### Task 1: `MqttControl.read_config()`

**Files:**
- Modify: `custom_components/openhomepower/control.py`
- Test: `tests/test_control.py`

**Interfaces:**
- Consumes: `MqttControl.read(reg: int, count: int, timeout: int = 15) -> list[int]` (existing); module constants `REG_MAX_SOC=67`, `REG_RESERVE_ON=105`, `REG_RESERVE_BLOCK=120`, `REG_MODE=231` (existing); `control_state_from_regs` (existing).
- Produces: `MqttControl.read_config(self) -> dict[int, int]` — the merged holding registers control needs.

- [ ] **Step 1: Write the failing test**

`tests/test_control.py` already imports `control` as a standalone module (`ohp_control`) and exercises `parse_holding_frames` / `control_state_from_regs`. Append this test (it stubs `read` so no socket/broker is involved):

```python
def test_read_config_assembles_registers(monkeypatch):
    cfg = control.BrokerConfig(host="h", port=1, username="u", password="p",
                               serial="0000000000")
    mc = control.MqttControl(cfg)
    calls = []

    def fake_read(reg, count, timeout=15):
        calls.append((reg, count))
        return {67: [90], 105: [5], 120: [100, 8, 2, 40], 231: [2]}[reg]

    monkeypatch.setattr(mc, "read", fake_read)
    regs = mc.read_config()

    assert regs == {67: 90, 105: 5, 120: 100, 121: 8, 122: 2, 123: 40, 231: 2}
    assert calls == [(67, 1), (105, 1), (120, 4), (231, 1)]   # minimal fn-03 reads
    # and it decodes to the control state the entities show
    assert control.control_state_from_regs(regs) == {
        "mode": "semi", "max_soc": 90, "reserve_on": 5,
        "reserve_off": 8, "excess": 40}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_control.py::test_read_config_assembles_registers -q`
Expected: FAIL — `MqttControl` has no attribute `read_config`.

- [ ] **Step 3: Write minimal implementation**

Add this method to the `MqttControl` class in `control.py`, immediately after the existing `read()` method:

```python
    def read_config(self) -> dict[int, int]:
        """Read the control holding registers over MQTT (fn-03, read-only).

        Blocking — call via executor. Reuses read(); one short connection per
        block. Returns {reg: value} covering everything control_state_from_regs
        needs: mode (231), max-SoC (67), reserve-on (105), and the 120-123 block
        (reserve-off 121 + excess 123).
        """
        regs: dict[int, int] = {}
        regs[REG_MAX_SOC] = self.read(REG_MAX_SOC, 1)[0]
        regs[REG_RESERVE_ON] = self.read(REG_RESERVE_ON, 1)[0]
        for i, value in enumerate(self.read(REG_RESERVE_BLOCK, 4)):
            regs[REG_RESERVE_BLOCK + i] = value
        regs[REG_MODE] = self.read(REG_MODE, 1)[0]
        return regs
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_control.py -q`
Expected: PASS (all control tests, including the new one).

- [ ] **Step 5: Commit**

```bash
git add custom_components/openhomepower/control.py tests/test_control.py
git commit -m "feat(control): read holding config over MQTT (read_config)"
```

---

### Task 2: Source-agnostic control read-back + SSH-free MQTT wiring

**Files:**
- Modify: `custom_components/openhomepower/control_coordinator.py`
- Modify: `custom_components/openhomepower/__init__.py`
- Modify: `custom_components/openhomepower/mqtt_coordinator.py`

**Interfaces:**
- Consumes: `MqttControl.read_config` (Task 1); `control.parse_holding_frames`, `control.control_state_from_regs` (existing); `Gateway.read_holding`, `Gateway.close`, `TransportError` (existing); `MqttReadCoordinator.__init__(hass, entry, regmap, creds, broker, stale_seconds)` (existing, `creds` becomes optional).
- Produces: `class SshConfigReader` and `class MqttConfigReader`, each with `async read_regs(self) -> dict[int, int]`; `ControlCoordinator.__init__(self, hass, reader, mqtt)` (was `(hass, gateway, mqtt)`).

This task is HA-wired; the pure decode it relies on is already covered by `test_control.py`. There is no new unit test — validate with `ast.parse` + the full suite staying green, and the live checklist (Task 4). Trace the setup/unload flow by hand.

- [ ] **Step 1: Rewrite `control_coordinator.py`**

Replace the whole file with:

```python
"""Coordinator for the control entities.

Reads the writable config (mode / max-SoC / reserve / excess) via a
source-specific reader: SSH entries scrape the gateway's log, MQTT entries do an
fn-03 read over the broker. Only *writes* go over MQTT (`self.mqtt`). Config
changes rarely, so this polls slowly (CONTROL_SCAN_INTERVAL)."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import control
from .const import CONTROL_SCAN_INTERVAL, DOMAIN, MANUFACTURER, MODEL
from .control import MqttControl
from .transport import Gateway, TransportError

_LOGGER = logging.getLogger(__name__)


def control_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Same device the sensors attach to, so control lands on the one card."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        name="Energizer Homepower",
        manufacturer=MANUFACTURER,
        model=MODEL,
    )


class SshConfigReader:
    """Read control config from the gateway's SSH log (fn-03 holding frames)."""

    def __init__(self, gateway: Gateway) -> None:
        self._gateway = gateway

    async def read_regs(self) -> dict[int, int]:
        tokens = await self._gateway.read_holding()
        return control.parse_holding_frames(tokens)


class MqttConfigReader:
    """Read control config over MQTT (fn-03 request/response on the broker)."""

    def __init__(self, hass: HomeAssistant, mqtt: MqttControl) -> None:
        self._hass = hass
        self._mqtt = mqtt

    async def read_regs(self) -> dict[int, int]:
        return await self._hass.async_add_executor_job(self._mqtt.read_config)


class ControlCoordinator(DataUpdateCoordinator[dict]):
    """Reads config via a source-specific reader; writes go via MQTT."""

    def __init__(self, hass: HomeAssistant,
                 reader: SshConfigReader | MqttConfigReader,
                 mqtt: MqttControl) -> None:
        super().__init__(
            hass, _LOGGER, name="OpenHomepower control",
            update_interval=CONTROL_SCAN_INTERVAL,
        )
        self._reader = reader
        self.mqtt = mqtt

    async def _async_update_data(self) -> dict:
        try:
            regs = await self._reader.read_regs()
        except (TransportError, OSError) as err:
            # TransportError = SSH; OSError covers the MQTT reader's
            # TimeoutError / ConnectionError (both OSError subclasses).
            raise UpdateFailed(f"control read failed: {err}") from err
        # Keep the last-known value for any register not in this batch — config
        # only changes when someone writes it, so a stale-but-unchanged value is
        # still the correct value.
        state = dict(self.data or {})
        for key, value in control.control_state_from_regs(regs).items():
            if value is not None:
                state[key] = value
        return state
```

- [ ] **Step 2: Confirm nothing else read `ControlCoordinator.gateway`**

Run: `grep -rn "control_coordinator\|ControlCoordinator" custom_components/openhomepower | grep -i gateway`
Expected: no matches (the only `.gateway` users are the read coordinators, not the control one). If a match appears, stop and report it.

- [ ] **Step 3: Make `MqttReadCoordinator`'s gateway optional**

In `mqtt_coordinator.py`, change the constructor signature and the two gateway lines. Constructor `creds` param:

```python
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, regmap: RegisterMap,
                 creds: Credentials | None, broker: BrokerConfig, stale_seconds: int) -> None:
```

Gateway construction (was `self.gateway = Gateway(creds)`):

```python
        # SSH-free MQTT mode passes creds=None: no gateway is built and control
        # read-back rides the broker instead.
        self.gateway = Gateway(creds) if creds is not None else None
```

And in `async_shutdown` (was `await self.gateway.close()`):

```python
        if self.gateway is not None:
            await self.gateway.close()
```

Also update the class docstring's second paragraph to: `A `gateway` is kept only for SSH entries; in SSH-free MQTT mode it is None and control read-back uses the broker.`

- [ ] **Step 4: Wire the reader by source in `__init__.py`**

Add to the `.control_coordinator` import:

```python
from .control_coordinator import ControlCoordinator, MqttConfigReader, SshConfigReader
```

Replace the credentials + coordinator block (currently: `creds = Credentials(...)` through the `if source == READ_SOURCE_MQTT: ... else: ...` coordinator construction) with — note `creds` moves into the SSH branch and the MQTT branch passes `None`:

```python
    source = entry.data.get(CONF_READ_SOURCE, DEFAULT_READ_SOURCE)
    if source == READ_SOURCE_MQTT:
        serial = str(entry.data[CONF_TOPIC_SERIAL]).strip()
        read_broker = BrokerConfig(
            host=str(entry.data[CONF_BROKER_HOST]).strip(),
            port=int(entry.data.get(CONF_BROKER_PORT, DEFAULT_BROKER_PORT)),
            username=str(entry.data[CONF_BROKER_USER]).strip(),
            password=str(entry.data[CONF_BROKER_PASSWORD]),
            serial=serial,
            client_id=f"openhomepower-ha-read-{serial}",
        )
        stale = entry.options.get(
            CONF_STALE_SECONDS,
            entry.data.get(CONF_STALE_SECONDS, DEFAULT_STALE_SECONDS))
        # SSH-free: no Credentials, no gateway.
        coordinator = MqttReadCoordinator(hass, entry, regmap, None, read_broker, stale)
        await coordinator.async_start()
        # Push model: wait for the first broker publish so entities come up with
        # data. On timeout, stop the reader before raising so HA's retry doesn't
        # leak a second reader thread.
        if not await coordinator.async_await_first_data(timeout=min(stale, 45)):
            await coordinator.async_shutdown()
            raise ConfigEntryNotReady("no telemetry received from the broker yet")
    else:
        creds = Credentials(
            host=entry.data[CONF_HOST],
            port=entry.data.get(CONF_PORT, 34522),
            username=entry.data.get(CONF_USERNAME, "homepower"),
            password=entry.data.get(CONF_PASSWORD, "123456"),
        )
        coordinator = HomepowerCoordinator(
            hass, entry, regmap, creds,
            entry.options.get(CONF_POLL_SECONDS,
                              entry.data.get(CONF_POLL_SECONDS, DEFAULT_POLL_SECONDS)),
        )
        await coordinator.async_config_entry_first_refresh()
```

Then, in the control-setup block inside the `try:`, replace the `control_coordinator = ControlCoordinator(hass, coordinator.gateway, mqtt)` line (and its comment) with a reader chosen by source:

```python
        broker = _broker_config(entry)
        if broker is not None:
            mqtt = MqttControl(broker)
            # Control read-back follows the entry's read source; writes are MQTT.
            if source == READ_SOURCE_MQTT:
                reader = MqttConfigReader(hass, mqtt)
            else:
                reader = SshConfigReader(coordinator.gateway)
            control_coordinator = ControlCoordinator(hass, reader, mqtt)
            # Best-effort: a control-read hiccup must not block setup.
            await control_coordinator.async_refresh()
            store["control"] = control_coordinator
            store["mqtt"] = mqtt
```

Finally, update the module docstring's first body line from `Reads are local and read-only (SSH).` to `Reads are local and read-only — SSH log scrape or MQTT broker subscription.`

- [ ] **Step 5: Static-validate the HA files and run the suite**

Run:
```bash
python3 -c "import ast; [ast.parse(open(f'custom_components/openhomepower/{f}').read()) for f in ('control_coordinator.py','__init__.py','mqtt_coordinator.py')]" && python3 -m pytest tests -q
```
Expected: no output from `ast.parse` (all three parse); tests PASS (26).

- [ ] **Step 6: Commit**

```bash
git add custom_components/openhomepower/control_coordinator.py custom_components/openhomepower/__init__.py custom_components/openhomepower/mqtt_coordinator.py
git commit -m "feat(control): MQTT control read-back; SSH-free MQTT mode"
```

---

### Task 3: Default to MQTT + SSH-free config/options flow

**Files:**
- Modify: `custom_components/openhomepower/const.py`
- Modify: `custom_components/openhomepower/config_flow.py`
- Modify: `custom_components/openhomepower/strings.json`, `custom_components/openhomepower/translations/en.json`
- Test: `tests/test_const.py` (create)

**Interfaces:**
- Produces (const): `DEFAULT_READ_SOURCE = READ_SOURCE_MQTT`.
- Config flow: `CONF_HOST` becomes optional; SSH entries still require it (error `host_required`); MQTT entries are stored without SSH host/creds; `_derive_broker` is SSH-free for MQTT entries.

- [ ] **Step 1: Write the failing test**

Create `tests/test_const.py` (imports through the conftest package, like `test_entities.py`):

```python
"""Guards for integration-wide constants."""
from openhomepower.const import DEFAULT_READ_SOURCE, READ_SOURCE_MQTT


def test_default_read_source_is_mqtt():
    # MQTT is the universal path (works on units whose daemon doesn't log to
    # disk); new installs default to it.
    assert DEFAULT_READ_SOURCE == READ_SOURCE_MQTT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_const.py -q`
Expected: FAIL — `DEFAULT_READ_SOURCE` is still `READ_SOURCE_SSH`.

- [ ] **Step 3: Flip the default in `const.py`**

Change the line `DEFAULT_READ_SOURCE = READ_SOURCE_SSH` to:

```python
DEFAULT_READ_SOURCE = READ_SOURCE_MQTT
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_const.py -q`
Expected: PASS.

- [ ] **Step 5: Make the config flow SSH-optional**

In `config_flow.py`:

**(a)** Change the host field in the user-step schema from `vol.Required` to `vol.Optional`:

```python
            vol.Optional(CONF_HOST, default=suggested_host): str,
```

**(b)** Update the selector labels so MQTT reads as the default:

```python
                    options=[
                        SelectOptionDict(value=READ_SOURCE_SSH, label="SSH log"),
                        SelectOptionDict(value=READ_SOURCE_MQTT, label="MQTT broker (default)"),
                    ],
```

**(c)** In the SSH branch (the `else:` whose body starts `creds = Credentials(...)`), require a host up front. Setting `errors["base"]` here falls through to the single `async_show_form` at the end of the method — no early return needed. Wrap the existing body so it only runs with a host:

```python
            else:
                if not host:
                    errors["base"] = "host_required"
                else:
                    creds = Credentials(
                        host=host,
                        port=user_input.get(CONF_PORT, DEFAULT_PORT),
                        username=user_input.get(CONF_USERNAME, DEFAULT_USERNAME),
                        password=user_input.get(CONF_PASSWORD, DEFAULT_PASSWORD),
                    )
                    serial, error = await self._async_probe(creds)
                    if error:
                        errors["base"] = error
                    else:
                        await self.async_set_unique_id(serial or host)
                        self._abort_if_unique_id_configured()
                        return self.async_create_entry(
                            title="Energizer Homepower",
                            data={
                                CONF_READ_SOURCE: READ_SOURCE_SSH,
                                CONF_HOST: host,
                                CONF_PORT: creds.port,
                                CONF_USERNAME: creds.username,
                                CONF_PASSWORD: creds.password,
                                CONF_POLL_SECONDS: user_input.get(
                                    CONF_POLL_SECONDS, DEFAULT_POLL_SECONDS),
                            },
                        )
```

The existing inline `schema = vol.Schema({...})` near the end of `async_step_user` stays — just apply changes (a) and (b) to it in place (host `vol.Optional`, updated selector labels). No schema helper is needed.

**(d)** Make the MQTT entry SSH-free — drop `CONF_HOST` from the created entry's `data`. In the MQTT branch's `async_create_entry(..., data={...})`, remove the `CONF_HOST: host,` line so the stored keys are: `CONF_READ_SOURCE`, `CONF_BROKER_HOST`, `CONF_BROKER_PORT`, `CONF_BROKER_USER`, `CONF_BROKER_PASSWORD`, `CONF_TOPIC_SERIAL`.

- [ ] **Step 6: Make option derivation SSH-free for MQTT entries**

In `config_flow.py`, add `CONF_READ_SOURCE` handling at the top of `_derive_broker` so MQTT entries never touch SSH — they default the control broker to the read broker the entry already stores:

```python
    async def _derive_broker(self) -> dict:
        """Defaults for the control-broker form.

        MQTT entries are SSH-free — reuse the read-broker settings the entry
        already stores. SSH entries derive from the gateway over SSH.
        """
        data = self.config_entry.data
        if data.get(CONF_READ_SOURCE) == READ_SOURCE_MQTT:
            return {
                "host": data.get(CONF_BROKER_HOST, ""),
                "port": data.get(CONF_BROKER_PORT, DEFAULT_BROKER_PORT),
                "user": data.get(CONF_BROKER_USER, ""),
                "pwd": data.get(CONF_BROKER_PASSWORD, ""),
                "serial": data.get(CONF_TOPIC_SERIAL, ""),
            }

        import re

        import asyncssh
        # ...existing SSH-derivation body unchanged, starting at `out: dict = {}`...
```

Keep the entire existing SSH body (from `out: dict = {}` through `return out`) below the new MQTT short-circuit. `CONF_READ_SOURCE`, `READ_SOURCE_MQTT`, `CONF_BROKER_HOST/PORT/USER/PASSWORD`, `CONF_TOPIC_SERIAL`, `DEFAULT_BROKER_PORT` are already imported in this module.

- [ ] **Step 7: Add the `host_required` error string**

In both `custom_components/openhomepower/strings.json` and `custom_components/openhomepower/translations/en.json`, add to the `config.error` object (NZ English, no literal `{...}`):

```json
"host_required": "Enter the battery's address for the SSH source."
```

- [ ] **Step 8: Run the suite + JSON validation + static check**

Run:
```bash
python3 -m pytest tests -q && python3 -c "import json; json.load(open('custom_components/openhomepower/strings.json')); json.load(open('custom_components/openhomepower/translations/en.json'))" && python3 -c "import ast; ast.parse(open('custom_components/openhomepower/config_flow.py').read())"
```
Expected: tests PASS (27 now); JSON loads; config_flow parses.

- [ ] **Step 9: Commit**

```bash
git add custom_components/openhomepower/const.py custom_components/openhomepower/config_flow.py custom_components/openhomepower/strings.json custom_components/openhomepower/translations/en.json tests/test_const.py
git commit -m "feat(config): default to MQTT; SSH-free MQTT config/options flow"
```

---

### Task 4: Version bump + docs + validation

**Files:**
- Modify: `custom_components/openhomepower/manifest.json`
- Modify: `README.md`

- [ ] **Step 1: Bump the version**

In `manifest.json`, change `"version": "0.3.0"` to:

```json
  "version": "0.4.0"
```

- [ ] **Step 2: Update the README's telemetry-source section**

Find the existing `### Telemetry source: SSH or MQTT` subsection and replace its body so it states MQTT is the default and fully SSH-free (control included), NZ English, no real secrets:

```markdown
### Telemetry source: SSH or MQTT

New installs default to **MQTT** — it works on every unit (including gateway
builds whose daemon doesn't log to disk) and needs no SSH at all: telemetry,
control writes, and control read-back all run over your broker. Pick **MQTT** and
enter the broker host, credentials, and topic serial; you don't need the
battery's SSH address.

**SSH** remains available as an independent option for units you'd rather read
over the local log with no broker — select it and enter the battery's address.
Everything downstream — sensors, the Energy Dashboard, and the control entities —
is identical either way.
```

- [ ] **Step 3: Full suite + JSON validation**

Run: `python3 -m pytest tests -q && python3 -c "import json,glob; [json.load(open(f)) for f in glob.glob('custom_components/openhomepower/**/*.json', recursive=True)]"`
Expected: PASS; all JSON valid.

- [ ] **Step 4: Secret scan**

Run: `git grep -nE "2103200212|0512017220|in\.tm\.enertek|testen|0401910433" -- . ':!docs' || echo "clean"`
Expected: `clean`. If anything matches outside `docs/`, STOP — do not commit.

- [ ] **Step 5: Commit**

```bash
git add custom_components/openhomepower/manifest.json README.md
git commit -m "docs(control): document SSH-free MQTT mode; bump to 0.4.0"
```

- [ ] **Step 6: Live validation (manual, against the reference unit)**

1. On an MQTT-source entry with control enabled, confirm the control entities (mode / max-SoC / reserve-on / reserve-off / excess) populate and match the portal.
2. Change a setting (vendor app or HA) and confirm the read-back reflects it within a `CONTROL_SCAN_INTERVAL` poll.
3. Drive the **off-grid-reserve** slider and confirm **excess is not clobbered** (the read-modify-write now uses the MQTT read-back's excess).
4. Confirm the MQTT entry makes **no SSH connection** (nothing dials the gateway on port 34522).

---

## Self-Review

**Spec coverage** (spec §→task):
- §4 architecture (source-agnostic reader, MQTT reader via `read_config`, no gateway in MQTT) → Tasks 1–2.
- §5 registers (67/105/120–123/231) → Task 1 (`read_config`) + test.
- §6 components: `control.py` → Task 1; `control_coordinator.py` → Task 2; `__init__.py` → Task 2; `mqtt_coordinator.py` → Task 2; `config_flow.py`/`const.py` → Task 3; `manifest.json`/`README.md` → Task 4.
- §7 data flow (poll → `read_config` → `control_state_from_regs`; reserve RMW safe) → Tasks 1–2 + live check (Task 4 step 6.3).
- §8 error handling (`UpdateFailed`, last-known retained, `OSError`/`TransportError`) → Task 2 `_async_update_data`.
- §10 migration (existing MQTT entries keep working; host ignored) → Task 2 (creds only in SSH branch; MQTT passes None) — no version/data migration.
- §11 security (read-only fn-03, per-device creds, no secrets) → Global Constraints + Task 4 secret scan.
- §12 testing (unit for `read_config` + default; regression SSH; live checklist) → Task 1, Task 3, Task 4.

**Placeholder scan:** none — every step has concrete code or an exact command.

**Type consistency:** `read_config() -> dict[int,int]` (Task 1) is consumed by `MqttConfigReader.read_regs` (Task 2); `read_regs() -> dict[int,int]` on both readers feeds `control_state_from_regs` (Task 2); `ControlCoordinator(hass, reader, mqtt)` matches the `__init__.py` call site (Task 2); `MqttReadCoordinator(..., creds: Credentials | None, ...)` matches the `None` passed in the MQTT branch (Task 2); `DEFAULT_READ_SOURCE` (Task 3) is read by `__init__.py`/`config_flow.py` (existing).
