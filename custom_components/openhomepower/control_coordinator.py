"""Coordinator for the control entities.

Reads the writable config (mode / max-SoC / reserve / excess) and the weekly
schedule with fn-03 reads over MQTT, and writes over MQTT (`self.mqtt`) — for
SSH entries too. The gateway's log only holds what the daemon last read: reserve
/ max-SoC there can be an hour old, and the schedule is never in it, so it can't
confirm a write. Config changes rarely, so this polls slowly
(CONTROL_SCAN_INTERVAL).

Real-time schedules: the battery follows its schedule by its own clock, which
never adjusts for daylight saving or drift. When `realtime` is on, the schedule
is shown and entered in real time and shifted by the battery clock's offset on
the way in and out. `applied` is the offset the stored schedule was written
with (persisted); when the measured offset moves CLOCK_REWRITE_MINUTES or more
from it, on two polls in a row, the schedule is re-written to stay on real time.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import replace

from homeassistant.config_entries import ConfigEntry
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import clock, control
from .clock_tracker import ClockTracker
from .const import (
    CLOCK_REWRITE_MINUTES,
    CONTROL_SCAN_INTERVAL,
    DOMAIN,
    MANUFACTURER,
    MODEL,
)
from .control import MqttControl
from .timeshift import shift_schedule

_LOGGER = logging.getLogger(__name__)


def control_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Same device the sensors attach to, so control lands on the one card."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        name="Energizer Homepower",
        manufacturer=MANUFACTURER,
        model=MODEL,
    )


class MqttConfigReader:
    """Read control config over MQTT (fn-03 request/response on the broker)."""

    def __init__(self, hass: HomeAssistant, mqtt: MqttControl) -> None:
        self._hass = hass
        self._mqtt = mqtt

    async def read_regs(self) -> dict[int, int]:
        return await self._hass.async_add_executor_job(self._mqtt.read_config)


class ClockUnknown(Exception):
    """The battery clock's offset isn't known yet, so times can't be converted."""


