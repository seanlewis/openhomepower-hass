"""Control frame builders, verified byte-for-byte against captured vendor frames.

Imports control.py in isolation so these run without Home Assistant.
"""
import importlib.util
import pathlib
import struct
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


def _fn03(start, values):
    # synthetic device serial — the parser ignores devsn content, so this keeps
    # a real serial out of the source (same reason the fixtures are scrubbed).
    body = (b"\x01\x03" + b"0000000000" + struct.pack("<H", start)
            + bytes([len(values) * 2]) + b"".join(struct.pack("<H", v) for v in values))
    return (body + struct.pack("<H", control.crc16(body))).hex()


def test_parse_holding_single_register():
    assert control.parse_holding_frames([_fn03(231, [1])]).get(231) == 1


def test_parse_holding_multi_and_state():
    regs = control.parse_holding_frames([_fn03(67, [90]), _fn03(120, [100, 8, 2, 40])])
    assert regs[67] == 90 and regs[121] == 8 and regs[123] == 40
    state = control.control_state_from_regs({**regs, 231: 2, 105: 5})
    assert state == {"mode": "semi", "max_soc": 90, "reserve_on": 5,
                     "reserve_off": 8, "excess": 40}


def test_parse_holding_rejects_bad_crc():
    bad = _fn03(231, [1])[:-4] + "0000"          # corrupt the CRC
    assert control.parse_holding_frames([bad]) == {}


def test_read_config_assembles_registers(monkeypatch):
    cfg = control.BrokerConfig(host="h", port=1, username="u", password="p",
                               serial="0000000000")
    mc = control.MqttControl(cfg)
    calls = []

    def fake_read(reg, count, timeout=15):
        calls.append((reg, count))
        return {67: [90], 105: [5], 120: [100, 8, 2, 40], 231: [2]}[reg]

    monkeypatch.setattr(mc, "read", fake_read)
    regs = mc.read_config()

    assert regs == {67: 90, 105: 5, 120: 100, 121: 8, 122: 2, 123: 40, 231: 2}
    assert calls == [(67, 1), (105, 1), (120, 4), (231, 1)]   # minimal fn-03 reads
    # and it decodes to the control state the entities show
    assert control.control_state_from_regs(regs) == {
        "mode": "semi", "max_soc": 90, "reserve_on": 5,
        "reserve_off": 8, "excess": 40}


# --- schedule read-back (Schedule sensor) ------------------------------------
_SCHED = {
    "mon": {"grid_charge": [{"start": "02:00", "end": "05:00", "power": 100}],
            "discharge": [{"start": "17:00", "end": "21:00", "power": 90}]},
    "sat": {"pv_charge": [{"start": "09:00", "end": "12:00", "power": 80},
                          {"start": "13:00", "end": "15:00", "power": 60}]},
}


def _mqtt_with_reads(monkeypatch, replies):
    cfg = control.BrokerConfig(host="h", port=1, username="u", password="p",
                               serial="0000000000")
    mc = control.MqttControl(cfg)
    calls = []

    def fake_read(reg, count, timeout=15):
        calls.append((reg, count))
        return replies[reg]

    monkeypatch.setattr(mc, "read", fake_read)
    return mc, calls


def test_schedule_block_from_frame_matches_payload():
    frame = control.build_schedule(control.schedule_json_to_windows(_SCHED))
    block = control.schedule_block_from_frame(frame)
    assert len(block) == 105
    assert control.schedule_registers_to_json(block) == _SCHED


def test_schedule_block_normalises_extra_windows():
    # A third window can't be stored; the read-back shows what the battery holds.
    three = {"tue": {"discharge": [{"start": f"{h:02d}:00", "end": f"{h:02d}:30",
                                    "power": 100} for h in (6, 12, 18)]}}
    frame = control.build_schedule(control.schedule_json_to_windows(three))
    held = control.schedule_registers_to_json(control.schedule_block_from_frame(frame))
    assert [w["start"] for w in held["tue"]["discharge"]] == ["06:00", "12:00"]


def test_read_schedule_decodes_block(monkeypatch):
    block = control.schedule_block_from_frame(
        control.build_schedule(control.schedule_json_to_windows(_SCHED)))
    mc, calls = _mqtt_with_reads(monkeypatch, {126: block})
    assert mc.read_schedule() == _SCHED
    assert calls == [(126, 105)]              # one fn-03 read of the whole block


def test_read_schedule_empty_block(monkeypatch):
    # Live read from a unit with no schedule set (2026-09-27): zeroed times,
    # 100%/100% powers.
    mc, _ = _mqtt_with_reads(monkeypatch, {126: [0] * 84 + [0x6464] * 21})
    assert mc.read_schedule() == {}


def test_read_schedule_rejects_short_reply(monkeypatch):
    import pytest
    mc, _ = _mqtt_with_reads(monkeypatch, {126: [0] * 60})
    with pytest.raises(ValueError):
        mc.read_schedule()


def test_schedule_summary():
    assert control.schedule_summary({}) == "Empty"
    assert control.schedule_summary(_SCHED) == "2 days, 4 windows"
    one = {"sun": {"discharge": [{"start": "17:00", "end": "21:00", "power": 100}]}}
    assert control.schedule_summary(one) == "1 day, 1 window"
