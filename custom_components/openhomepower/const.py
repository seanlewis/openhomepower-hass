"""Constants for the OpenHomepower integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "openhomepower"
MANUFACTURER = "Energizer / Enertek"
MODEL = "Homepower"

CONF_POLL_SECONDS = "poll_seconds"

# The vendor daemon only refreshes its log every ~15-20 s while the battery is
# active, and minutes apart at idle, so polling faster gains nothing and only
# loads a fragile gateway.
DEFAULT_POLL_SECONDS = 60
MIN_POLL_SECONDS = 30
DEFAULT_SCAN_INTERVAL = timedelta(seconds=DEFAULT_POLL_SECONDS)

# Values that belong on the device page rather than in the main UI. The DC
# figures duplicate the AC ones (differing only by inverter loss) and the
# per-module currents are only interesting when diagnosing a failing module.
DIAGNOSTIC_KEYS = {
    "battery_charge_dc_w",
    "battery_discharge_dc_w",
    "battery_current_mod1_a",
    "battery_current_mod2_a",
    "device_serial",
}

# Not surfaced at all: the DC daily counters would sit beside the AC ones in the
# Energy Dashboard's statistic picker and invite picking the wrong one.
EXCLUDED_KEYS = {
    "daily_charge_dc_kwh",
    "daily_discharge_dc_kwh",
}

# --- control (opt-in write path) --------------------------------------------
# Control publishes to a CONFIGURABLE broker — the vendor broker today, a local
# broker after cutover. Off by default so the integration stays read-only unless
# a user deliberately turns it on.
CONF_CONTROL_ENABLED = "control_enabled"
CONF_BROKER_HOST = "broker_host"
CONF_BROKER_PORT = "broker_port"
CONF_BROKER_USER = "broker_user"
CONF_BROKER_PASSWORD = "broker_password"
CONF_TOPIC_SERIAL = "topic_serial"

# Broker settings are NOT hardcoded — they are derived from the device over SSH
# at config time (host, port, credentials, topic serial), so no vendor secrets
# live in this (public) source. Only the port default is a plain, non-secret value.
DEFAULT_BROKER_PORT = 1884

# Config registers barely change, so poll them rarely.
CONTROL_SCAN_INTERVAL = timedelta(seconds=600)

SERVICE_SET_SCHEDULE = "set_schedule"
