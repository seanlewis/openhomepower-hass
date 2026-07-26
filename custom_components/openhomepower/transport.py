"""SSH transport: read telemetry frames off the gateway.

Uses asyncssh rather than shelling out to `ssh`, which means no system ssh
binary is required and behaviour is identical on Windows, macOS and Linux.
(The original prototype drove a Unix pty to answer the password prompt — that
approach can never work on Windows.)

READ-ONLY. The only command this module ever runs is a grep over the vendor
daemon's log file. It does not write, configure, or touch the serial line.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import asyncssh

from .protocol import Frame, iter_frames

log = logging.getLogger(__name__)

DEFAULT_PORT = 34522
DEFAULT_USER = "homepower"
DEFAULT_PASSWORD = "123456"      # vendor default, published in Enertek's own PDF
LOG_PATH = "/tmp/wemonitor.log"

# Matches RESPONSE frames only: after the 0104 header a response carries a
# 10-digit ASCII device serial, i.e. ten bytes in the range 0x30-0x39. Requests
# have ten zero bytes there instead, so this filters them out at the source.
# (Note the regex quantifier braces — do not run this through str.format.)
RESPONSE_PATTERN = r"0104(3[0-9]){10}[0-9a-f]+"


def _read_command(n: int) -> str:
    """The only command this library ever runs. Read-only by construction."""
    return f"grep -aoE '{RESPONSE_PATTERN}' {LOG_PATH} | tail -{int(n)}"

# One full telemetry cycle is three responses (starts 0, 127, 254). Fetch
# comfortably more than that so a complete bank is always available even when
# the daemon interleaves its clock-only poll.
DEFAULT_FETCH = 12

# Timeouts are chosen so a completely failing poll cycle still finishes inside
# the 60 s update interval: 2 attempts x 15 s + one 3 s backoff = ~33 s worst
# case. (Previously 3 x 30 s + 6 s = 96 s, which overran the interval.)
CONNECT_TIMEOUT = 15
COMMAND_TIMEOUT = 15
RETRY_BACKOFF = 3
DEFAULT_ATTEMPTS = 2

# The gateway periodically re-associates to its AP, which drops the link without
# either end sending a FIN. Without keepalives the dead session sits half-open —
# we have observed exactly that, an orphaned ESTABLISHED session lingering on the
# device. asyncssh keepalives detect it in ~45 s and tear it down, so the next
# poll redials a clean connection instead of blocking on a corpse.
KEEPALIVE_INTERVAL = 15
KEEPALIVE_COUNT_MAX = 3


class TransportError(RuntimeError):
    pass


@dataclass
class Credentials:
    host: str
    port: int = DEFAULT_PORT
    username: str = DEFAULT_USER
    password: str = DEFAULT_PASSWORD


class Gateway:
    """A persistent SSH session to the gateway.

    The connection is held open and reused: the device periodically
    re-associates to its AP, and repeatedly re-handshaking is both slow and
    the main source of transient failures.
    """

    def __init__(self, creds: Credentials):
        self.creds = creds
        self._conn: asyncssh.SSHClientConnection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._conn is not None:
            return
        try:
            self._conn = await asyncio.wait_for(
                asyncssh.connect(
                    self.creds.host,
                    port=self.creds.port,
                    username=self.creds.username,
                    password=self.creds.password,
                    # Host key verification is disabled: this is a LAN appliance
                    # with no key infrastructure, and its login credentials are
                    # the manufacturer's own published defaults, identical on
                    # every unit. There is no secret here for a MITM to steal,
                    # and the session is read-only. Revisit if writes are ever
                    # added.
                    known_hosts=None,
                    client_keys=None,
                    keepalive_interval=KEEPALIVE_INTERVAL,
                    keepalive_count_max=KEEPALIVE_COUNT_MAX,
                ),
                CONNECT_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            raise TransportError("timed out connecting (device may be re-associating "
                                 "to WiFi — retry)") from exc
        except asyncssh.PermissionDenied as exc:
            raise TransportError("wrong username or password") from exc
        except (OSError, asyncssh.Error) as exc:
            raise TransportError(f"could not connect: {exc}") from exc

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            try:
                await self._conn.wait_closed()
            except (OSError, asyncssh.Error):
                pass
            self._conn = None

    async def _run(self, command: str) -> str:
        async with self._lock:
            await self.connect()
            assert self._conn is not None
            try:
                result = await asyncio.wait_for(
                    self._conn.run(command, check=False), COMMAND_TIMEOUT
                )
            except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
                # Connection is probably stale — drop it so the next call redials.
                await self.close()
                raise TransportError(f"command failed: {exc}") from exc
            return result.stdout or ""

    async def read_frames(self, count: int = DEFAULT_FETCH) -> list[Frame]:
        """Fetch the newest frame for each block, giving one complete bank.

        The log interleaves several block types; keeping only the most recent
        frame per start index avoids returning a partial register bank (which
        would silently drop whole groups of readings).
        """
        out = await self._run(_read_command(count))
        newest: dict[int, Frame] = {}
        for frame in iter_frames(out.splitlines()):
            newest[frame.start] = frame          # later lines are newer
        if not newest:
            raise TransportError("no valid frames in the log "
                                 "(is the vendor daemon running?)")
        return [newest[k] for k in sorted(newest)]

    async def read_latest(self, attempts: int = DEFAULT_ATTEMPTS) -> list[Frame]:
        """read_frames with retries, for the device's flaky WiFi.

        The first failure usually means the session died with the WiFi link;
        _run() has already dropped it, so the retry redials.
        """
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return await self.read_frames()
            except TransportError as exc:
                last = exc
                log.debug("read attempt %d/%d failed: %s", attempt + 1, attempts, exc)
                if attempt < attempts - 1:
                    await asyncio.sleep(RETRY_BACKOFF)
        raise TransportError(f"all {attempts} read attempts failed: {last}")


async def probe(creds: Credentials) -> bool:
    """Can we connect and read? Used by the setup wizard."""
    gw = Gateway(creds)
    try:
        await gw.read_frames(count=4)
        return True
    finally:
        await gw.close()
