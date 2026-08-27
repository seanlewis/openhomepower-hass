"""MQTT telemetry extraction, verified without Home Assistant.

Imports through the synthetic `openhomepower` package that conftest.py
registers — the same pattern test_entities.py uses — so the modules keep
plain relative imports and need no test-only fallbacks.
"""
import struct

from openhomepower.control import mqtt_payload
from openhomepower.mqtt_reader import telemetry_frame
from openhomepower.protocol import crc16


def make_frame(start: int, regs: list[int], devsn: bytes = b"0000000000") -> bytes:
    """Build a CRC-valid function-04 response frame (secret-free devsn)."""
    body = (b"\x01\x04" + devsn + struct.pack("<H", start)
            + bytes([len(regs) * 2])
            + b"".join(struct.pack("<H", r) for r in regs))
    return body + struct.pack("<H", crc16(body))


def test_telemetry_frame_extracts_from_wrapped_payload():
    frame = make_frame(0, [70, 3520, 118])          # SOC-ish/power-ish sample
    payload = mqtt_payload(frame, seq=3)             # [0x33][0x02] + frame
    out = telemetry_frame(payload)
    assert out is not None
    assert out.start == 0
    assert out.registers == (70, 3520, 118)


def test_telemetry_frame_rejects_non_frame_payload():
    assert telemetry_frame(b"\x33\x02hello") is None       # not a frame
    assert telemetry_frame(b"") is None                    # empty
    bad = bytearray(mqtt_payload(make_frame(0, [1, 2]), 1))
    bad[-1] ^= 0xFF                                         # corrupt CRC
    assert telemetry_frame(bytes(bad)) is None
