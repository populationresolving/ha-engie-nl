"""Data update coordinator: one per Mijn ENGIE account."""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EngieAuthError, EngieClient, EngieCodeRequired, EngieData, EngieError
from .const import CONF_COOKIES, DOMAIN, RETRY_INTERVAL, UPDATE_INTERVAL
from .crypto import EngieCipher
from .statistics import ReadingHistory, async_update_daily_statistics, async_update_statistics

_LOGGER = logging.getLogger(__name__)

type EngieConfigEntry = ConfigEntry["EngieCoordinator"]


class EngieCoordinator(DataUpdateCoordinator[EngieData]):
    """Refreshes every 6 h, retries after 15 min when a refresh fails.

    Background re-logins never make ENGIE email a code: when Okta wants one, the
    entry goes to re-authentication and Home Assistant asks the user.
    """

    config_entry: EngieConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, cipher: EngieCipher) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL, config_entry=entry)
        self._cipher = cipher
        # Own session (and cookie jar) per account; closed on unload.
        self.session = async_create_clientsession(hass, auto_cleanup=False)
        self.history = ReadingHistory(hass, entry.entry_id)
        self.daily: dict[str, Any] = {"usage": {}, "cost": []}
        self._client: EngieClient | None = None
        self._saved_cookies: str | None = None

    def _client_or_auth_failed(self) -> EngieClient:
        if self._client is None:
            data = self.config_entry.data
            try:
                password = self._cipher.decrypt(data[CONF_PASSWORD])
                cookies = json.loads(self._cipher.decrypt(data[CONF_COOKIES])) if data.get(CONF_COOKIES) else []
            except ValueError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            self._client = EngieClient(self.session, data[CONF_USERNAME], password)
            self._client.import_cookies(cookies)
            self._saved_cookies = json.dumps(cookies, sort_keys=True)
        return self._client

    async def _async_update_data(self) -> EngieData:
        self.update_interval = RETRY_INTERVAL
        client = self._client_or_auth_failed()
        try:
            data = await client.async_get_data()
        except EngieCodeRequired as err:
            raise ConfigEntryAuthFailed("ENGIE asks for a new email code; sign in again") from err
        except EngieAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EngieError as err:
            raise UpdateFailed(str(err)) from err
        finally:
            self._async_save_cookies()
        self.update_interval = UPDATE_INTERVAL
        await self.history.async_merge(data)
        async_update_statistics(self.hass, self.history.history, data)
        self.daily = self.history.daily(data)
        async_update_daily_statistics(self.hass, self.daily)
        return data

    def _async_save_cookies(self) -> None:
        """Keep Okta's session/device cookies so a re-login can skip the email code."""
        if self._client is None:
            return
        cookies = self._client.export_cookies()
        serial = json.dumps(cookies, sort_keys=True)
        if serial == self._saved_cookies:
            return
        self._saved_cookies = serial
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_COOKIES: self._cipher.encrypt(json.dumps(cookies))},
        )

    async def async_close(self) -> None:
        await self.session.close()
