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
import socket
import struct
import time
from dataclasses import dataclass, replace
from typing import Any

from . import control
from .control import REG_MODE, BrokerConfig, MqttControl, frame03, mqtt_payload

_LOGGER = logging.getLogger(__name__)

ADDON_REPO = "https://github.com/seanlewis/openhomepower-broker"
ADDON_SLUG = "openhomepower_broker"
# 0.2.1 couldn't read its own password file and exited at start.
MIN_ADDON_VERSION = (0, 2, 2)
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
        raise MoveError("The broker add-on is out of date. Update it (version 0.2.2 or "
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
    existing = str(login.get("username") or "")
    if existing and existing != battery_user:
        # The add-on has one battery login for every battery it serves. Replacing
        # it only matters if another battery is relying on it; with just this
        # battery configured, the old value can't be in use (typically it was
        # typed in by hand and is wrong), so replace it.
        others = [d for d in devices if str(d.get("serial")) != serial]
        if others:
            raise MoveError(
                f"The broker add-on's battery login is '{existing}', but this battery "
                f"uses '{battery_user}', and the add-on is also set up for another "
                "battery that may rely on the current one. Check the add-on's "
                "Configuration tab.")
        _LOGGER.warning("replacing the broker add-on's battery login '%s' with '%s' "
                        "read from the battery (no other battery configured)",
                        existing, battery_user)
    new["battery_login"] = {"username": battery_user, "password": battery_password}
    return new, client_password


def addon_version_ok(version: str | None) -> bool:
    """True if the installed add-on is new enough. Unknown versions pass (the
    login check that follows still catches a broker that isn't running)."""
    try:
        parts = tuple(int(p) for p in str(version).split(".")[:3])
    except ValueError:
        return True
    return parts >= MIN_ADDON_VERSION


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
                out = (result.stdout or "").strip()
                if result.stderr:
                    _LOGGER.debug("gateway %s stderr: %s", self.login.host,
                                  result.stderr.strip())
                return out
        except asyncssh.PermissionDenied as exc:
            _LOGGER.warning("gateway %s rejected the SSH login for %s",
                            self.login.host, self.login.username)
            raise MoveError("The battery rejected the SSH login.") from exc
        except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
            _LOGGER.debug("gateway %s: SSH failed: %r", self.login.host, exc)
            raise MoveError(f"Couldn't connect to the battery at {self.login.host} "
                            f"over SSH ({exc}).") from exc

    async def check_include(self) -> None:
        included = await self.run(INCLUDE_CHECK)
        _LOGGER.debug("gateway %s: firewall.user included at boot: %s",
                      self.login.host, included)
        if included != "yes":
            raise MoveError("This battery's firewall doesn't load /etc/firewall.user at "
                            "boot, so the redirect wouldn't survive a restart. Use the "
                            "manual instructions instead.")

    async def apply_redirect(self, ip: str, port: int) -> None:
        out = await self.run(apply_script(redirect_rule(ip, port)))
        _LOGGER.info("gateway %s: redirect to %s:%s written (%s rule line)",
                     self.login.host, ip, port, out or "no")
        if out.splitlines()[-1:] != ["1"]:
            raise MoveError(f"Writing the redirect rule didn't take effect ({out!r}).")

    async def remove_redirect(self) -> None:
        out = await self.run(remove_script())
        _LOGGER.info("gateway %s: redirect removed (%s rule lines left)",
                     self.login.host, out or "0")
        if out.splitlines()[-1:] not in (["0"], []):
            raise MoveError(f"Removing the redirect rule didn't take effect ({out!r}).")

    async def reboot(self) -> None:
        _LOGGER.info("gateway %s: rebooting", self.login.host)
        await self.run(REBOOT)

    async def wait_until_back(self, timeout: float = GATEWAY_BACK_TIMEOUT) -> None:
        """After a reboot: wait for SSH to answer again."""
        await asyncio.sleep(30)                 # it takes at least this long to go down and up
        start = time.monotonic()
        end = start + timeout
        while True:
            try:
                await self.run("true", timeout=10)
                _LOGGER.info("gateway %s: back after reboot (%.0fs)", self.login.host,
                             30 + time.monotonic() - start)
                return
            except MoveError:
                if time.monotonic() > end:
                    _LOGGER.warning("gateway %s: not reachable %.0fs after reboot",
                                    self.login.host, 30 + timeout)
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
        except (OSError, ConnectionError) as err:
            if time.monotonic() > end:
                _LOGGER.warning("broker at %s:%s didn't accept the login for %s: %s",
                                cfg.host, cfg.port, cfg.username, err)
                return False
            await asyncio.sleep(3)


def probe_battery(cfg: BrokerConfig, window: float) -> str | None:
    """Listen on `cfg`'s broker for any sign of the battery. Blocking.

    Subscribes to everything under the battery's serial and asks for data two
    ways — a settings read and a read-all — because units differ in which they
    answer. Anything the battery publishes (a reply to either, or an unprompted
    push) proves it is on this broker; only our own requests, on `/Input`
    topics, are ignored. Returns the topic that answered, or None. Raises
    OSError/ConnectionError if the broker itself can't be reached.
    """
    base = f"Enertek/{cfg.serial}"
    s = MqttControl(cfg)._connect()
    try:
        sub = struct.pack("!H", 1) + control._ms(f"{base}/#".encode()) + bytes([0])
        s.send(bytes([0x82]) + control._rlen(len(sub)) + sub)
        requests = [
            (f"{base}/DataTransmission/Input", mqtt_payload(frame03(REG_MODE, 1), seq=2)),
            (f"{base}/Read_All_Input_Registers/Input", bytes([0x31, 0x02]) + b"\xff\xff"),
        ]
        buf = b""
        end = time.monotonic() + window
        next_request = 0.0
        while time.monotonic() < end:
            if time.monotonic() >= next_request:
                for topic, payload in requests:
                    pub = control._ms(topic.encode()) + payload
                    s.send(bytes([0x30]) + control._rlen(len(pub)) + pub)
                next_request = time.monotonic() + 20
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
                _LOGGER.debug("move check: %s (%d bytes)", topic, len(payload))
                if not topic.endswith("/Input"):
                    return topic
        return None
    finally:
        s.close()


async def battery_answers(hass, cfg: BrokerConfig, timeout: float = VERIFY_TIMEOUT) -> bool:
    """Poll until the battery shows up on `cfg`'s broker (see probe_battery)."""
    probe = replace(cfg, client_id=f"openhomepower-ha-move-{cfg.serial}")
    start = time.monotonic()
    end = start + timeout
    last_problem = "no message from the battery yet"
    while time.monotonic() < end:
        window = max(5.0, min(30.0, end - time.monotonic()))
        try:
            topic = await hass.async_add_executor_job(probe_battery, probe, window)
        except (OSError, ConnectionError) as err:
            last_problem = f"couldn't use the broker at {cfg.host}:{cfg.port}: {err}"
            _LOGGER.debug("move check: %s", last_problem)
            await asyncio.sleep(VERIFY_INTERVAL)
            continue
        if topic:
            _LOGGER.info("battery %s answered on %s:%s (%s) after %.0fs",
                         cfg.serial, cfg.host, cfg.port, topic, time.monotonic() - start)
            return True
        last_problem = "the broker was reachable but the battery sent nothing"
    _LOGGER.warning("battery %s didn't appear on %s:%s within %.0fs (%s)",
                    cfg.serial, cfg.host, cfg.port, timeout, last_problem)
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
        candidates = addon_slug_candidates(installed)
        _LOGGER.debug("looking for the broker add-on as: %s", candidates)
        for slug in candidates:
            manager = AddonManager(hass, _LOGGER, "OpenHomepower Secure Broker", slug)
            try:
                info = await manager.async_get_addon_info()
            except Exception as err:  # noqa: BLE001 - unknown slug on this Supervisor
                _LOGGER.debug("add-on slug %s: %r", slug, err)
                continue
            if info.state is not AddonState.NOT_INSTALLED:
                _LOGGER.info("found the broker add-on: %s (version %s, %s)",
                             slug, info.version, info.state.value)
                if not addon_version_ok(info.version):
                    raise MoveError(
                        f"The broker add-on is version {info.version}, which has a bug "
                        "that stops it starting. Update it to 0.2.2 or later "
                        "(Settings → Add-ons → OpenHomepower Secure Broker → Update), "
                        "then try again.")
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
            _LOGGER.info("broker add-on options saved (%d device(s)); %s it",
                         len(options.get("devices") or []),
                         "restarting" if info.state is AddonState.RUNNING else "starting")
            if info.state is AddonState.RUNNING:
                await self._manager.async_restart_addon()
            else:
                await self._manager.async_start_addon()
        except Exception as exc:  # noqa: BLE001 - AddonError and friends
            _LOGGER.warning("configuring the broker add-on failed: %r", exc)
            raise MoveError(f"Couldn't configure the broker add-on ({exc}).") from exc


async def home_assistant_ip(hass) -> str:
    """Best guess at the address the battery should use to reach us."""
    try:
        from homeassistant.components.network import async_get_source_ip

        return await async_get_source_ip(hass)
    except Exception:  # noqa: BLE001 - the user can type it instead
        return ""
