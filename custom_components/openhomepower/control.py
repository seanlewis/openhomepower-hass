"""Write/control layer for OpenHomepower.

Reverse-engineered from the vendor's own commands (never by writing to a device
to discover it). Builds byte-identical control frames and publishes them to a
CONFIGURABLE MQTT broker — the vendor broker today, a local broker after
cutover. The integration only ever talks to this one broker, so moving off the
vendor cloud is a config change, not a code change.

Safety: an allowlist of only the registers we have observed the vendor write;
the reserve/excess block is written whole (they share registers 120-123);
schedules are a full overwrite; the firmware Upgrade path is never touched.
"""
from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass

# --- register map (only registers observed being written by the vendor) ------
REG_MAX_SOC = 67
REG_RESERVE_ON = 105
REG_RESERVE_BLOCK = 120           # block 120-123 = [const 100, reserve_off, const 2, excess]
REG_EXCESS = 123
REG_SCHEDULE = 126                # block 126-230 (105 registers)
REG_MODE = 231

ALLOWED_SINGLE = {REG_MAX_SOC, REG_RESERVE_ON, REG_EXCESS, REG_MODE}
ALLOWED_BLOCK = {REG_RESERVE_BLOCK: 4, REG_SCHEDULE: 105}
RESERVE_CONST = (100, 2)          # regs 120 and 122 — stable across every capture

MODES = {"auto": 1, "semi": 2, "manual": 3}
MODES_INV = {v: k for k, v in MODES.items()}

# schedule layout within the 105-register block (offsets from reg 126)
CAT_TIME = {"grid_charge": 0, "pv_charge": 28, "discharge": 56}
CAT_POWER = {"grid_charge": 84, "pv_charge": 91, "discharge": 98}
DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DEVSN = bytes(10)                  # zeros in the request; the daemon fills the serial


# --- framing -----------------------------------------------------------------
def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def frame06(reg: int, val: int) -> bytes:
    if reg not in ALLOWED_SINGLE:
        raise ValueError(f"register {reg} not in single-write allowlist")
    if not 0 <= val <= 0xFFFF:
        raise ValueError(f"value {val} out of range")
    body = b"\x01\x06" + DEVSN + struct.pack("<HH", reg, val)
    return body + struct.pack("<H", crc16(body))


def frame16(reg: int, values: list[int]) -> bytes:
    if ALLOWED_BLOCK.get(reg) != len(values):
        raise ValueError(f"block {reg} x{len(values)} not allowed")
    payload = b"".join(struct.pack("<H", v & 0xFFFF) for v in values)
    body = (b"\x01\x10" + DEVSN + struct.pack("<HH", reg, len(values))
            + bytes([len(payload)]) + payload)
    return body + struct.pack("<H", crc16(body))


def frame03(reg: int, count: int) -> bytes:      # read request (read-only)
    body = b"\x01\x03" + DEVSN + struct.pack("<HH", reg, count)
    return body + struct.pack("<H", crc16(body))


# --- encoders ----------------------------------------------------------------
def enc_time(hour: int, minute: int) -> int:
    return (minute << 8) | hour                  # register = min*256 + hour


def dec_time(v: int) -> str:
    return f"{v & 0xFF:02d}:{v >> 8:02d}"


def enc_power(win1: int, win2: int) -> int:
    return (win2 << 8) | win1


# --- semantic builders -------------------------------------------------------
def build_mode(name: str) -> bytes:
    return frame06(REG_MODE, MODES[name])


def build_max_soc(pct: int) -> bytes:
    return frame06(REG_MAX_SOC, pct)


def build_excess(pct: int) -> bytes:
    return frame06(REG_EXCESS, pct)


def build_reserve_on(pct: int) -> bytes:
    return frame06(REG_RESERVE_ON, pct)


def build_reserve_block(off_grid: int, excess: int) -> bytes:
    # regs 120-123: [100, reserve_off, 2, excess] — write whole so neither the
    # off-grid reserve nor the excess-gen (which share this block) is clobbered.
    return frame16(REG_RESERVE_BLOCK, [RESERVE_CONST[0], off_grid, RESERVE_CONST[1], excess])


def build_schedule(windows: list[dict]) -> bytes:
    """windows: [{day,cat,win(0/1),sh,sm,eh,em,power}]. Full overwrite of 126-230."""
    times = [0] * 84
    powers_win: dict[tuple, list[int]] = {}
    for w in windows:
        d = DAY_ORDER.index(w["day"])
        base = CAT_TIME[w["cat"]] + d * 4 + w["win"] * 2
        times[base] = enc_time(w["sh"], w["sm"])
        times[base + 1] = enc_time(w["eh"], w["em"])
        powers_win.setdefault((w["cat"], d), [100, 100])[w["win"]] = w["power"]
    powers = []
    for cat in ("grid_charge", "pv_charge", "discharge"):
        for d in range(7):
            w1, w2 = powers_win.get((cat, d), [100, 100])
            powers.append(enc_power(w1, w2))
    return frame16(REG_SCHEDULE, times + powers)


# --- canonical JSON schedule <-> windows -------------------------------------
def schedule_json_to_windows(sched: dict) -> list[dict]:
    out = []
    for day, cats in (sched or {}).items():
        for cat, wins in (cats or {}).items():
            for i, wd in enumerate((wins or [])[:2]):
                sh, sm = map(int, wd["start"].split(":"))
                eh, em = map(int, wd["end"].split(":"))
                out.append(dict(day=day, cat=cat, win=i,
                                sh=sh, sm=sm, eh=eh, em=em, power=wd["power"]))
    return out


