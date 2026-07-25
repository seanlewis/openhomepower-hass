"""Find a Homepower gateway on the local network.

Three tiers, cheapest first. Every tier identifies the device the same way: an
unauthenticated GET of `/` whose page title contains "Energizer Homepower"
(PROTOCOL.md §1).

  1. hostname   — the unit sets a DHCP hostname, so most routers publish it.
                  Typically resolves in well under a second.
  2. neighbours — fingerprint only the hosts the OS already knows are alive
                  (ARP/neighbour table). Usually 10-30 addresses instead of 254.
  3. sweep      — last resort, the full local /24.

The caller falls back to asking the user for an address.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass

# The gateway's network-config page. Unique enough to be definitive, and it
# needs no credentials.
FINGERPRINT = "energizer homepower"
HTTP_PORT = 80
SSH_PORT = 34522
HOSTNAMES = ("Homepower", "homepower", "homepower.local", "Homepower.local")

# Concurrency: two different limits, for two different reasons.
#
# The "scan gently" rule exists because the MT7628 drops connections when the
# SAME host is hit repeatedly in parallel. A subnet sweep is not that: each
# address receives exactly one connection, so the gateway only ever sees one
# regardless of how wide the sweep is. Fanning out across DISTINCT hosts is
# therefore safe, and necessary — at 8-wide with a 2 s timeout a /24 took 60 s,
# which reads as a hang to someone waiting on the setup wizard.
SCAN_CONCURRENCY = 64        # across distinct hosts — safe
DEVICE_CONCURRENCY = 4       # against a single device — keep low

SCAN_CONNECT_TIMEOUT = 1.0   # LAN round trips are sub-millisecond
CONNECT_TIMEOUT = 3.0        # verifying one known host: be patient instead
READ_TIMEOUT = 3.0
MAX_RESPONSE_BYTES = 16384


@dataclass(frozen=True)
class Candidate:
    host: str
    title: str

    def __str__(self) -> str:
        return f"{self.host} ({self.title})"


def local_ipv4() -> str | None:
    """Best-effort local address, without sending anything."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))   # TEST-NET-1, never routed
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


