"""OpenHomepower — local, read-only monitoring for Energizer Homepower batteries.

Not affiliated with, endorsed by, or supported by Energizer, 8 Star Energy or
Enertek Holdings.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant

from .const import CONF_POLL_SECONDS, DEFAULT_POLL_SECONDS, DOMAIN
from .coordinator import HomepowerCoordinator
from .registry import RegisterMap
from .transport import Credentials

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OpenHomepower from a config entry."""
    # Loading the register map reads a file; keep it off the event loop.
    regmap = await hass.async_add_executor_job(RegisterMap.load)

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

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: HomepowerCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.gateway.close()
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change (e.g. the poll interval)."""
    await hass.config_entries.async_reload(entry.entry_id)
