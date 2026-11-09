"""At-rest encryption for the stored ENGIE password.

The integration must sign in again on its own when the Mijn ENGIE session
expires, so it keeps the password, but only encrypted. The Fernet key lives in a
separate store (``.storage/engie_nl_key``), so ``core.config_entries`` alone (a
pasted file, a diagnostics dump) does not reveal a reusable password. Anyone with
full filesystem access can still recover it; this is not disk encryption.

Same approach as the ENGIE România integration (coffeerunhobby/ha-engie).
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY_SECRET, STORAGE_VERSION

PREFIX = "enc:v1:"


class EngieCipher:
    """Encrypts/decrypts the stored password with a locally stored key."""

    def __init__(self, key: bytes) -> None:
        from cryptography.fernet import Fernet

        self._fernet = Fernet(key)

    @classmethod
    async def async_load(cls, hass: HomeAssistant) -> EngieCipher:
        from cryptography.fernet import Fernet

        store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY_SECRET, private=True)
        data = await store.async_load() or {}
        key = data.get("key")
        if not key:
            key = Fernet.generate_key().decode()
            await store.async_save({"key": key})
        return cls(key.encode())

    def encrypt(self, value: str) -> str:
        return PREFIX + self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        """Raises ValueError when the value can't be decrypted (e.g. key file missing)."""
        if not isinstance(value, str) or not value.startswith(PREFIX):
            raise ValueError("The stored ENGIE password is not encrypted")
        from cryptography.fernet import InvalidToken

        try:
            return self._fernet.decrypt(value[len(PREFIX) :].encode()).decode()
        except (InvalidToken, ValueError) as err:
            raise ValueError(
                "The stored ENGIE password could not be decrypted; sign in again"
            ) from err