async def _http_title(host: str, connect_timeout: float = CONNECT_TIMEOUT) -> str | None:
    """GET / and return the page <title>, or None."""
    try:
        fut = asyncio.open_connection(host, HTTP_PORT)
        reader, writer = await asyncio.wait_for(fut, connect_timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        writer.write(
            f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        # Read until we have the title, hit EOF, or reach a sane cap.
        #
        # A single read() is NOT enough: it returns as soon as any bytes are
        # available, which is often just the HTTP headers. That produced an
        # intermittent miss — the device was found when the whole response
        # happened to arrive in one packet, and silently skipped when it did
        # not, which looked like random scan unreliability.
        chunks: list[bytes] = []
        total = 0
        deadline = asyncio.get_event_loop().time() + READ_TIMEOUT
        while total < MAX_RESPONSE_BYTES:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            chunk = await asyncio.wait_for(reader.read(2048), remaining)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if b"</title>" in b"".join(chunks).lower():
                break
        data = b"".join(chunks)
    except (OSError, asyncio.TimeoutError):
        return None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    text = data.decode("utf-8", "replace")
    lo = text.lower()
    start = lo.find("<title>")
    if start == -1:
        return None
    end = lo.find("</title>", start)
    return text[start + 7:end].strip() if end != -1 else None


async def _check(host: str, connect_timeout: float = CONNECT_TIMEOUT) -> Candidate | None:
    title = await _http_title(host, connect_timeout)
    if title and FINGERPRINT in title.lower():
        return Candidate(host=host, title=title)
    return None


def neighbours() -> list[str]:
    """IPv4 addresses the OS already knows are alive on this subnet.

    Reading the ARP/neighbour table is instant, generates no traffic, and
    typically yields 10-30 real hosts instead of 254 mostly-dead addresses.
    That matters for more than speed: blind-sweeping a /24 floods the
    neighbour table, and in testing it evicted the very device we were looking
    for — the sweep found the battery only about half the time.
    """
    import re
    import subprocess

    commands = (
        ["ip", "neigh", "show"],     # modern Linux
        ["arp", "-an"],              # macOS, BSD, older Linux
        ["arp", "-a"],               # Windows
    )
    text = ""
    for cmd in commands:
        try:
            text = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if text.strip():
            break
    if not text:
        return []

    local = local_ipv4()
    prefix = local.rsplit(".", 1)[0] + "." if local else None
    # A resolved entry carries a hardware address. Unresolved ones appear as
    # "(incomplete)" / "FAILED" — and a previous subnet sweep leaves one of
    # those behind for every dead address, so without this check the table
    # looks like the entire /24 is alive.
    ip_re = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    mac_re = re.compile(r"\b(?:[0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2}\b")

    found = []
    for line in text.splitlines():
        if not mac_re.search(line):
            continue
        ips = ip_re.findall(line)
        if not ips:
            continue
        ip = ips[0]
        if prefix and not ip.startswith(prefix):
            continue
        if ip.endswith(".255") or ip == local:
            continue
        if ip not in found:
            found.append(ip)
    return found


async def by_neighbours(progress=None) -> list[Candidate]:
    """Fingerprint only hosts the OS already knows about."""
    hosts = neighbours()
    if not hosts:
        return []
    sem = asyncio.Semaphore(DEVICE_CONCURRENCY * 4)
    done = 0
    found: list[Candidate] = []

    async def one(host):
        nonlocal done
        async with sem:
            result = await _check(host, CONNECT_TIMEOUT)
        done += 1
        if progress:
            progress(done, len(hosts))
        if result:
            found.append(result)

    await asyncio.gather(*(one(h) for h in hosts))
    return found


async def by_hostname() -> Candidate | None:
    """Try the DHCP hostname the unit advertises."""
    for name in HOSTNAMES:
        try:
            socket.gethostbyname(name)
        except OSError:
            continue
        found = await _check(name)
        if found:
            return found
    return None


async def scan_subnet(cidr: str | None = None, progress=None) -> list[Candidate]:
    """Fingerprint every host on the local /24."""
    if cidr is None:
        ip = local_ipv4()
        if not ip:
            return []
        cidr = f"{ip}/24"
    net = ipaddress.ip_network(cidr, strict=False)
    hosts = [str(h) for h in net.hosts()]

    sem = asyncio.Semaphore(SCAN_CONCURRENCY)
    done = 0
    found: list[Candidate] = []

    async def one(host):
        nonlocal done
        async with sem:
            result = await _check(host, SCAN_CONNECT_TIMEOUT)
        done += 1
        if progress:
            progress(done, len(hosts))
        if result:
            found.append(result)

    await asyncio.gather(*(one(h) for h in hosts))
    return found


async def discover(progress=None) -> list[Candidate]:
    """Find the gateway, cheapest method first.

    1. hostname       — instant when the router publishes the DHCP name
    2. neighbours     — fingerprint hosts the OS already knows are alive
    3. full sweep     — last resort; slower and less reliable (see neighbours())
    """
    quick = await by_hostname()
    if quick:
        return [quick]

    known = await by_neighbours(progress=progress)
    if known:
        return known

    return await scan_subnet(progress=progress)


async def looks_like_gateway(host: str) -> bool:
    """Verify a user-entered address actually is a Homepower."""
    return await _check(host) is not None


def running_in_container() -> bool:
    """Docker Desktop on macOS/Windows cannot see the host LAN.

    Used to explain a failed scan accurately instead of showing a generic
    'not found', which would otherwise be the most common support issue.
    """
    import os
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8") as fh:
            return "docker" in fh.read() or "containerd" in fh.read()
    except OSError:
        return False
