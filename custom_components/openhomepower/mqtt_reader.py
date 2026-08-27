"""Read Homepower telemetry off an MQTT broker.

The gateway daemon publishes its input-register frames to the broker unprompted
(`Enertek/<serial>/Realtime` and `.../Read_All_Input_Registers/Output`). This
module subscribes, pulls the `0104` frames out of the payloads, and hands them
to a callback — the same frames the SSH path scrapes from the log, so the shared
decoder in protocol.py/registry.py does the rest.

READ-ONLY: this module only subscribes; it never publishes.
"""
from __future__ import annotations

from .protocol import Frame, FrameError, parse_frame


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