class ScheduleDoesNotFit(Exception):
    """After shifting to battery time, some day needs more than two windows."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__(", ".join(problems))
        self.problems = problems


class ControlCoordinator(DataUpdateCoordinator[dict]):
    """Reads config and writes it, both over MQTT."""

    def __init__(self, hass: HomeAssistant,
                 reader: MqttConfigReader,
                 mqtt: MqttControl, entry_id: str = "",
                 clock_tracker: ClockTracker | None = None,
                 realtime: bool = False) -> None:
        super().__init__(
            hass, _LOGGER, name="OpenHomepower control",
            update_interval=CONTROL_SCAN_INTERVAL,
        )
        self._reader = reader
        self.mqtt = mqtt
        self._entry_id = entry_id
        self.clock = clock_tracker
        self.realtime = realtime and clock_tracker is not None
        self._store: Store = Store(hass, 1, f"{DOMAIN}.{entry_id}.clock")
        # Offset (minutes) the battery's schedule was written with; None until
        # known. On first run we can't know what an existing schedule assumed
        # (the owner may already have shifted it by hand), so we never write on
        # first run: we adopt the current offset, which shows the schedule at
        # the real times it actually runs, and ask the owner to check it.
        self.applied: int | None = None
        self._pending: int | None = None
        # Own client-id, so a schedule read can never evict (or be evicted by)
        # a concurrent write or config read.
        self.schedule_reader = MqttControl(
            replace(mqtt.cfg, client_id=f"openhomepower-ha-sched-{mqtt.cfg.serial}"))

    async def _async_update_data(self) -> dict:
        try:
            regs = await self._reader.read_regs()
        except (OSError, IndexError, struct.error) as err:
            # OSError covers the MQTT reader's TimeoutError / ConnectionError
            # (both OSError subclasses). IndexError/struct.error catch a
            # malformed or short frame (e.g. a nb=0 fn-03 reply, or a truncated
            # read).
            raise UpdateFailed(f"control read failed: {err}") from err
        # Keep the last-known value for any register not in this batch — config
        # only changes when someone writes it, so a stale-but-unchanged value is
        # still the correct value.
        state = dict(self.data or {})
        for key, value in control.control_state_from_regs(regs).items():
            if value is not None:
                state[key] = value
        # Best-effort: a unit that won't answer the schedule read leaves just the
        # schedule sensor unavailable (or on its last-known value), never the
        # mode / reserve entities.
        try:
            raw = await self.hass.async_add_executor_job(
                self.schedule_reader.read_schedule)
        except (OSError, ValueError, IndexError, struct.error) as err:
            _LOGGER.debug("schedule read failed: %s", err)
        else:
            raw = await self._follow_clock(raw)
            state["battery_schedule"] = raw
            state["schedule"] = self.to_real(raw)
        return state

    # -- real time <-> battery time -----------------------------------------------
    async def async_load(self) -> None:
        stored = await self._store.async_load()
        if stored is not None and stored.get("applied") is not None:
            self.applied = int(stored["applied"])

    async def _save_applied(self, minutes: int) -> None:
        self.applied = int(minutes)
        await self._store.async_save({"applied": self.applied})

    def to_real(self, battery_sched: dict) -> dict:
        """Battery-time schedule -> what people see (real time when enabled)."""
        if not self.realtime or not self.applied:        # None or 0: as stored
            return battery_sched
        real, _ = shift_schedule(battery_sched, -self.applied)
        return real

    def prepare_write(self, real_sched: dict) -> tuple[dict, int]:
        """Real-time schedule -> (battery-time schedule, offset used).

        Raises ClockUnknown if the offset isn't measured yet, and
        ScheduleDoesNotFit if shifting needs more than two windows on a day.
        """
        if not self.realtime:
            return real_sched, 0
        offset = self.clock.offset()
        if offset is None:
            raise ClockUnknown
        battery, problems = shift_schedule(real_sched, offset)
        if problems:
            raise ScheduleDoesNotFit(problems)
        return battery, offset

    async def async_written(self, battery_sched: dict, offset: int) -> None:
        """Record a schedule write: offset used, optimistic state, confirm."""
        if self.realtime and offset != self.applied:
            await self._save_applied(offset)
        if self.data is not None:
            self.async_set_updated_data({
                **self.data, "battery_schedule": battery_sched,
                "schedule": self.to_real(battery_sched)})
        await self.async_request_refresh()

    async def _follow_clock(self, raw: dict) -> dict:
        """Re-write the schedule if the battery clock's offset has moved.

        Needs the same new offset on two polls in a row, so one odd reading
        never triggers a write. Returns the schedule now on the battery.
        """
        if not self.realtime:
            return raw
        offset = self.clock.offset()
        if offset is not None and self.applied is None:
            await self._save_applied(offset)          # first run: adopt, never write
            if abs(offset) >= CLOCK_REWRITE_MINUTES and raw:
                self._notify(
                    "Check your battery schedule",
                    f"The battery's clock is {clock.describe(offset)}. Schedule times "
                    "are now shown in real time, allowing for that, so check the "
                    "Schedule sensor (or Configure → Schedule) shows the times you "
                    "want. From now on Home Assistant keeps the schedule on real time, "
                    "including after daylight-saving changes.")
            return raw
        if offset is None or abs(offset - self.applied) < CLOCK_REWRITE_MINUTES:
            self._pending = None
            return raw
        if self._pending is None or abs(self._pending - offset) > 1:
            self._pending = offset
            return raw
        self._pending = None
        if not raw:                           # nothing to move; just note the clock
            await self._save_applied(offset)
            return raw
        real = self.to_real(raw)
        new_raw, problems = shift_schedule(real, offset)
        if problems:
            self._notify(
                "Couldn't adjust the battery schedule",
                f"The battery's clock is now {clock.describe(offset)}, but moving the "
                f"schedule to match would need more than two windows on: "
                f"{', '.join(problems)}. The schedule hasn't been changed, so it now "
                "runs at the wrong real times. Adjust it under Configure → Schedule.")
            return raw
        frame = control.build_schedule(control.schedule_json_to_windows(new_raw))
        try:
            await self.hass.async_add_executor_job(self.mqtt.publish, frame)
        except OSError as err:
            _LOGGER.warning("clock adjustment: write failed, will retry: %s", err)
            return raw
        before = self.applied
        await self._save_applied(offset)
        _LOGGER.info("battery clock offset %s -> %s min; schedule re-written",
                     before, offset)
        self._notify(
            "Battery schedule adjusted",
            f"The battery's clock is now {clock.describe(offset)} "
            f"(it was {clock.describe(before)}), for example after a daylight-saving "
            "change. Home Assistant has re-written the battery's schedule so it still "
            "runs at the same real times.")
        return new_raw

    def _notify(self, title: str, message: str) -> None:
        persistent_notification.async_create(
            self.hass, message, title=f"OpenHomepower: {title}",
            notification_id=f"{DOMAIN}_clock_{self._entry_id}")
