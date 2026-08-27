"""Guards for integration-wide constants."""
from openhomepower.const import DEFAULT_READ_SOURCE, READ_SOURCE_MQTT


def test_default_read_source_is_mqtt():
    # MQTT is the universal path (works on units whose daemon doesn't log to
    # disk); new installs default to it.
    assert DEFAULT_READ_SOURCE == READ_SOURCE_MQTT
