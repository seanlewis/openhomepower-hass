"""Config flow: find the battery, verify it, create the entry."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    SelectOptionDict,
)

from . import discovery
from .const import (
    CONF_BROKER_HOST,
    CONF_BROKER_PASSWORD,
    CONF_BROKER_PORT,
    CONF_BROKER_USER,
    CONF_CONTROL_ENABLED,
    CONF_POLL_SECONDS,
    CONF_READ_SOURCE,
    CONF_TOPIC_SERIAL,
    DEFAULT_BROKER_PORT,
    DEFAULT_POLL_SECONDS,
    DEFAULT_READ_SOURCE,
    CONF_HA_IP,
    CONF_LOCAL_BROKER,
    CONF_VENDOR_BROKER,
    DOMAIN,
    MIN_POLL_SECONDS,
    READ_SOURCE_AUTO,
    READ_SOURCE_MQTT,
    READ_SOURCE_SSH,
)
from .protocol import merge
from .registry import RegisterMap
from .transport import Credentials, Gateway, TransportError

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 34522
DEFAULT_USERNAME = "homepower"
DEFAULT_PASSWORD = "123456"   # vendor default, published in Enertek's setup PDF


async def _ssh_derive_broker(host: str, port: int,
                             username: str, password: str) -> dict:
    """Read the device's MQTT broker config (host/port/creds/serial) over SSH.

    Best-effort and never fatal — returns {} if the device can't be reached.
    Used only to PRE-FILL the broker form fields (most units still point at the
    vendor broker, whose fleet credentials aren't published, so reading them off
    the device is the only easy way to get them). Runtime never depends on SSH.
    """
    import re

    import asyncssh

    out: dict = {}
    try:
        async with asyncssh.connect(
            host, port=port, username=username, password=password,
            known_hosts=None,
        ) as conn:
            result = await conn.run(
                "uci show we2; echo ---; "
                "grep -oE 'Enertek/[0-9]+/' /tmp/wemonitor.log 2>/dev/null | head -1",
                timeout=10,
            )
            text = result.stdout or ""
            for key, field in (("host", "host"), ("port", "port"),
                               ("user", "user"), ("pwd", "pwd")):
                m = re.search(rf"we2\.mqtt\.{field}='([^']*)'", text)
                if m:
                    out[key] = m.group(1)
            m = re.search(r"Enertek/([0-9]+)/", text)
            if m:
                out["serial"] = m.group(1)
            if "port" in out and out["port"].isdigit():
                out["port"] = int(out["port"])
    except Exception:  # noqa: BLE001 - derivation is best-effort, never fatal
        _LOGGER.debug("could not derive broker settings over SSH", exc_info=True)
    return out


def _fill_broker_fields(user_input: dict[str, Any], derived: dict) -> dict[str, Any]:
    """Copy of the form input with blank broker fields taken from `derived`.

    Anything the user typed wins; `derived` is `_ssh_derive_broker()` output.
    """
    out = dict(user_input)
    # The port belongs with the host: if the host came from the device, so
    # does the port (the form's port field just holds the default then).
    if not str(out.get(CONF_BROKER_HOST, "")).strip() and derived.get("host"):
        out[CONF_BROKER_PORT] = derived.get("port", DEFAULT_BROKER_PORT)
    for conf, key in ((CONF_BROKER_HOST, "host"), (CONF_BROKER_USER, "user"),
                      (CONF_BROKER_PASSWORD, "pwd"), (CONF_TOPIC_SERIAL, "serial")):
        if not str(out.get(conf, "")).strip() and derived.get(key):
            out[conf] = derived[key]
    return out


class OpenHomepowerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OpenHomepower."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: list[str] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the address, pre-filled with anything we can find."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            source = user_input.get(CONF_READ_SOURCE, DEFAULT_READ_SOURCE)
            if source == READ_SOURCE_MQTT:
                serial, error = await self._async_probe_mqtt(user_input)
                if error:
                    errors["base"] = error
                else:
                    return await self._async_create_mqtt_entry(serial, user_input)
            elif not host:
                errors["base"] = "host_required"
            else:
                creds = Credentials(
                    host=host,
                    port=user_input.get(CONF_PORT, DEFAULT_PORT),
                    username=user_input.get(CONF_USERNAME, DEFAULT_USERNAME),
                    password=user_input.get(CONF_PASSWORD, DEFAULT_PASSWORD),
                )
                serial, error = await self._async_probe(creds)
                if not error:
                    return await self._async_create_ssh_entry(
                        serial, creds, user_input)
                if source == READ_SOURCE_AUTO and error == "no_data":
                    # SSH works but this unit's daemon doesn't log frames to
                    # disk, so fall back to MQTT — which every unit speaks —
                    # filling any blank broker field from the device we just
                    # reached. Other SSH errors mean the device itself wasn't
                    # reachable, so MQTT can't be derived either.
                    _LOGGER.info(
                        "no telemetry in the gateway log at %s; trying MQTT", host)
                    derived = await _ssh_derive_broker(
                        host, creds.port, creds.username, creds.password)
                    mqtt_input = _fill_broker_fields(user_input, derived)
                    serial, error = await self._async_probe_mqtt(mqtt_input)
                    if not error:
                        return await self._async_create_mqtt_entry(
                            serial, mqtt_input)
                errors["base"] = error
            suggested_host = host
            # Preserve what was typed across an error re-render.
            broker = {
                "host": user_input.get(CONF_BROKER_HOST, ""),
                "port": user_input.get(CONF_BROKER_PORT, DEFAULT_BROKER_PORT),
                "user": user_input.get(CONF_BROKER_USER, ""),
                "pwd": user_input.get(CONF_BROKER_PASSWORD, ""),
                "serial": user_input.get(CONF_TOPIC_SERIAL, ""),
            }
        else:
            # Best-effort autodiscovery so most people never type an address.
            try:
                found = await discovery.discover()
                self._discovered = [c.host for c in found]
            except Exception:  # discovery must never block setup
                _LOGGER.debug("discovery failed", exc_info=True)
            suggested_host = self._discovered[0] if self._discovered else ""
            # Pre-fill the MQTT broker fields from the device (vendor defaults
            # over SSH), so a new user doesn't have to hunt down the broker host,
            # credentials and serial. Best-effort; left blank if the device isn't
            # reachable. Runtime stays SSH-free.
            broker: dict = {}
            if suggested_host:
                broker = await _ssh_derive_broker(
                    suggested_host, DEFAULT_PORT, DEFAULT_USERNAME, DEFAULT_PASSWORD)

        schema = vol.Schema({
            vol.Optional(CONF_HOST, default=suggested_host): str,
            vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
            vol.Optional(CONF_USERNAME, default=DEFAULT_USERNAME): str,
            vol.Optional(CONF_PASSWORD, default=DEFAULT_PASSWORD): str,
            vol.Optional(CONF_POLL_SECONDS, default=DEFAULT_POLL_SECONDS):
                vol.All(int, vol.Range(min=MIN_POLL_SECONDS, max=3600)),
            vol.Required(CONF_READ_SOURCE, default=DEFAULT_READ_SOURCE): SelectSelector(
                SelectSelectorConfig(
                    mode=SelectSelectorMode.DROPDOWN,
                    options=[
                        SelectOptionDict(value=READ_SOURCE_AUTO,
                                         label="Automatic (recommended)"),
                        SelectOptionDict(value=READ_SOURCE_SSH, label="SSH log"),
                        SelectOptionDict(value=READ_SOURCE_MQTT, label="MQTT broker"),
                    ],
                )
            ),
            vol.Optional(CONF_BROKER_HOST, default=broker.get("host", "")): str,
            vol.Optional(CONF_BROKER_PORT,
                         default=broker.get("port", DEFAULT_BROKER_PORT)): int,
            vol.Optional(CONF_BROKER_USER, default=broker.get("user", "")): str,
            vol.Optional(CONF_BROKER_PASSWORD, default=broker.get("pwd", "")): str,
            vol.Optional(CONF_TOPIC_SERIAL, default=broker.get("serial", "")): str,
        })
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "found": ", ".join(self._discovered) if self._discovered
                else "none found automatically",
            },
        )

    async def _async_create_ssh_entry(
        self, serial: str | None, creds: Credentials, user_input: dict[str, Any]
    ) -> ConfigFlowResult:
        # Serial keeps a second setup of the same battery from duplicating
        # every entity.
        await self.async_set_unique_id(serial or creds.host)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Energizer Homepower",
            data={
                CONF_READ_SOURCE: READ_SOURCE_SSH,
                CONF_HOST: creds.host,
                CONF_PORT: creds.port,
                CONF_USERNAME: creds.username,
                CONF_PASSWORD: creds.password,
                CONF_POLL_SECONDS: user_input.get(
                    CONF_POLL_SECONDS, DEFAULT_POLL_SECONDS),
            },
        )

    async def _async_create_mqtt_entry(
        self, serial: str | None, user_input: dict[str, Any]
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(
            serial or user_input[CONF_TOPIC_SERIAL].strip())
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Energizer Homepower",
            data={
                CONF_READ_SOURCE: READ_SOURCE_MQTT,
                CONF_BROKER_HOST: user_input[CONF_BROKER_HOST].strip(),
                CONF_BROKER_PORT: user_input.get(
                    CONF_BROKER_PORT, DEFAULT_BROKER_PORT),
                CONF_BROKER_USER: user_input[CONF_BROKER_USER].strip(),
                CONF_BROKER_PASSWORD: user_input[CONF_BROKER_PASSWORD],
                CONF_TOPIC_SERIAL: user_input[CONF_TOPIC_SERIAL].strip(),
            },
        )

    async def _async_probe(self, creds: Credentials) -> tuple[str | None, str | None]:
        """Connect and decode once. Returns (serial, error_key)."""
        gateway = Gateway(creds)
        try:
            frames = await gateway.read_latest(attempts=2)
        except TransportError as err:
            # The UI can only show a generic "cannot connect", but the underlying
            # message distinguishes a wrong password from an SSH/algorithm failure
            # from an unreadable log — log it so a failed setup is diagnosable.
            _LOGGER.warning(
                "could not read from the gateway at %s: %s", creds.host, err)
            message = str(err).lower()
            if "password" in message or "username" in message:
                return None, "invalid_auth"
            return None, "cannot_connect"
        except Exception:  # noqa: BLE001 - surface as a generic failure
            _LOGGER.exception("unexpected error probing the gateway")
            return None, "unknown"
        finally:
            await gateway.close()

        regmap = await self.hass.async_add_executor_job(RegisterMap.load)
        readings = regmap.decode(merge(frames))
        if not readings:
            return None, "no_data"
        serial = readings.get("device_serial")
        return (str(serial.value) if serial else frames[0].devsn), None

    async def _async_probe_mqtt(self, data: dict[str, Any]) -> tuple[str | None, str | None]:
        """Confirm the broker yields decodable telemetry. Returns (serial, error_key).

        Starts a short-lived MqttReader (which requests a reading), waits for one
        decodable reply, then always stops it again — this must never leak a
        background thread, whichever way the probe ends.
        """
        import asyncio

        from .control import BrokerConfig
        from .mqtt_reader import MqttReader

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
        regs_seen: dict[int, int] = {}

        def _on_update(regs):
            regs_seen.clear()
            regs_seen.update(regs)
            self.hass.loop.call_soon_threadsafe(got.set)

        reader = MqttReader(cfg, _on_update)
        reader.start()
        try:
            await asyncio.wait_for(got.wait(), timeout=25)
        except asyncio.TimeoutError:
            return None, "cannot_connect"
        finally:
            await self.hass.async_add_executor_job(reader.stop)

        regmap = await self.hass.async_add_executor_job(RegisterMap.load)
        readings = regmap.decode(regs_seen) if regs_seen else {}
        if not readings:
            return None, "no_data"
        dev = readings.get("device_serial")
        return (str(dev.value) if dev else serial), None

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return OpenHomepowerOptionsFlow()


class OpenHomepowerOptionsFlow(OptionsFlow):
    """Poll interval, plus opt-in control settings.

    Control is off by default. Turning it on lets HA write settings via a
    configurable broker — the vendor broker to start, a local broker after
    cutover (change the host here; nothing else moves).
    """

    def __init__(self) -> None:
        self._move_task: asyncio.Task | None = None
        self._move_host = ""
        self._move_plan: dict = {}
        self._move_error: str | None = None
        self._move_verified = False

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """A menu where add-ons exist (the move steps need one); otherwise
        straight to the settings form, as before."""
        try:
            from homeassistant.helpers.hassio import is_hassio

            supervised = is_hassio(self.hass)
        except ImportError:
            supervised = False
        if not supervised:
            return await self.async_step_settings(user_input)
        local = self.config_entry.options.get(CONF_LOCAL_BROKER, False)
        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "move_vendor" if local else "move_local"],
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        data = self.config_entry.data
        opts = self.config_entry.options
        current_source = data.get(CONF_READ_SOURCE, READ_SOURCE_SSH)

        if user_input is not None:
            # The read source lives in entry.data. Switching it here rewrites the
            # stored connection config; MQTT reuses the broker fields on this
            # form. The entry's unique_id (serial) never changes, so entities and
            # their history survive the reload the update-listener triggers.
            source = user_input.get(CONF_READ_SOURCE, current_source)
            new_data = dict(data)
            new_data[CONF_READ_SOURCE] = source
            if source == READ_SOURCE_MQTT:
                b_host = str(user_input.get(CONF_BROKER_HOST, "")).strip()
                b_user = str(user_input.get(CONF_BROKER_USER, "")).strip()
                b_pwd = str(user_input.get(CONF_BROKER_PASSWORD, ""))
                b_serial = str(user_input.get(CONF_TOPIC_SERIAL, "")).strip()
                if not (b_host and b_user and b_pwd and b_serial):
                    errors["base"] = "mqtt_fields_missing"
                else:
                    new_data[CONF_BROKER_HOST] = b_host
                    new_data[CONF_BROKER_PORT] = int(
                        user_input.get(CONF_BROKER_PORT, DEFAULT_BROKER_PORT))
                    new_data[CONF_BROKER_USER] = b_user
                    new_data[CONF_BROKER_PASSWORD] = b_pwd
                    new_data[CONF_TOPIC_SERIAL] = b_serial
            else:
                s_host = str(user_input.get(CONF_HOST, "")).strip()
                if not s_host:
                    errors["base"] = "host_required"
                else:
                    new_data[CONF_HOST] = s_host
            if not errors:
                if new_data != dict(data):
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, data=new_data)
                # Options are replaced wholesale on save; carry the move
                # bookkeeping through, or "Move back" would lose its settings.
                kept = {k: opts[k] for k in (CONF_LOCAL_BROKER, CONF_VENDOR_BROKER)
                        if k in opts}
                return self.async_create_entry(data={**kept, **user_input})

        # Defaults come from the resubmission (on error) or the stored config;
        # broker fields fall back to a best-effort device derivation on first show.
        src = user_input if user_input is not None else {}
        d = {} if user_input is not None else await self._derive_broker()

        return self.async_show_form(
            step_id="settings",
            errors=errors,
            data_schema=vol.Schema({
                vol.Required(CONF_READ_SOURCE,
                             default=src.get(CONF_READ_SOURCE, current_source)):
                    SelectSelector(SelectSelectorConfig(
                        mode=SelectSelectorMode.DROPDOWN,
                        options=[
                            SelectOptionDict(value=READ_SOURCE_SSH, label="SSH log"),
                            SelectOptionDict(value=READ_SOURCE_MQTT, label="MQTT broker"),
                        ])),
                vol.Optional(CONF_HOST,
                             default=src.get(CONF_HOST, data.get(CONF_HOST, ""))): str,
                vol.Optional(CONF_POLL_SECONDS,
                             default=src.get(CONF_POLL_SECONDS,
                                             opts.get(CONF_POLL_SECONDS,
                                                      data.get(CONF_POLL_SECONDS, DEFAULT_POLL_SECONDS)))):
                    vol.All(int, vol.Range(min=MIN_POLL_SECONDS, max=3600)),
                vol.Optional(CONF_CONTROL_ENABLED,
                             default=src.get(CONF_CONTROL_ENABLED,
                                             opts.get(CONF_CONTROL_ENABLED, False))): bool,
                vol.Optional(CONF_BROKER_HOST,
                             default=src.get(CONF_BROKER_HOST,
                                             opts.get(CONF_BROKER_HOST, d.get("host", "")))): str,
                vol.Optional(CONF_BROKER_PORT,
                             default=src.get(CONF_BROKER_PORT,
                                             opts.get(CONF_BROKER_PORT, d.get("port", DEFAULT_BROKER_PORT)))): int,
                vol.Optional(CONF_BROKER_USER,
                             default=src.get(CONF_BROKER_USER,
                                             opts.get(CONF_BROKER_USER, d.get("user", "")))): str,
                vol.Optional(CONF_BROKER_PASSWORD,
                             default=src.get(CONF_BROKER_PASSWORD,
                                             opts.get(CONF_BROKER_PASSWORD, d.get("pwd", "")))): str,
                vol.Optional(CONF_TOPIC_SERIAL,
                             default=src.get(CONF_TOPIC_SERIAL,
                                             opts.get(CONF_TOPIC_SERIAL, d.get("serial", "")))): str,
            }),
        )

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

        return await _ssh_derive_broker(
            data[CONF_HOST], data.get(CONF_PORT, DEFAULT_PORT),
            data.get(CONF_USERNAME, DEFAULT_USERNAME),
            data.get(CONF_PASSWORD, DEFAULT_PASSWORD))

    # --- moving between Enertek's broker and the local broker add-on ----------
    #
    # Each move is: a form (confirm + addresses) -> quick checks that change
    # nothing -> a progress screen running the move -> a result message. The
    # gateway work lives in local_broker.py; this only orchestrates and records
    # the outcome on the entry.

    def _gateway_login(self, host: str):
        from .local_broker import GatewayLogin

        data = self.config_entry.data
        return GatewayLogin(
            host=host,
            port=int(data.get(CONF_PORT, DEFAULT_PORT)),
            username=data.get(CONF_USERNAME, DEFAULT_USERNAME),
            password=data.get(CONF_PASSWORD, DEFAULT_PASSWORD),
        )

    def _move_form(self, step_id: str, errors: dict, error: str,
                   ha_ip: str | None) -> ConfigFlowResult:
        fields: dict = {
            vol.Required(CONF_HOST, default=self._move_host): str,
        }
        if ha_ip is not None:
            fields[vol.Required(CONF_HA_IP, default=ha_ip)] = str
        return self.async_show_form(
            step_id=step_id, data_schema=vol.Schema(fields), errors=errors,
            description_placeholders={"error": error},
        )

    async def _async_run_with_progress(self, step_id: str, work) -> ConfigFlowResult | None:
        """Drive `work` behind a progress screen. Returns None once it's done.

        The work runs as a background task shielded from the flow: if the dialog
        is closed mid-move, the move (including any rollback) still finishes
        rather than stopping with the gateway half-changed.
        """
        if self._move_task is None:
            inner = self.hass.async_create_background_task(work(), "openhomepower move")

            async def _wait():
                return await asyncio.shield(inner)

            self._move_task = self.hass.async_create_task(_wait())
        if not self._move_task.done():
            return self.async_show_progress(
                step_id=step_id, progress_action="moving",
                progress_task=self._move_task,
            )
        return None

    def _move_result(self) -> tuple[Any, str | None]:
        from .local_broker import MoveError

        task, self._move_task = self._move_task, None
        try:
            return task.result(), None
        except MoveError as err:
            _LOGGER.warning("moving the battery failed: %s", err)
            return None, str(err)
        except Exception as err:  # noqa: BLE001 - surface anything else too
            _LOGGER.exception("moving the battery failed")
            return None, f"Unexpected error: {err}"

    def _save_broker(self, broker: dict, local: bool,
                     vendor: dict | None) -> None:
        """Point the entry's control (and, for MQTT entries, read) broker at
        `broker`. The update listener reloads the entry."""
        opts = dict(self.config_entry.options)
        opts.update({
            CONF_BROKER_HOST: broker["host"],
            CONF_BROKER_PORT: int(broker["port"]),
            CONF_BROKER_USER: broker["user"],
            CONF_BROKER_PASSWORD: broker["pwd"],
            CONF_TOPIC_SERIAL: broker["serial"],
            CONF_LOCAL_BROKER: local,
        })
        if vendor is not None:
            opts[CONF_VENDOR_BROKER] = vendor
        data = dict(self.config_entry.data)
        if data.get(CONF_READ_SOURCE) == READ_SOURCE_MQTT:
            data.update({
                CONF_BROKER_HOST: broker["host"],
                CONF_BROKER_PORT: int(broker["port"]),
                CONF_BROKER_USER: broker["user"],
                CONF_BROKER_PASSWORD: broker["pwd"],
                CONF_TOPIC_SERIAL: broker["serial"],
            })
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=data, options=opts)
        _LOGGER.info("integration now uses the broker at %s:%s (%s)", broker["host"],
                     broker["port"], "local" if local else "Enertek")

    # -- to the local broker --

    async def async_step_move_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        from .local_broker import (
            LOCAL_BROKER_PORT, BrokerAddon, GatewayAdmin, MoveError,
            home_assistant_ip, merge_addon_options, redirect_rule)

        self._move_task = None
        if user_input is None:
            self._move_host = self.config_entry.data.get(CONF_HOST, "")
            return self._move_form("move_local", {}, "",
                                   await home_assistant_ip(self.hass))

        self._move_host = user_input[CONF_HOST].strip()
        ha_ip = user_input[CONF_HA_IP].strip()
        # Checks that change nothing: fail here and the battery is untouched.
        try:
            redirect_rule(ha_ip, LOCAL_BROKER_PORT)
            addon = await BrokerAddon.find(self.hass)
            login = self._gateway_login(self._move_host)
            gateway = GatewayAdmin(login)
            await gateway.check_include()
            device = await _ssh_derive_broker(
                login.host, login.port, login.username, login.password)
            if not (device.get("user") and device.get("pwd")):
                raise MoveError("Couldn't read the battery's broker login from it.")
            opts, data = self.config_entry.options, self.config_entry.data
            serial = str(opts.get(CONF_TOPIC_SERIAL) or data.get(CONF_TOPIC_SERIAL)
                         or device.get("serial") or "").strip()
            if not serial:
                raise MoveError("Couldn't find the battery's MQTT topic serial.")
            addon_opts, client_pw = merge_addon_options(
                await addon.options(), serial, device["user"], device["pwd"])
        except MoveError as err:
            _LOGGER.warning("move to local broker: pre-check failed, nothing changed: %s", err)
            return self._move_form("move_local", {"base": "move_check_failed"},
                                   str(err), ha_ip)

        # What "Move back" restores: the broker the battery used until now —
        # unless the entry already points at a local broker (a manual move), in
        # which case the device's own settings are the vendor ones.
        already_local = (opts.get(CONF_LOCAL_BROKER)
                         or opts.get(CONF_BROKER_HOST) == ha_ip
                         or int(opts.get(CONF_BROKER_PORT, 0) or 0) == LOCAL_BROKER_PORT)
        if opts.get(CONF_BROKER_HOST) and not already_local:
            vendor = {"host": opts[CONF_BROKER_HOST],
                      "port": int(opts.get(CONF_BROKER_PORT, DEFAULT_BROKER_PORT)),
                      "user": opts.get(CONF_BROKER_USER, ""),
                      "pwd": opts.get(CONF_BROKER_PASSWORD, "")}
        else:
            vendor = {"host": device.get("host", ""),
                      "port": int(device.get("port", DEFAULT_BROKER_PORT)),
                      "user": device["user"], "pwd": device["pwd"]}
        self._move_plan = {
            "addon": addon, "addon_opts": addon_opts, "gateway": gateway,
            "local": {"host": ha_ip, "port": LOCAL_BROKER_PORT, "user": serial,
                      "pwd": client_pw, "serial": serial},
            "vendor": {**vendor, "serial": serial},
        }
        return await self.async_step_move_local_run()

    async def async_step_move_local_run(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        shown = await self._async_run_with_progress("move_local_run", self._do_move_local)
        if shown is not None:
            return shown
        _, self._move_error = self._move_result()
        return self.async_show_progress_done(next_step_id="move_local_done")

    async def _do_move_local(self) -> None:
        from .control import BrokerConfig
        from .local_broker import (
            BROKER_READY_TIMEOUT, MoveError, battery_answers, broker_accepts)

        plan = self._move_plan
        local, gateway = plan["local"], plan["gateway"]
        cfg = BrokerConfig(host=local["host"], port=local["port"],
                           username=local["user"], password=local["pwd"],
                           serial=local["serial"])

        _LOGGER.info("move to local broker: starting (battery %s, serial %s, broker %s:%s)",
                     gateway.login.host, local["serial"], local["host"], local["port"])
        await plan["addon"].apply(plan["addon_opts"])
        if not await broker_accepts(self.hass, cfg, BROKER_READY_TIMEOUT):
            raise MoveError("The broker add-on didn't accept Home Assistant's login after "
                            "restarting. Check the add-on's Log tab. The battery hasn't "
                            "been changed.")
        try:
            await gateway.apply_redirect(local["host"], local["port"])
        except MoveError:
            # Nothing is applied until the reboot, so tidying the file is enough.
            try:
                await gateway.remove_redirect()
            except MoveError:
                pass
            raise
        await gateway.reboot()
        _LOGGER.info("move to local broker: waiting for the battery to connect")
        if await battery_answers(self.hass, cfg):
            _LOGGER.info("move to local broker: done")
            return

        _LOGGER.warning("move to local broker: battery didn't connect; rolling back")
        try:
            await gateway.wait_until_back()
            await gateway.remove_redirect()
            await gateway.reboot()
        except MoveError as err:
            _LOGGER.error("move to local broker: rollback failed, the redirect may still "
                          "be on the gateway: %s", err)
            raise MoveError(
                "The battery didn't connect to the local broker, and undoing the change "
                f"also failed ({err}). Use the manual Undo steps in the broker add-on's "
                "documentation.") from err
        raise MoveError(
            "The battery didn't connect to the local broker within 6 minutes, so the "
            "change has been undone and the battery is going back to Enertek's broker. "
            "Check the broker add-on's Log tab, and that the Home Assistant address is "
            "right.")

    async def async_step_move_local_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._move_error:
            return self.async_abort(reason="move_failed",
                                    description_placeholders={"error": self._move_error})
        plan = self._move_plan
        self._save_broker(plan["local"], local=True, vendor=plan["vendor"])
        return self.async_abort(reason="moved_local")

    # -- back to Enertek's broker --

    async def async_step_move_vendor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        from .local_broker import GatewayAdmin, MoveError

        self._move_task = None
        if user_input is None:
            self._move_host = self.config_entry.data.get(CONF_HOST, "")
            return self._move_form("move_vendor", {}, "", None)

        self._move_host = user_input[CONF_HOST].strip()
        opts = self.config_entry.options
        try:
            login = self._gateway_login(self._move_host)
            gateway = GatewayAdmin(login)
            await gateway.run("true")
            vendor = opts.get(CONF_VENDOR_BROKER)
            if not vendor:
                device = await _ssh_derive_broker(
                    login.host, login.port, login.username, login.password)
                vendor = {"host": device.get("host", ""),
                          "port": int(device.get("port", DEFAULT_BROKER_PORT)),
                          "user": device.get("user", ""), "pwd": device.get("pwd", ""),
                          "serial": device.get("serial", "")}
            vendor = dict(vendor)
            vendor.setdefault("serial", "")
            vendor["serial"] = vendor["serial"] or str(opts.get(CONF_TOPIC_SERIAL, ""))
        except MoveError as err:
            _LOGGER.warning("move back to Enertek: pre-check failed, nothing changed: %s", err)
            return self._move_form("move_vendor", {"base": "move_check_failed"},
                                   str(err), None)
        self._move_plan = {"gateway": gateway, "vendor": vendor}
        return await self.async_step_move_vendor_run()

    async def async_step_move_vendor_run(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        shown = await self._async_run_with_progress("move_vendor_run", self._do_move_vendor)
        if shown is not None:
            return shown
        self._move_verified, self._move_error = self._move_result()
        return self.async_show_progress_done(next_step_id="move_vendor_done")

    async def _do_move_vendor(self) -> bool:
        """Remove the redirect and reboot. Returns whether Enertek's broker was
        confirmed — it may simply be down, which is why people move away."""
        from .control import BrokerConfig
        from .local_broker import battery_answers

        plan = self._move_plan
        _LOGGER.info("move back to Enertek: starting (battery %s)", plan["gateway"].login.host)
        await plan["gateway"].remove_redirect()
        await plan["gateway"].reboot()
        v = plan["vendor"]
        if not (v.get("host") and v.get("user") and v.get("pwd") and v.get("serial")):
            _LOGGER.warning("move back to Enertek: no saved Enertek broker settings to "
                            "check against; skipping the check")
            return False
        cfg = BrokerConfig(host=v["host"], port=int(v["port"]), username=v["user"],
                           password=v["pwd"], serial=v["serial"])
        return await battery_answers(self.hass, cfg, timeout=300)

    async def async_step_move_vendor_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._move_error:
            return self.async_abort(reason="move_failed",
                                    description_placeholders={"error": self._move_error})
        vendor = self._move_plan["vendor"]
        if vendor.get("host"):
            self._save_broker(vendor, local=False, vendor=None)
        else:
            opts = dict(self.config_entry.options)
            opts[CONF_LOCAL_BROKER] = False
            self.hass.config_entries.async_update_entry(self.config_entry, options=opts)
        return self.async_abort(
            reason="moved_vendor" if self._move_verified else "moved_vendor_unverified")
