"""Sensor entities, generated from the vendored register map."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import clock, control
from .clock_tracker import ClockTracker
from .const import (
    CONF_LOCAL_DAILY_RESET,
    DEFAULT_LOCAL_DAILY_RESET,
    DIAGNOSTIC_KEYS,
    DOMAIN,
    EXCLUDED_KEYS,
    MANUFACTURER,
    MODEL,
)
from .control_coordinator import ControlCoordinator, control_device_info
from .coordinator import HomepowerCoordinator
from .daily_reset import DailyAccumulator
from .entity_spec import HomepowerSensorSpec, build_specs

# Only map classes Home Assistant actually defines; anything else is left unset
# rather than guessed, which would make HA reject the entity.
DEVICE_CLASSES = {
    "battery": SensorDeviceClass.BATTERY,
    "power": SensorDeviceClass.POWER,
    "energy": SensorDeviceClass.ENERGY,
    "voltage": SensorDeviceClass.VOLTAGE,
    "current": SensorDeviceClass.CURRENT,
    "frequency": SensorDeviceClass.FREQUENCY,
    "temperature": SensorDeviceClass.TEMPERATURE,
}
STATE_CLASSES = {
    "measurement": SensorStateClass.MEASUREMENT,
    "total": SensorStateClass.TOTAL,
    "total_increasing": SensorStateClass.TOTAL_INCREASING,
}


def _is_local_daily(spec: HomepowerSensorSpec) -> bool:
    """The device's own "...today" energy counters — the ones it zeroes on its
    DST-less clock. These are what we re-bucket to HA's local midnight."""
    return spec.device_class == "energy" and spec.state_class == "total_increasing"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    """Set up the sensors."""
    coordinator: HomepowerCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    specs = build_specs(coordinator.regmap)
    local_daily = entry.options.get(CONF_LOCAL_DAILY_RESET, DEFAULT_LOCAL_DAILY_RESET)
    async_add_entities(
        HomepowerLocalDailySensor(coordinator, entry, spec)
        if local_daily and _is_local_daily(spec)
        else HomepowerSensor(coordinator, entry, spec)
        for spec in specs
    )
    tracker: ClockTracker | None = hass.data[DOMAIN][entry.entry_id].get("clock")
    if tracker is not None:
        async_add_entities([HomepowerClockOffsetSensor(coordinator, entry, tracker)])
    control_coordinator: ControlCoordinator | None = (
        hass.data[DOMAIN][entry.entry_id].get("control"))
    if control_coordinator is not None:      # control enabled
        async_add_entities([HomepowerScheduleSensor(control_coordinator, entry)])


