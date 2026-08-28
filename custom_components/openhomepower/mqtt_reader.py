"""Read Homepower telemetry off an MQTT broker.

The gateway daemon does not stream telemetry unprompted: it answers a *request*.
We publish a read-all request to `Enertek/<serial>/Read_All_Input_Registers/Input`
and the daemon replies on `.../Output` with the full input-register bank as a raw
little-endian `u16` array (NOT the framed `0104` format the SSH log carries). That
array feeds the shared registry decoder unchanged.

We deliberately do NOT consume the daemon's unprompted `Realtime` pushes: their
header layout varies between units (e.g. 570 vs 526 bytes), so decoding them at a
fixed offset produces a byte-shifted, garbage reading. The reply to our own
request is consistent across units, so we rely on it alone.

READ-ONLY: the only thing this module publishes is the read-all *request*
(`ffff`) — it never writes a register. Writes remain the opt-in control path.
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from collections.abc import Callable

from . import control
from .control import BrokerConfig

_LOGGER = logging.getLogger(__name__)

_KEEPALIVE = 20            # PINGREQ cadence (< the 30 s CONNECT keepalive)
_BACKOFF_MAX = 60
_CONNECT_TIMEOUT = 8       # bounds how long a hung TCP connect can block stop()
_REQUEST_INTERVAL = 30     # how often to ask the daemon for a fresh reading
_REALTIME_HEADER = 6       # bytes of header before the register array on Realtime


def strip_payload(payload: bytes) -> bytes:
    """Remove the daemon's `[seq-digit][0x02]` wrapper if present.

    Strips whenever the 2-byte wrapper is present, even with nothing after it, so
    a bare wrapper collapses to empty rather than being read as a register.
    """
    if len(payload) >= 2 and payload[1] == 0x02:
        return payload[2:]
    return payload


def registers_from_payload(topic: str, payload: bytes) -> dict[int, int] | None:
    """Decode a telemetry payload into a {register: value} map, or None.

    Both telemetry topics carry a raw little-endian `u16` register array behind
    the `[seq][0x02]` wrapper; `Realtime` prefixes it with a 6-byte header, the
    `Read_All_Input_Registers` reply does not. Returns None for anything too
    short to be a register dump, so a stray publish can't take the reader down.
    """
    body = strip_payload(payload)
    offset = _REALTIME_HEADER if topic.endswith("Realtime") else 0
    if len(body) < offset + 2:
        return None
    body = body[offset:]
    return {i: int.from_bytes(body[2 * i:2 * i + 2], "little")
            for i in range(len(body) // 2)}


def plausible_reading(readings: dict) -> bool:
    """Reject a byte-shifted / partial decode.

    Some units answer the read-all request with a reply that doesn't decode to
    the full register bank, seen as readings flickering between good values and
    garbage (a non-numeric serial, an out-of-range SOC). A genuine full-bank
    reading has an all-digit serial and a 0-100 SOC — drop anything that doesn't,
    so the corrupt frames never reach the entities. `readings` maps key -> an
    object with a `.value` (registry Reading).
    """
    ser = readings.get("device_serial")
    soc = readings.get("battery_soc_pct")
    if ser is None or not str(ser.value).isdigit():
        return False
    if soc is None or soc.value is None or not (0 <= soc.value <= 100):
        return False
    return True


class MqttReader:
    """Poll the daemon for telemetry over MQTT and emit decoded register maps.

    Runs a daemon thread. Reuses control.py's raw-socket MQTT helpers so there is
    no new dependency. Reconnects with backoff until stop() is called.
    """

    def __init__(self, cfg: BrokerConfig,
                 on_update: Callable[[dict[int, int]], None],
                 topics: list[str] | None = None,
                 request_interval: int = _REQUEST_INTERVAL) -> None:
        self.cfg = cfg
        self._on_update = on_update
        # Only the reply to our own request. NOT `.../Realtime` — its header
        # layout varies per unit, so decoding it byte-shifts into garbage (seen
        # as readings alternating between good and corrupt values).
        self._topics = topics if topics is not None else [
            f"Enertek/{cfg.serial}/Read_All_Input_Registers/Output",
        ]
        self._request_topic = f"Enertek/{cfg.serial}/Read_All_Input_Registers/Input"
        self._request_interval = request_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="openhomepower-mqtt-reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=_CONNECT_TIMEOUT + 5)
            if self._thread.is_alive():
                _LOGGER.warning("mqtt reader thread did not stop within timeout")
            else:
                self._thread = None

    def _run(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 1
            except (OSError, ConnectionError) as err:
                if self._stop.is_set():
                    break
                _LOGGER.debug("mqtt reader session ended (%s); retry in %ds", err, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)
            except Exception:  # noqa: BLE001 - never let the reader thread die silently
                if self._stop.is_set():
                    break
                _LOGGER.exception("mqtt reader session crashed unexpectedly; retry in %ds", backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    def _session(self) -> None:
        # Deliberately not calling control._connect(): the CONNECT/CONNACK bytes
        # are inlined here so the connect timeout and the recv loop's stop-check
        # bound shutdown latency (_CONNECT_TIMEOUT + join window), rather than
        # reusing a helper that hides those bounds.
        s = socket.create_connection((self.cfg.host, self.cfg.port),
                                      timeout=_CONNECT_TIMEOUT)
        self._sock = s
        try:
            s.settimeout(3)
            body = (control._ms(b"MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 30)
                    + control._ms(self.cfg.client_id.encode())
                    + control._ms(self.cfg.username.encode())
                    + control._ms(self.cfg.password.encode()))
            s.send(bytes([0x10]) + control._rlen(len(body)) + body)
            time.sleep(0.4)
            ca = s.recv(4)
            if not (len(ca) >= 4 and ca[0] == 0x20 and ca[3] == 0):
                raise ConnectionError(f"CONNACK failed: {ca!r}")
            for i, topic in enumerate(self._topics):
                sub = struct.pack("!H", i + 1) + control._ms(topic.encode()) + bytes([0])
                s.send(bytes([0x82]) + control._rlen(len(sub)) + sub)
            # No separate SUBACK drain: control._next_publish skips non-PUBLISH
            # packets (SUBACK is type 9), so _recv_loop consumes them safely
            # without risking an over-read that clips a following PUBLISH.
            self._recv_loop(s)
        finally:
            self._sock = None
            s.close()

    def _send_request(self, s: socket.socket) -> None:
        """Ask the daemon for a full input-register reading. `ffff` = read-all."""
        payload = bytes([0x31, 0x02]) + b"\xff\xff"
        pub = control._ms(self._request_topic.encode()) + payload
        s.send(bytes([0x30]) + control._rlen(len(pub)) + pub)

    def _recv_loop(self, s: socket.socket) -> None:
        buf = b""
        last_ping = time.time()
        last_request = 0.0                       # request immediately on entry
        while not self._stop.is_set():
            now = time.time()
            if now - last_request > self._request_interval:
                self._send_request(s)
                last_request = now
            if now - last_ping > _KEEPALIVE:
                s.send(b"\xc0\x00")              # PINGREQ
                last_ping = now
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
                # Logs the raw wire format (enable debug logging for this
                # component to diagnose a unit whose reply differs from the norm).
                _LOGGER.debug("telemetry payload: topic=%s len=%d head=%s",
                              topic, len(payload), payload[:24].hex())
                try:
                    regs = registers_from_payload(topic, payload)
                    if regs:
                        self._on_update(regs)
                except Exception:  # noqa: BLE001 - one bad payload must not kill the reader
                    _LOGGER.exception("dropping a telemetry payload the reader could not handle")
