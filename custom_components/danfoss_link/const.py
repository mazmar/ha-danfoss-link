"""Constants for the Danfoss Link integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "danfoss_link"

CONF_PEER_ID = "peer_id"
CONF_PRIVATE_KEY = "private_key"
CONF_CERTIFICATE = "certificate"
CONF_PAIRING_CODE = "pairing_code"
CONF_DEVICE_NAME = "device_name"
CONF_ALARM_NOTIFICATIONS = "alarm_notifications"

DEFAULT_DEVICE_NAME = "Home Assistant"
DEFAULT_SCAN_INTERVAL = timedelta(seconds=60)
MIN_SCAN_INTERVAL = 20  # seconds; every poll goes through the Danfoss cloud

# Room regulation types
REGULATION_SCHEDULE = "Schedule"
REGULATION_MANUAL = "Manual"
# Rooms in this zone share one regulation type and one schedule.
ZONE_LIVING = "Living"

# House modes (/api/modes) and the path used to PUT them
MODE_AT_HOME = "AtHome"
MODE_VACATION = "Vacation"
MODE_PAUSE = "Pause"
MODE_BOOST = "Boost"
MODE_PATHS = {
    MODE_AT_HOME: "athome",
    MODE_VACATION: "vacation",
    MODE_PAUSE: "pause",
    MODE_BOOST: "boost",
}
MODE_ACTIVE = "Active"
MODE_INACTIVE = "Inactive"
MODE_PLANNED = "Planned"  # a vacation whose start time is in the future

# Alarm ids (Android string resource names) used for diagnostics entities
ALARM_BATTERY_LOW = "IDS_UA1_ALERTVIEWER_BATTERY_LOW_ALERT"
ALARM_BATTERY_CRITICAL = "IDS_UA1_ALERTVIEWER_BATTERY_CRITICAL_ALERT"
ALARM_NODE_DEAD = "IDS_UA1_ALERTVIEWER_NODE_DEAD_ALERT"
