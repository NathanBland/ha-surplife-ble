"""Constants for the Surplife BLE integration."""

from datetime import timedelta

DOMAIN = "surplife_ble"
PLATFORMS = ["light"]
UPDATE_INTERVAL = timedelta(seconds=15)

CONF_DISCONNECT_DELAY = "disconnect_delay"
DEFAULT_DISCONNECT_DELAY = 30
MIN_DISCONNECT_DELAY = 0
MAX_DISCONNECT_DELAY = 3600
