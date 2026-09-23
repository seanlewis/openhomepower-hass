"""Guards for integration-wide constants."""
from openhomepower.const import (
    DEFAULT_READ_SOURCE, READ_SOURCE_AUTO, READ_SOURCE_MQTT, READ_SOURCE_SSH)


def test_default_read_source_is_auto():
    # New installs try SSH (fully local) and fall back to MQTT on units whose
    # daemon doesn't log to disk. "auto" is a setup-form choice only.
    assert DEFAULT_READ_SOURCE == READ_SOURCE_AUTO


def test_auto_is_distinct_from_stored_sources():
    # Entries only ever store SSH or MQTT; __init__ falls back to SSH for
    # anything else, so "auto" must never collide with either.
    assert READ_SOURCE_AUTO not in (READ_SOURCE_SSH, READ_SOURCE_MQTT)
