"""OpenHomepower — local monitoring for Energizer Homepower batteries, with
opt-in control.

Reads are local and read-only — SSH log scrape or MQTT broker subscription.
Control is opt-in and off by default; when enabled it publishes to a
configurable broker (the vendor broker today, a local broker after cutover).

Not affiliated with, endorsed by, or supported by Energizer, 8 Star Energy or
Enertek Holdings.
"""
from __future__ import annotations

import logging
from dataclasses import replace

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady

from . import control
from .const import (
    CONF_BROKER_HOST,
    CONF_BROKER_PASSWORD,
    CONF_BROKER_PORT,
    CONF_BROKER_USER,
    CONF_CONTROL_ENABLED,
    CONF_POLL_SECONDS,
    CONF_READ_SOURCE,
    CONF_STALE_SECONDS,
    CONF_TOPIC_SERIAL,
    DEFAULT_BROKER_PORT,
    DEFAULT_POLL_SECONDS,
    DEFAULT_STALE_SECONDS,
    DOMAIN,
    READ_SOURCE_MQTT,
    READ_SOURCE_SSH,
    SERVICE_SET_SCHEDULE,
)
from .control import BrokerConfig, MqttControl
from .control_coordinator import ControlCoordinator, MqttConfigReader, SshConfigReader
from .coordinator import HomepowerCoordinator
from .mqtt_coordinator import MqttReadCoordinator
from .registry import RegisterMap
from .transport import Credentials

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SELECT, Platform.NUMBER]

SET_SCHEDULE_SCHEMA = vol.Schema({vol.Required("schedule"): dict})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OpenHomepower from a config entry."""
    # Loading the register map reads a file; keep it off the event loop.
    regmap = await hass.async_add_executor_job(RegisterMap.load)

    # Interpret a stored entry, NOT the config-flow form default. Entries created
    # before the read-source setting existed (pre-0.3.0) carry no CONF_READ_SOURCE
    # and are SSH by definition — falling back to the current default (MQTT) would
    # mis-read them and crash on the absent MQTT broker keys. New entries always
    # stamp CONF_READ_SOURCE, so this fallback only ever applies to legacy ones.
    source = entry.data.get(CONF_READ_SOURCE, READ_SOURCE_SSH)
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

    try:
        store: dict = {"coordinator": coordinator, "control": None, "mqtt": None}

        broker = _broker_config(entry)
        if broker is not None:
            mqtt = MqttControl(broker)
            # Control read-back follows the entry's read source; writes are MQTT.
            if source == READ_SOURCE_MQTT:
                # A distinct client-id for the read poll so it can never evict
                # (or be evicted by) a concurrent write on the write client-id.
                read_cfg = replace(broker, client_id=f"openhomepower-ha-cfg-{broker.serial}")
                reader = MqttConfigReader(hass, MqttControl(read_cfg))
            else:
                reader = SshConfigReader(coordinator.gateway)
            control_coordinator = ControlCoordinator(hass, reader, mqtt)
            # Best-effort: a control-read hiccup must not block setup.
            await control_coordinator.async_refresh()
            store["control"] = control_coordinator
            store["mqtt"] = mqtt

        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = store
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # Setup failed after the reader/gateway were live; stop them so HA's
        # retry doesn't leak a second reader on the same client_id.
        await coordinator.async_shutdown()
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        raise
    _register_services(hass)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


def _broker_config(entry: ConfigEntry) -> BrokerConfig | None:
    """The configured control broker, or None if control isn't fully set up.

    All broker settings come from config (derived from the device at setup); no
    vendor secrets are baked into this source, so nothing falls back to a hardcoded
    credential.
    """
    if not entry.options.get(CONF_CONTROL_ENABLED, False):
        return None
    host = str(entry.options.get(CONF_BROKER_HOST, "")).strip()
    user = str(entry.options.get(CONF_BROKER_USER, "")).strip()
    password = str(entry.options.get(CONF_BROKER_PASSWORD, "")).strip()
    serial = str(entry.options.get(CONF_TOPIC_SERIAL, "")).strip()
    if not (host and user and password and serial):
        _LOGGER.warning("Control enabled but broker settings incomplete; skipping control")
        return None
    return BrokerConfig(
        host=host,
        port=int(entry.options.get(CONF_BROKER_PORT, DEFAULT_BROKER_PORT)),
        username=user,
        password=password,
        serial=serial,
        client_id=f"openhomepower-ha-{serial}",
    )


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE):
        return

    async def _set_schedule(call: ServiceCall) -> None:
        windows = control.schedule_json_to_windows(call.data["schedule"])
        frame = control.build_schedule(windows)
        published = False
        for store in hass.data.get(DOMAIN, {}).values():
            mqtt: MqttControl | None = store.get("mqtt")
            if mqtt is not None:
                await hass.async_add_executor_job(mqtt.publish, frame)
                published = True
        if not published:
            _LOGGER.warning("set_schedule called but no entry has control enabled")

    hass.services.async_register(
        DOMAIN, SERVICE_SET_SCHEDULE, _set_schedule, schema=SET_SCHEDULE_SCHEMA
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        store = hass.data[DOMAIN].pop(entry.entry_id)
        await store["coordinator"].async_shutdown()
        if not any(s.get("mqtt") for s in hass.data[DOMAIN].values()):
            if hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE):
                hass.services.async_remove(DOMAIN, SERVICE_SET_SCHEDULE)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change (poll interval, or control settings)."""
    await hass.config_entries.async_reload(entry.entry_id)