class HomepowerSensor(CoordinatorEntity[HomepowerCoordinator], SensorEntity):
    """One decoded register, exposed as a sensor."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HomepowerCoordinator, entry: ConfigEntry,
                 spec: HomepowerSensorSpec) -> None:
        super().__init__(coordinator)
        self._spec = spec
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_{spec.key}"
        self.entity_description = SensorEntityDescription(
            key=spec.key,
            name=spec.name,
            native_unit_of_measurement=spec.unit,
            device_class=DEVICE_CLASSES.get(spec.device_class or ""),
            state_class=STATE_CLASSES.get(spec.state_class or ""),
            entity_category=EntityCategory.DIAGNOSTIC if spec.diagnostic else None,
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            name="Energizer Homepower",
            manufacturer=MANUFACTURER,
            model=MODEL,
            configuration_url=f"http://{entry.data.get('host')}/",
        )

    @property
    def native_value(self):
        reading = self.coordinator.data.get(self._spec.key)
        if reading is None:
            return None
        # Enum-style registers read better as their label.
        return reading.enum_label if reading.enum_label else reading.value

    @property
    def extra_state_attributes(self) -> dict[str, str | int | None]:
        """Expose provenance so an unverified value is never mistaken for fact."""
        reading = self.coordinator.data.get(self._spec.key)
        return {
            "confidence": self._spec.confidence,
            "register": reading.register if reading else None,
            # Non-null and growing means we are serving the last good reading
            # through a WiFi drop rather than a fresh one.
            "reading_age_seconds": self.coordinator.reading_age,
        }

    @property
    def available(self) -> bool:
        return super().available and self._spec.key in (self.coordinator.data or {})


@dataclass
class _DailyRestoreData(ExtraStoredData):
    """Persists the running total and the last raw reading across restarts, so a
    restart neither zeroes the day nor double-counts the step across the gap."""

    total: float
    last_source: float | None

    def as_dict(self) -> dict:
        return {"total": self.total, "last_source": self.last_source}


class HomepowerLocalDailySensor(CoordinatorEntity[HomepowerCoordinator],
                                RestoreEntity, SensorEntity):
    """A daily energy counter re-bucketed to Home Assistant's local midnight.

    The battery resets its own "...today" counters on a clock that ignores
    daylight saving, so they roll over an hour late through NZ summer. This wraps
    one such counter and resets at HA's own local midnight instead, which stays
    correct across daylight-saving changes. It sums the counter's increases, so
    the device's late reset becomes a harmless mid-day meter reset. Keyed exactly
    like the raw sensor it replaces (same unique id and name), so turning the
    option off swaps it back with no entity churn.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: HomepowerCoordinator, entry: ConfigEntry,
                 spec: HomepowerSensorSpec) -> None:
        super().__init__(coordinator)
        self._spec = spec
        self._acc = DailyAccumulator()
        self._unsub_midnight = None
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_{spec.key}"
        self.entity_description = SensorEntityDescription(
            key=spec.key,
            name=spec.name,
            native_unit_of_measurement=spec.unit,
            device_class=DEVICE_CLASSES.get(spec.device_class or ""),
            state_class=STATE_CLASSES.get(spec.state_class or ""),
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            name="Energizer Homepower",
            manufacturer=MANUFACTURER,
            model=MODEL,
            configuration_url=f"http://{entry.data.get('host')}/",
        )

    def _source(self) -> float | None:
        reading = self.coordinator.data.get(self._spec.key)
        return None if reading is None else reading.value

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Restore the running total and last raw reading from before the restart.
        extra = await self.async_get_last_extra_data()
        if extra is not None:
            data = extra.as_dict()
            self._acc.total = float(data.get("total") or 0.0)
            last = data.get("last_source")
            self._acc.last_source = None if last is None else float(last)
        # Reset at HA's own local midnight, which is daylight-saving aware.
        self._unsub_midnight = async_track_time_change(
            self.hass, self._reset_daily, hour=0, minute=0, second=0)
        self.async_on_remove(self._cancel_midnight)
        # Seed from whatever the coordinator already holds so the first real
        # update measures a step rather than counting the baseline as energy.
        self._acc.update(self._source())

    def _cancel_midnight(self) -> None:
        if self._unsub_midnight is not None:
            self._unsub_midnight()
            self._unsub_midnight = None

    @callback
    def _reset_daily(self, now) -> None:
        self._acc.reset()
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._acc.update(self._source())
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float | None:
        if self._acc.last_source is None:
            return None
        # The kWh counters carry two decimals; keep that and lose float dust.
        return round(self._acc.total, 2)

    @property
    def extra_state_attributes(self) -> dict[str, float | str | None]:
        return {
            "reset": "local midnight",
            # The raw device counter, for anyone comparing against the app.
            "device_counter": self._source(),
            "reading_age_seconds": self.coordinator.reading_age,
        }

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        return _DailyRestoreData(self._acc.total, self._acc.last_source)

    @property
    def available(self) -> bool:
        return super().available and self._acc.last_source is not None


class HomepowerScheduleSensor(CoordinatorEntity[ControlCoordinator], SensorEntity):
    """The weekly schedule stored on the battery (opt-in control).

    State is a short summary; the full schedule is in the `schedule` attribute,
    in exactly the format `openhomepower.set_schedule` takes, so it can be
    copied, edited and sent back.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "schedule"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: ControlCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_schedule"
        self._attr_device_info = control_device_info(entry)

    @property
    def _schedule(self) -> dict | None:
        return (self.coordinator.data or {}).get("schedule")

    @property
    def native_value(self) -> str | None:
        sched = self._schedule
        return None if sched is None else control.schedule_summary(sched)

    @property
    def extra_state_attributes(self) -> dict:
        mode = (self.coordinator.data or {}).get("mode")
        data = self.coordinator.data or {}
        return {
            "schedule": self._schedule,
            # The battery only follows the schedule in Manual mode.
            "active": mode == "manual" if mode is not None else None,
            # Real time unless real-time schedules are off; the battery holds
            # `battery_schedule`, shifted by its clock's offset.
            "times": "real" if self.coordinator.realtime else "battery clock",
            "battery_schedule": data.get("battery_schedule"),
            "clock_offset_minutes": self.coordinator.applied,
        }

    @property
    def available(self) -> bool:
        return super().available and self._schedule is not None


class HomepowerClockOffsetSensor(CoordinatorEntity[HomepowerCoordinator], SensorEntity):
    """How far the battery's own clock is from real time, in minutes.

    Negative = behind. The battery runs its schedule by this clock, which never
    adjusts for daylight saving or drift.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "clock_offset"
    _attr_icon = "mdi:clock-alert-outline"
    _attr_native_unit_of_measurement = "min"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HomepowerCoordinator, entry: ConfigEntry,
                 tracker: ClockTracker) -> None:
        super().__init__(coordinator)
        self._tracker = tracker
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_clock_offset"
        self._attr_device_info = control_device_info(entry)

    @property
    def native_value(self) -> int | None:
        return self._tracker.offset()

    @property
    def extra_state_attributes(self) -> dict:
        return {"description": clock.describe(self._tracker.offset()),
                "time_zone": str(self._tracker.tz)}

    @property
    def available(self) -> bool:
        return super().available and self._tracker.offset() is not None
