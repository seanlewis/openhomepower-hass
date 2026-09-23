"""Move a battery between the vendor broker and the local OpenHomepower broker.

The gateway's broker address is compiled into its firmware, so the battery is
moved at the network layer: one iptables rule in /etc/firewall.user redirects
its outbound MQTT (port 1884) to the local broker, and a reboot applies it
cleanly. This is the same rule the manual instructions use, so either route can
undo the other.

THIS MODULE WRITES TO THE GATEWAY. It is kept apart from transport.py, which is
read-only by construction. The only things it ever changes are:
  * /etc/firewall.user — adds or removes our one redirect line (the file is
    backed up once, before the first change);
  * a reboot, to apply that change;
  * the OpenHomepower broker add-on's options, via the Supervisor.

Pure helpers (validation, shell scripts, option merging) are at the top and are
unit-tested without Home Assistant; HA/asyncssh are imported lazily below them.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import secrets
import time
from dataclasses import dataclass, replace
from typing import Any

from .control import REG_MODE, BrokerConfig, MqttControl

_LOGGER = logging.getLogger(__name__)

ADDON_REPO = "https://github.com/seanlewis/openhomepower-broker"
ADDON_SLUG = "openhomepower_broker"
LOCAL_BROKER_PORT = 1885
VENDOR_MQTT_PORT = 1884               # what the daemon dials; the rule matches on it

FIREWALL_USER = "/etc/firewall.user"
FIREWALL_BACKUP = "/etc/firewall.user.openhomepower.bak"
RULE_MATCH = "--dport 1884 -j DNAT"   # identifies our line (and a manual one)

# Timings. The gateway takes a minute or two to reboot and rejoin Wi-Fi, and the
# daemon connects to its broker shortly after.
VERIFY_TIMEOUT = 360
VERIFY_INTERVAL = 10
GATEWAY_BACK_TIMEOUT = 240
BROKER_READY_TIMEOUT = 60


class MoveError(Exception):
    """A step failed. The message is shown to the user as-is."""


# --- pure helpers ---------------------------------------------------------------

def redirect_rule(ip: str, port: int) -> str:
    """The iptables line for firewall.user. Validates both parts strictly,
    because they end up in a shell command on the gateway."""
    try:
        addr = ipaddress.IPv4Address(ip.strip())
    except ValueError as exc:
        raise MoveError(f"'{ip}' isn't a valid IPv4 address.") from exc
    if addr.is_loopback or addr.is_unspecified or addr.is_multicast:
        raise MoveError(f"'{ip}' can't be used: the battery must reach this address "
                        "over your network.")
    port = int(port)
    if not 1 <= port <= 65535:
        raise MoveError(f"'{port}' isn't a valid port.")
    return (f"iptables -t nat -A OUTPUT -p tcp --dport {VENDOR_MQTT_PORT} "
            f"-j DNAT --to-destination {addr}:{port}")


def apply_script(rule: str) -> str:
    """Back up firewall.user once, replace any existing redirect with `rule`,
    and print how many redirect lines remain (must be 1)."""
    return (
        f"F={FIREWALL_USER}; B={FIREWALL_BACKUP}; "
        '[ -f "$F" ] || touch "$F"; '
        '[ -f "$B" ] || cp "$F" "$B"; '
        f"sed -i '/{RULE_MATCH}/d' \"$F\" && "
        f"echo '{rule}' >> \"$F\" && "
        f"grep -c -- '{RULE_MATCH}' \"$F\""
    )


def remove_script() -> str:
    """Remove any redirect line and print how many remain (must be 0)."""
    return (
        f"F={FIREWALL_USER}; "
        '[ -f "$F" ] || exit 0; '
        f"sed -i '/{RULE_MATCH}/d' \"$F\" && "
        f"(grep -c -- '{RULE_MATCH}' \"$F\" || true)"
    )


# firewall.user only runs at boot if the firewall config includes it (the
# OpenWrt default). Without that the rule would vanish on reboot.
INCLUDE_CHECK = "uci -q show firewall | grep -q \"path='/etc/firewall.user'\" && echo yes || echo no"

# Detach so the SSH command returns before the connection drops.
REBOOT = "( sleep 2; reboot ) </dev/null >/dev/null 2>&1 &"


def merge_addon_options(options: dict[str, Any], serial: str, battery_user: str,
                        battery_password: str) -> tuple[dict[str, Any], str]:
    """Add this battery to the add-on's options. Returns (new_options, client_password).

    An existing entry for the serial keeps its password (the user may already use
    it elsewhere); otherwise a new one is generated. The battery login is set if
    blank, and must match if already set — overwriting a different one would cut
    off whichever battery uses it.
    """
    if "battery_login" not in options:
        raise MoveError("The broker add-on is out of date. Update it (version 0.2.1 or "
                        "later) and try again.")
    new = dict(options)
    devices = [dict(d) for d in new.get("devices") or []]
    mine = next((d for d in devices if str(d.get("serial")) == serial), None)
    if mine and mine.get("password"):
        client_password = str(mine["password"])
    else:
        client_password = secrets.token_urlsafe(18)
        if mine:
            mine["password"] = client_password
        else:
            devices.append({"serial": serial, "password": client_password})
    new["devices"] = devices

    login = dict(new.get("battery_login") or {})
    if login.get("username") and login["username"] != battery_user:
        raise MoveError("The broker add-on already has a different battery login "
                        "configured. Check its Configuration tab.")
    new["battery_login"] = {"username": battery_user, "password": battery_password}
    return new, client_password


def addon_slug_candidates(installed_slugs: list[str]) -> list[str]:
    """Slugs the add-on could be installed under, most likely first.

    The Supervisor prefixes a store add-on's slug with a hash of its repository
    URL, so look for any installed slug ending in ours, then fall back to the
    hash of the published repository URL and a locally-built copy.
    """
    found = [s for s in installed_slugs if s.endswith(f"_{ADDON_SLUG}")]
    repo_hash = hashlib.sha1(ADDON_REPO.lower().encode()).hexdigest()[:8]
    for slug in (f"{repo_hash}_{ADDON_SLUG}", f"local_{ADDON_SLUG}"):
        if slug not in found:
            found.append(slug)
    return found


# --- the gateway (asyncssh; writes) ----------------------------------------------

@dataclass
class GatewayLogin:
    host: str
    port: int
    username: str
    password: str


class GatewayAdmin:
    """Short-lived SSH sessions for the few write operations above."""

    def __init__(self, login: GatewayLogin):
        self.login = login

    async def run(self, command: str, timeout: float = 20) -> str:
        import asyncssh

        try:
            async with asyncssh.connect(
                self.login.host, port=self.login.port,
                username=self.login.username, password=self.login.password,
                known_hosts=None, client_keys=None,
            ) as conn:
                result = await asyncio.wait_for(conn.run(command), timeout)
                return (result.stdout or "").strip()
        except asyncssh.PermissionDenied as exc:
            raise MoveError("The battery rejected the SSH login.") from exc
        except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
            raise MoveError(f"Couldn't connect to the battery at {self.login.host} "
                            f"over SSH ({exc}).") from exc

    async def check_include(self) -> None:
        if await self.run(INCLUDE_CHECK) != "yes":
            raise MoveError("This battery's firewall doesn't load /etc/firewall.user at "
                            "boot, so the redirect wouldn't survive a restart. Use the "
                            "manual instructions instead.")

    async def apply_redirect(self, ip: str, port: int) -> None:
        out = await self.run(apply_script(redirect_rule(ip, port)))
        if out.splitlines()[-1:] != ["1"]:
            raise MoveError(f"Writing the redirect rule didn't take effect ({out!r}).")

    async def remove_redirect(self) -> None:
        out = await self.run(remove_script())
        if out.splitlines()[-1:] not in (["0"], []):
            raise MoveError(f"Removing the redirect rule didn't take effect ({out!r}).")

    async def reboot(self) -> None:
        await self.run(REBOOT)

    async def wait_until_back(self, timeout: float = GATEWAY_BACK_TIMEOUT) -> None:
        """After a reboot: wait for SSH to answer again."""
        await asyncio.sleep(30)                 # it takes at least this long to go down and up
        end = time.monotonic() + timeout
        while True:
            try:
                await self.run("true", timeout=10)
                return
            except MoveError:
                if time.monotonic() > end:
                    raise
                await asyncio.sleep(10)


# --- brokers ----------------------------------------------------------------------

async def broker_accepts(hass, cfg: BrokerConfig, timeout: float) -> bool:
    """Poll until `cfg` can log in to its broker (e.g. after an add-on restart)."""
    probe = replace(cfg, client_id=f"openhomepower-ha-move-{cfg.serial}")
    end = time.monotonic() + timeout
    while True:
        try:
            await hass.async_add_executor_job(MqttControl(probe).check_login)
            return True
        except (OSError, ConnectionError):
            if time.monotonic() > end:
                return False
            await asyncio.sleep(3)


async def battery_answers(hass, cfg: BrokerConfig, timeout: float = VERIFY_TIMEOUT) -> bool:
    """Poll until the battery answers a read through `cfg`'s broker.

    A reply can only come from the battery itself, so this is proof it's on
    that broker — the same check used when this was first done by hand.
    """
    probe = replace(cfg, client_id=f"openhomepower-ha-move-{cfg.serial}")
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            await hass.async_add_executor_job(MqttControl(probe).read, REG_MODE, 1, 15)
            return True
        except (OSError, ConnectionError, TimeoutError):
            await asyncio.sleep(VERIFY_INTERVAL)
    return False


# --- the add-on (Supervisor) -------------------------------------------------------

class BrokerAddon:
    """The OpenHomepower broker add-on, managed through the Supervisor."""

    def __init__(self, hass, manager):
        self.hass = hass
        self._manager = manager

    @classmethod
    async def find(cls, hass) -> BrokerAddon:
        try:
            from homeassistant.components.hassio import AddonManager, AddonState, get_addons_info
            from homeassistant.helpers.hassio import is_hassio
        except ImportError as exc:
            raise MoveError("This needs Home Assistant OS or Supervised. Use the manual "
                            "instructions instead.") from exc
        if not is_hassio(hass):
            raise MoveError("This needs Home Assistant OS or Supervised (add-ons aren't "
                            "available on this install). Use the manual instructions "
                            "instead.")
        try:
            installed = list((get_addons_info(hass) or {}).keys())
        except Exception:  # noqa: BLE001 - not ready yet; fall back to known slugs
            installed = []
        for slug in addon_slug_candidates(installed):
            manager = AddonManager(hass, _LOGGER, "OpenHomepower Secure Broker", slug)
            try:
                info = await manager.async_get_addon_info()
            except Exception:  # noqa: BLE001 - unknown slug on this Supervisor
                continue
            if info.state is not AddonState.NOT_INSTALLED:
                return cls(hass, manager)
        raise MoveError("The OpenHomepower Secure Broker add-on isn't installed. Install "
                        "it first (see the integration's README), then try again.")

    async def options(self) -> dict[str, Any]:
        return dict((await self._manager.async_get_addon_info()).options)

    async def apply(self, options: dict[str, Any]) -> None:
        """Save options and (re)start so they take effect."""
        from homeassistant.components.hassio import AddonState

        try:
            await self._manager.async_set_addon_options(options)
            info = await self._manager.async_get_addon_info()
            if info.state is AddonState.RUNNING:
                await self._manager.async_restart_addon()
            else:
                await self._manager.async_start_addon()
        except Exception as exc:  # noqa: BLE001 - AddonError and friends
            raise MoveError(f"Couldn't configure the broker add-on ({exc}).") from exc


async def home_assistant_ip(hass) -> str:
    """Best guess at the address the battery should use to reach us."""
    try:
        from homeassistant.components.network import async_get_source_ip

        return await async_get_source_ip(hass)
    except Exception:  # noqa: BLE001 - the user can type it instead
        return ""