def schedule_registers_to_json(block: list[int]) -> dict:
    """block = the 105 register values (126-230) -> canonical schedule JSON."""
    times, powers = block[:84], block[84:]
    sched: dict = {}
    for cat, tb in CAT_TIME.items():
        pb = CAT_POWER[cat] - 84
        for di, day in enumerate(DAY_ORDER):
            wins = []
            for wi in range(2):
                s = times[tb + di * 4 + wi * 2]
                e = times[tb + di * 4 + wi * 2 + 1]
                if s != e:                          # active window
                    pw = powers[pb + di]
                    p = (pw & 0xFF) if wi == 0 else (pw >> 8)
                    wins.append({"start": dec_time(s), "end": dec_time(e), "power": p})
            if wins:
                sched.setdefault(day, {})[cat] = wins
    return sched


# --- MQTT transport (self-contained; runs off the event loop via executor) ---
def mqtt_payload(frame: bytes, seq: int = 1) -> bytes:
    return bytes([0x30 + (seq % 10), 0x02]) + frame   # observed wire format


def _ms(b: bytes) -> bytes:
    return struct.pack("!H", len(b)) + b


def _rlen(n: int) -> bytes:
    out = b""
    while True:
        d = n % 128
        n //= 128
        out += bytes([d | (0x80 if n else 0)])
        if not n:
            return out


@dataclass
class BrokerConfig:
    host: str
    port: int
    username: str
    password: str
    serial: str                    # MQTT topic serial (Enertek/<serial>/...)
    client_id: str = "openhomepower-ha"


class MqttControl:
    """Blocking MQTT publish + request/response read. Call via executor."""

    def __init__(self, cfg: BrokerConfig):
        self.cfg = cfg
        self._t_in = f"Enertek/{cfg.serial}/DataTransmission/Input"
        self._t_out = f"Enertek/{cfg.serial}/DataTransmission/Output"

    def _connect(self) -> socket.socket:
        s = socket.create_connection((self.cfg.host, self.cfg.port), timeout=10)
        s.settimeout(3)
        body = (_ms(b"MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 30)
                + _ms(self.cfg.client_id.encode())
                + _ms(self.cfg.username.encode()) + _ms(self.cfg.password.encode()))
        s.send(bytes([0x10]) + _rlen(len(body)) + body)
        time.sleep(0.4)
        ca = s.recv(4)
        if not (len(ca) >= 4 and ca[0] == 0x20 and ca[3] == 0):
            s.close()
            raise ConnectionError(f"MQTT CONNACK failed: {ca!r}")
        return s

    def publish(self, frame: bytes) -> None:
        s = self._connect()
        try:
            pub = _ms(self._t_in.encode()) + mqtt_payload(frame)
            s.send(bytes([0x30]) + _rlen(len(pub)) + pub)
            time.sleep(0.4)
        finally:
            s.close()

    def publish_many(self, frames: list[bytes], gap: float = 1.5) -> None:
        for i, f in enumerate(frames):
            self.publish(f)
            if i < len(frames) - 1:
                time.sleep(gap)

    def read(self, reg: int, count: int, timeout: int = 15) -> list[int]:
        s = self._connect()
        try:
            sub = struct.pack("!H", 1) + _ms(self._t_out.encode()) + bytes([0])
            s.send(bytes([0x82]) + _rlen(len(sub)) + sub)
            time.sleep(0.3)
            s.recv(5)
            pub = _ms(self._t_in.encode()) + mqtt_payload(frame03(reg, count), seq=2)
            s.send(bytes([0x30]) + _rlen(len(pub)) + pub)
            buf = b""
            end = time.time() + timeout
            last = time.time()
            while time.time() < end:
                if time.time() - last > 15:
                    s.send(b"\xc0\x00")
                    last = time.time()
                try:
                    data = s.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf += data
                topic, payload, buf = _next_publish(buf)
                if topic is None:
                    continue
                if not topic.endswith("/Output"):
                    continue
                fr = payload[2:] if len(payload) > 2 and payload[1] == 0x02 else payload
                if len(fr) < 15 or fr[1] != 0x03:
                    continue
                if struct.unpack("<H", fr[12:14])[0] != reg:
                    continue
                nb = fr[14]
                d = fr[15:15 + nb]
                return [struct.unpack("<H", d[i:i + 2])[0] for i in range(0, len(d), 2)]
            raise TimeoutError(f"no reply reading reg {reg}")
        finally:
            s.close()


def _next_publish(buf: bytes):
    """Parse one MQTT PUBLISH out of buf; return (topic, payload, remaining)."""
    while len(buf) >= 2:
        mult, val, i, ok = 1, 0, 1, False
        while i < len(buf):
            b = buf[i]
            val += (b & 127) * mult
            mult *= 128
            i += 1
            if not b & 128:
                ok = True
                break
        if not ok or len(buf) < i + val:
            return None, None, buf
        if (buf[0] >> 4) == 3:
            qos = (buf[0] >> 1) & 3
            pkt = buf[i:i + val]
            tlen = struct.unpack("!H", pkt[0:2])[0]
            topic = pkt[2:2 + tlen].decode("utf-8", "replace")
            off = 2 + tlen + (2 if qos else 0)
            return topic, pkt[off:], buf[i + val:]
        buf = buf[i + val:]
    return None, None, buf
