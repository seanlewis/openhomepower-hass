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
                            CONF_HOST: host,
                            CONF_BROKER_HOST: user_input[CONF_BROKER_HOST].strip(),
                            CONF_BROKER_PORT: user_input.get(
                                CONF_BROKER_PORT, DEFAULT_BROKER_PORT),
                            CONF_BROKER_USER: user_input[CONF_BROKER_USER].strip(),
                            CONF_BROKER_PASSWORD: user_input[CONF_BROKER_PASSWORD],
                            CONF_TOPIC_SERIAL: user_input[CONF_TOPIC_SERIAL].strip(),
                        },
                    )
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
        else:
            # Best-effort autodiscovery so most people never type an address.
            try:
                found = await discovery.discover()
                self._discovered = [c.host for c in found]
            except Exception:  # discovery must never block setup
                _LOGGER.debug("discovery failed", exc_info=True)
            suggested_host = self._discovered[0] if self._discovered else ""

        schema = vol.Schema({
            vol.Required(CONF_HOST, default=suggested_host): str,
            vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
            vol.Optional(CONF_USERNAME, default=DEFAULT_USERNAME): str,
            vol.Optional(CONF_PASSWORD, default=DEFAULT_PASSWORD): str,
            vol.Optional(CONF_POLL_SECONDS, default=DEFAULT_POLL_SECONDS):
                vol.All(int, vol.Range(min=MIN_POLL_SECONDS, max=3600)),
            vol.Required(CONF_READ_SOURCE, default=DEFAULT_READ_SOURCE): SelectSelector(
                SelectSelectorConfig(
                    mode=SelectSelectorMode.DROPDOWN,
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

        Starts a short-lived MqttReader, waits for one telemetry frame, then
        always stops it again — this must never leak a background thread,
        whichever way the probe ends.
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

        regmap = await self.hass.async_add_executor_job(RegisterMap.load)
        readings = regmap.decode(merge(frames_seen)) if frames_seen else {}
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
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        opts = self.config_entry.options
        data = self.config_entry.data
        poll = opts.get(CONF_POLL_SECONDS,
                        data.get(CONF_POLL_SECONDS, DEFAULT_POLL_SECONDS))
        # Derive broker host/creds/serial from the device (best-effort) for the
        # form defaults — nothing sensitive is stored in this source.
        d = await self._derive_broker()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_POLL_SECONDS, default=poll):
                    vol.All(int, vol.Range(min=MIN_POLL_SECONDS, max=3600)),
                vol.Optional(CONF_CONTROL_ENABLED,
                             default=opts.get(CONF_CONTROL_ENABLED, False)): bool,
                vol.Optional(CONF_BROKER_HOST,
                             default=opts.get(CONF_BROKER_HOST, d.get("host", ""))): str,
                vol.Optional(CONF_BROKER_PORT,
                             default=opts.get(CONF_BROKER_PORT, d.get("port", DEFAULT_BROKER_PORT))): int,
                vol.Optional(CONF_BROKER_USER,
                             default=opts.get(CONF_BROKER_USER, d.get("user", ""))): str,
                vol.Optional(CONF_BROKER_PASSWORD,
                             default=opts.get(CONF_BROKER_PASSWORD, d.get("pwd", ""))): str,
                vol.Optional(CONF_TOPIC_SERIAL,
                             default=opts.get(CONF_TOPIC_SERIAL, d.get("serial", ""))): str,
            }),
        )

    async def _derive_broker(self) -> dict:
        """Read broker host/port/creds + topic serial off the gateway via SSH.

        Best-effort and never fatal. Keeps vendor secrets out of this source —
        they come from the device the user already owns.
        """
        import re

        import asyncssh

        data = self.config_entry.data
        out: dict = {}
        try:
            async with asyncssh.connect(
                data[CONF_HOST], port=data.get(CONF_PORT, DEFAULT_PORT),
                username=data.get(CONF_USERNAME, DEFAULT_USERNAME),
                password=data.get(CONF_PASSWORD, DEFAULT_PASSWORD),
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
            _LOGGER.debug("could not derive broker settings", exc_info=True)
        return out
