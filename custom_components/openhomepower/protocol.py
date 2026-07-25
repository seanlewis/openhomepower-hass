"""Frame parsing and decoding for the Energizer Homepower.

Pure functions over bytes — no I/O, no network, no device. Everything here is
testable against captured frames, which is how it is verified.

See PROTOCOL.md for the specification this implements.

READ-ONLY: this module can parse and decode responses. It deliberately provides
no way to construct a write request.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

FUNCTION_READ_INPUT = 0x04
DEVSN_LEN = 10
HEADER_LEN = 2 + DEVSN_LEN + 2 + 1  # addr+func, devsn, start, nbytes
CRC_LEN = 2


class FrameError(ValueError):
    """A frame was malformed, truncated, or failed its CRC."""


def crc16(data: bytes) -> int:
    """CRC-16/MODBUS: init 0xFFFF, polynomial 0xA001, reflected."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


@dataclass(frozen=True)
class Frame:
    """One decoded response frame."""

    devsn: str
    start: int          # linear index of the first register in payload
    registers: tuple[int, ...]   # raw u16 little-endian values

    @property
    def end(self) -> int:
        return self.start + len(self.registers)

    def as_map(self) -> dict[int, int]:
        return {self.start + i: v for i, v in enumerate(self.registers)}


def parse_frame(raw: bytes | str) -> Frame:
    """Parse one response frame. Raises FrameError if it is not trustworthy.

    CRC validation is not optional: corrupt frames really do occur (observed
    while the gateway restarts) and carry plausible-looking but wrong values.
    """
    if isinstance(raw, str):
        try:
            raw = bytes.fromhex(raw.strip())
        except ValueError as exc:
            raise FrameError(f"not valid hex: {exc}") from exc

    if len(raw) < HEADER_LEN + CRC_LEN:
        raise FrameError(f"too short: {len(raw)} bytes")
    if raw[1] != FUNCTION_READ_INPUT:
        raise FrameError(f"unexpected function code 0x{raw[1]:02x}")

    nbytes = raw[14]
    expected = HEADER_LEN + nbytes + CRC_LEN
    if len(raw) != expected:
        raise FrameError(f"length mismatch: got {len(raw)}, header implies {expected}")

    if crc16(raw[:-CRC_LEN]) != int.from_bytes(raw[-CRC_LEN:], "little"):
        raise FrameError("CRC mismatch")

    if nbytes % 2:
        raise FrameError(f"odd payload length {nbytes}")

    devsn = raw[2:2 + DEVSN_LEN].decode("ascii", "replace")
    start = int.from_bytes(raw[12:14], "little")
    payload = raw[HEADER_LEN:HEADER_LEN + nbytes]
    regs = struct.unpack(f"<{nbytes // 2}H", payload)
    return Frame(devsn=devsn, start=start, registers=regs)


def iter_frames(lines) -> "list[Frame]":
    """Parse every valid response frame from log lines, skipping bad ones.

    Accepts raw log lines or bare hex tokens. Corrupt frames are dropped
    silently — that is the intended behaviour, see PROTOCOL.md §3.
    """
    out = []
    for line in lines:
        token = line.strip().split()[-1] if line.strip() else ""
        if not token.startswith("0104"):
            continue
        try:
            out.append(parse_frame(token))
        except FrameError:
            continue
    return out


def merge(frames) -> dict[int, int]:
    """Combine frames into one register map. Later frames win."""
    merged: dict[int, int] = {}
    for f in frames:
        merged.update(f.as_map())
    return merged


def as_signed(value: int) -> int:
    return value - 0x10000 if value >= 0x8000 else value


def decode_clock(regs: dict[int, int], base: int = 76) -> str | None:
    """Device clock from three registers of packed byte pairs.

    Returns an ISO-ish 'YYYY-MM-DD HH:MM:SS' string, or None if the registers
    are missing or implausible.
    """
    try:
        r0, r1, r2 = regs[base], regs[base + 1], regs[base + 2]
    except KeyError:
        return None
    year, month = r0 & 0xFF, r0 >> 8
    day, hour = r1 & 0xFF, r1 >> 8
    minute, second = r2 & 0xFF, r2 >> 8
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24
            and minute < 60 and second < 60):
        return None
    return f"20{year:02d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}"


def decode_ascii(regs: dict[int, int], indices) -> str | None:
    """ASCII text stored as register byte pairs (e.g. the device serial)."""
    out = bytearray()
    for i in indices:
        if i not in regs:
            return None
        out.append(regs[i] & 0xFF)
        out.append(regs[i] >> 8)
    text = out.decode("ascii", "replace").strip("\x00 ")
    return text or None
