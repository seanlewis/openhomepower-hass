"""Read Homepower telemetry off an MQTT broker.

The gateway daemon publishes its input-register frames to the broker unprompted
(`Enertek/<serial>/Realtime` and `.../Read_All_Input_Registers/Output`). This
module subscribes, pulls the `0104` frames out of the payloads, and hands them
to a callback — the same frames the SSH path scrapes from the log, so the shared
decoder in protocol.py/registry.py does the rest.

READ-ONLY: this module only subscribes; it never publishes.
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
from .protocol import Frame, FrameError, parse_frame

_LOGGER = logging.getLogger(__name__)

_KEEPALIVE = 20            # PINGREQ cadence (< the 30 s CONNECT keepalive)
_BACKOFF_MAX = 60


class FrameCache:
    """Newest frame per register block, so a full read is a merge of all blocks."""

    def __init__(self) -> None:
        self._by_start: dict[int, Frame] = {}

    def update(self, frame: Frame) -> None:
        self._by_start[frame.start] = frame

    def frames(self) -> list[Frame]:
        return list(self._by_start.values())


def strip_payload(payload: bytes) -> bytes:
    """Remove the daemon's `[seq-digit][0x02]` wrapper if present."""
    if len(payload) > 2 and payload[1] == 0x02:
        return payload[2:]
    return payload


def telemetry_frame(payload: bytes) -> Frame | None:
    """Parse a wrapped MQTT payload into a telemetry Frame, or None if it isn't one.

    Non-frame payloads and CRC failures return None (never raise), so a stray
    publish can't take the subscriber down.
    """
    try:
        return parse_frame(strip_payload(payload))
    except FrameError:
        return None


class MqttReader:
    """Subscribe to the daemon's telemetry topics and emit decoded frames.

    Runs a daemon thread. Reuses control.py's raw-socket MQTT helpers so there is
    no new dependency. Reconnects with backoff until stop() is called.
    """

    def __init__(self, cfg: BrokerConfig,
                 on_update: Callable[[list[Frame]], None],
                 topics: list[str] | None = None) -> None:
        self.cfg = cfg
        self._on_update = on_update
        self._topics = topics or [
            f"Enertek/{cfg.serial}/Realtime",
            f"Enertek/{cfg.serial}/Read_All_Input_Registers/Output",
        ]
        self._cache = FrameCache()
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
            self._thread.join(timeout=5)
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

    def _session(self) -> None:
        s = socket.create_connection((self.cfg.host, self.cfg.port), timeout=10)
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
                time.sleep(0.2)
                s.recv(64)                       # drain SUBACK
            self._recv_loop(s)
        finally:
            self._sock = None
            s.close()

    def _recv_loop(self, s: socket.socket) -> None:
        buf = b""
        last_ping = time.time()
        while not self._stop.is_set():
            if time.time() - last_ping > _KEEPALIVE:
                s.send(b"\xc0\x00")              # PINGREQ
                last_ping = time.time()
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
                frame = telemetry_frame(payload)
                if frame is not None:
                    self._cache.update(frame)
                    self._on_update(self._cache.frames())
