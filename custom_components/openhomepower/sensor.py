"""Sensor entities, generated from the vendored register map."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DIAGNOSTIC_KEYS, DOMAIN, EXCLUDED_KEYS, MANUFACTURER, MODEL
from .coordinator import HomepowerCoordinator
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    """Set up the sensors."""
    coordinator: HomepowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    specs = build_specs(coordinator.regmap)
    async_add_entities(
        HomepowerSensor(coordinator, entry, spec) for spec in specs
    )


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
