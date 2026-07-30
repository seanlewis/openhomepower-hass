"""Control frame builders, verified byte-for-byte against captured vendor frames.

Imports control.py in isolation so these run without Home Assistant.
"""
import importlib.util
import pathlib
import sys

_PATH = (pathlib.Path(__file__).resolve().parents[1]
         / "custom_components" / "openhomepower" / "control.py")
_spec = importlib.util.spec_from_file_location("ohp_control", _PATH)
control = importlib.util.module_from_spec(_spec)
sys.modules["ohp_control"] = control        # so @dataclass can resolve annotations
_spec.loader.exec_module(control)


# Real frames captured from the vendor's own app writes.
CAPTURED = {
    "mode_auto": (lambda: control.build_mode("auto"),
                  "010600000000000000000000e7000100ce46"),
    "mode_semi": (lambda: control.build_mode("semi"),
                  "010600000000000000000000e7000200ceb6"),
    "excess_40": (lambda: control.build_excess(40),
                  "0106000000000000000000007b002800ff86"),
    "reserve_on_8": (lambda: control.build_reserve_on(8),
                     "01060000000000000000000069000800e33e"),
    "reserve_block": (lambda: control.build_reserve_block(10, 40),
                      "011000000000000000000000780004000864000a000200280025ce"),
}


def test_frames_match_vendor():
    for name, (build, expected) in CAPTURED.items():
        assert build().hex() == expected, name


def test_time_and_power_encoding():
    assert control.enc_time(3, 20) == 5123
    assert control.enc_time(15, 1) == 271
    assert control.dec_time(5123) == "03:20"
    assert control.enc_power(95, 85) == 0x555F


def test_schedule_places_registers():
    frame = control.build_schedule([{"day": "wed", "cat": "grid_charge", "win": 0,
                                     "sh": 5, "sm": 20, "eh": 6, "em": 3, "power": 47}])
    payload = frame[17:17 + frame[16]]
    regs = [int.from_bytes(payload[i:i + 2], "little") for i in range(0, len(payload), 2)]
    assert regs[8] == 5125 and regs[9] == 774   # reg 134/135 = 05:20 / 06:03
    assert regs[86] == 25647                     # reg 212 = 47% | 100%


def test_schedule_json_round_trip():
    j = {
        "mon": {"grid_charge": [{"start": "02:00", "end": "05:00", "power": 100}],
                "discharge": [{"start": "17:00", "end": "21:00", "power": 90}]},
        "sat": {"pv_charge": [{"start": "09:00", "end": "15:00", "power": 80}]},
    }
    frame = control.build_schedule(control.schedule_json_to_windows(j))
    payload = frame[17:17 + frame[16]]
    regs = [int.from_bytes(payload[i:i + 2], "little") for i in range(0, len(payload), 2)]
    assert control.schedule_registers_to_json(regs) == j


def test_allowlist_rejects_unknown_register():
    import pytest
    with pytest.raises(ValueError):
        control.frame06(200, 1)          # not an allowed single-write register
