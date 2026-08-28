"""MQTT telemetry decode, verified without Home Assistant.

Imports through the synthetic `openhomepower` package that conftest.py
registers — the same pattern test_entities.py uses — so the modules keep
plain relative imports and need no test-only fallbacks.

The fixtures under tests/fixtures/ are REAL Read_All_Input_Registers/Output and
Realtime register dumps captured from a unit, with the device serial scrubbed to
zeros. The daemon replies to a read-all request with the full input-register bank
as a raw little-endian u16 array; these tests prove that array decodes through
the shared registry.
"""
import pathlib

from openhomepower.mqtt_reader import plausible_reading, registers_from_payload
from openhomepower.registry import RegisterMap

_FIX = pathlib.Path(__file__).parent / "fixtures"
_READ_ALL_TOPIC = "Enertek/0000000000/Read_All_Input_Registers/Output"
_REALTIME_TOPIC = "Enertek/0000000000/Realtime"


def _wrap(dump: bytes) -> bytes:
    """Prepend the daemon's `[seq][0x02]` wrapper, as seen on the wire."""
    return bytes([0x31, 0x02]) + dump


def _dump(name: str) -> bytes:
    return bytes.fromhex((_FIX / f"{name}_dump.hex").read_text())


def test_read_all_dump_decodes_to_real_readings():
    payload = _wrap(_dump("read_all"))
    regs = registers_from_payload(_READ_ALL_TOPIC, payload)
    assert regs is not None and regs[0] == 32          # register array starts at offset 0
    readings = RegisterMap.load().decode(regs)
    assert readings["battery_soc_pct"].value == 14
    assert readings["solar_pv_w"].value == 4732
    assert readings["ac_voltage_v"].value == 240.5
    assert readings["device_serial"].value == "0000000000"   # scrubbed fixture


def test_realtime_dump_decodes_via_six_byte_header_offset():
    payload = _wrap(_dump("realtime"))
    regs = registers_from_payload(_REALTIME_TOPIC, payload)
    readings = RegisterMap.load().decode(regs)
    # Same array as read-all, just behind a 6-byte header; must still decode sanely.
    assert readings["battery_soc_pct"].value == 13
    assert readings["device_serial"].value == "0000000000"


def test_realtime_offset_actually_matters():
    # Decoding the Realtime payload without the 6-byte offset (i.e. as if it were
    # a Read_All reply) must NOT produce the same sane serial — proving the
    # topic-specific offset is load-bearing, not cosmetic.
    payload = _wrap(_dump("realtime"))
    as_read_all = registers_from_payload(_READ_ALL_TOPIC, payload)   # wrong offset
    readings = RegisterMap.load().decode(as_read_all)
    assert readings.get("device_serial") is None or \
        readings["device_serial"].value != "0000000000"


def test_rejects_payloads_too_short_to_be_a_dump():
    assert registers_from_payload(_READ_ALL_TOPIC, b"") is None
    assert registers_from_payload(_READ_ALL_TOPIC, b"\x31\x02") is None
    assert registers_from_payload(_REALTIME_TOPIC, b"\x31\x02\x00\x00") is None  # header only


class _R:
    def __init__(self, value):
        self.value = value


def test_plausible_accepts_a_real_decode():
    regs = registers_from_payload(_READ_ALL_TOPIC, _wrap(_dump("read_all")))
    readings = RegisterMap.load().decode(regs)
    assert plausible_reading(readings) is True


def test_plausible_rejects_garbage_decodes():
    assert plausible_reading({"device_serial": _R("\x0005120"),
                              "battery_soc_pct": _R(14)}) is False   # non-digit serial
    assert plausible_reading({"device_serial": _R("0000000000"),
                              "battery_soc_pct": _R(8555)}) is False  # SOC out of range
    assert plausible_reading({"battery_soc_pct": _R(14)}) is False    # no serial at all
