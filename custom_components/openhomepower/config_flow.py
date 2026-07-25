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

from . import discovery
from .const import (
    CONF_POLL_SECONDS,
    DEFAULT_POLL_SECONDS,
    DOMAIN,
    MIN_POLL_SECONDS,
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

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return OpenHomepowerOptionsFlow()


class OpenHomepowerOptionsFlow(OptionsFlow):
    """Allow the poll interval to be changed after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options.get(
            CONF_POLL_SECONDS,
            self.config_entry.data.get(CONF_POLL_SECONDS, DEFAULT_POLL_SECONDS),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_POLL_SECONDS, default=current):
                    vol.All(int, vol.Range(min=MIN_POLL_SECONDS, max=3600)),
            }),
        )
