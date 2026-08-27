"""MQTT telemetry extraction, verified without Home Assistant (see conftest)."""
import importlib.util
import pathlib
import struct
import sys

_DIR = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "openhomepower"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"ohp_{name}", _DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"ohp_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


protocol = _load("protocol")
control = _load("control")
mqtt_reader = _load("mqtt_reader")


def make_frame(start: int, regs: list[int], devsn: bytes = b"0000000000") -> bytes:
    """Build a CRC-valid function-04 response frame (secret-free devsn)."""
    body = (b"\x01\x04" + devsn + struct.pack("<H", start)
            + bytes([len(regs) * 2])
            + b"".join(struct.pack("<H", r) for r in regs))
    return body + struct.pack("<H", protocol.crc16(body))


def test_telemetry_frame_extracts_from_wrapped_payload():
    frame = make_frame(0, [70, 3520, 118])          # SOC-ish/power-ish sample
    payload = control.mqtt_payload(frame, seq=3)     # [0x33][0x02] + frame
    out = mqtt_reader.telemetry_frame(payload)
    assert out is not None
    assert out.start == 0
    assert out.registers == (70, 3520, 118)


def test_telemetry_frame_rejects_non_frame_payload():
    assert mqtt_reader.telemetry_frame(b"\x33\x02hello") is None       # not a frame
    assert mqtt_reader.telemetry_frame(b"") is None                    # empty
    bad = bytearray(control.mqtt_payload(make_frame(0, [1, 2]), 1))
    bad[-1] ^= 0xFF                                                     # corrupt CRC
    assert mqtt_reader.telemetry_frame(bytes(bad)) is None
