"""Entity-generation tests. No Home Assistant, no hardware, no network.

These guard the mistakes that actually break a custom integration: a
device_class Home Assistant does not recognise, an energy sensor without a
total state class (the Energy Dashboard silently ignores it), or a value the
Energy Dashboard picker would render ambiguous.
"""
import pytest

from openhomepower.const import DIAGNOSTIC_KEYS, EXCLUDED_KEYS  # noqa: E402
from openhomepower.entity_spec import build_specs  # noqa: E402
from openhomepower.registry import RegisterMap  # noqa: E402

# Mirrors the mapping in sensor.py. Anything outside this set is left unset
# rather than guessed.
HA_DEVICE_CLASSES = {"battery", "power", "energy", "voltage", "current",
                     "frequency", "temperature"}
HA_STATE_CLASSES = {"measurement", "total", "total_increasing"}
HA_ENERGY_UNITS = {"Wh", "kWh", "MWh"}
HA_POWER_UNITS = {"W", "kW", "MW"}


@pytest.fixture(scope="module")
def specs():
    return build_specs(RegisterMap.load())


def test_specs_are_generated(specs):
    assert len(specs) > 15


def test_keys_are_unique(specs):
    keys = [s.key for s in specs]
    assert len(keys) == len(set(keys))


def test_device_classes_are_recognised(specs):
    for s in specs:
        if s.device_class is not None:
            assert s.device_class in HA_DEVICE_CLASSES, f"{s.key}: {s.device_class}"


def test_state_classes_are_recognised(specs):
    for s in specs:
        if s.state_class is not None:
            assert s.state_class in HA_STATE_CLASSES, f"{s.key}: {s.state_class}"


def test_energy_sensors_are_dashboard_compatible(specs):
    """device_class energy needs an energy unit and a total state class, or the
    Energy Dashboard will not offer the sensor at all."""
    energy = [s for s in specs if s.device_class == "energy"]
    assert energy, "no energy sensors — the Energy Dashboard would be unusable"
    for s in energy:
        assert s.unit in HA_ENERGY_UNITS, f"{s.key} unit {s.unit}"
        assert s.state_class in {"total", "total_increasing"}, f"{s.key} {s.state_class}"


def test_power_sensors_are_measurements(specs):
    for s in (s for s in specs if s.device_class == "power"):
        assert s.unit in HA_POWER_UNITS, f"{s.key} unit {s.unit}"
        assert s.state_class == "measurement", f"{s.key} {s.state_class}"


def test_every_sensor_with_a_unit_has_a_state_class(specs):
    """Without one, HA records no long-term statistics for the sensor."""
    for s in specs:
        if s.unit:
            assert s.state_class is not None, f"{s.key} has a unit but no state_class"


def test_excluded_keys_are_absent(specs):
    """The DC daily counters would sit beside the AC ones in the Energy
    Dashboard picker and invite choosing the wrong one."""
    keys = {s.key for s in specs}
    for excluded in EXCLUDED_KEYS:
        assert excluded not in keys


def test_diagnostic_keys_are_marked(specs):
    by_key = {s.key: s for s in specs}
    for key in DIAGNOSTIC_KEYS:
        if key in by_key:
            assert by_key[key].diagnostic, f"{key} should be diagnostic"


def test_primary_sensors_are_not_diagnostic(specs):
    by_key = {s.key: s for s in specs}
    for key in ("battery_soc_pct", "house_load_w", "grid_power_w",
                "daily_solar_kwh"):
        assert key in by_key, f"{key} missing"
        assert not by_key[key].diagnostic, f"{key} should be primary"


def test_the_energy_dashboard_essentials_exist(specs):
    keys = {s.key for s in specs}
    for key in ("daily_solar_kwh", "daily_charge_kwh", "daily_discharge_kwh",
                "daily_grid_import_kwh", "daily_grid_export_kwh"):
        assert key in keys, f"{key} missing — Energy Dashboard would be incomplete"


def test_signed_grid_power_is_present(specs):
    """The single sensor HA wants for 'Standard' grid power measurement."""
    assert "grid_power_w" in {s.key for s in specs}


def test_confidence_is_always_set(specs):
    """Unverified mappings must stay distinguishable from verified ones."""
    for s in specs:
        assert s.confidence in {"confirmed", "candidate", "derived"}, s.key


def test_names_are_human_readable(specs):
    for s in specs:
        assert s.name and not s.name.startswith("_")
        assert "_" not in s.name, f"{s.key} name looks like a raw key: {s.name}"
