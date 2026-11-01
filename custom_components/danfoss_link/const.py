"""Constants for the Danfoss Link integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "danfoss_link"

CONF_PEER_ID = "peer_id"
CONF_PRIVATE_KEY = "private_key"
CONF_CERTIFICATE = "certificate"
CONF_PAIRING_CODE = "pairing_code"
CONF_DEVICE_NAME = "device_name"

DEFAULT_DEVICE_NAME = "Home Assistant"
DEFAULT_SCAN_INTERVAL = timedelta(seconds=60)

# REST "modes" understood by PUT /api/modes/<path>
MODE_PATHS = {
    "AtHome": "athome",
    "Vacation": "vacation",
    "Pause": "pause",
    "Boost": "boost",
}
