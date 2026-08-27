"""Config flow: find the battery, verify it, create the entry."""
from __future__ import annotations

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
    DOMAIN,
    MIN_POLL_SECONDS,
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
                        # Serial keeps a second setup of the same battery from
                        # duplicating every entity.
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
                        SelectOptionDict(value=READ_SOURCE_SSH, label="SSH log"),
                        SelectOptionDict(value=READ_SOURCE_MQTT, label="MQTT broker (default)"),
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

    async def async_step_init(
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
                return self.async_create_entry(data=user_input)

        # Defaults come from the resubmission (on error) or the stored config;
        # broker fields fall back to a best-effort device derivation on first show.
        src = user_input if user_input is not None else {}
        d = {} if user_input is not None else await self._derive_broker()

        return self.async_show_form(
            step_id="init",
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
