"""Constants for the ENGIE Nederland integration."""

from datetime import timedelta

DOMAIN = "engie_nl"
NAME = "ENGIE Nederland"

UPDATE_INTERVAL = timedelta(hours=6)
RETRY_INTERVAL = timedelta(minutes=15)

STORAGE_VERSION = 1
STORAGE_KEY_SECRET = f"{DOMAIN}_key"
STORAGE_KEY_READINGS = f"{DOMAIN}_readings"  # + f".{entry_id}"

CONF_COOKIES = "cookies"  # Fernet-encrypted JSON list of cookies (Okta session/device)

ATTRIBUTION = "Data from Mijn ENGIE (unofficial)"
