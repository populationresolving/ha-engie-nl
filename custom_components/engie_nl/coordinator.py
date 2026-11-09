"""Data update coordinator: one per Mijn ENGIE account."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EngieAuthError, EngieClient, EngieData, EngieError
from .const import DOMAIN, RETRY_INTERVAL, UPDATE_INTERVAL
from .crypto import EngieCipher
from .statistics import ReadingHistory, async_update_statistics

_LOGGER = logging.getLogger(__name__)

type EngieConfigEntry = ConfigEntry["EngieCoordinator"]


class EngieCoordinator(DataUpdateCoordinator[EngieData]):
    """Refreshes every 6 h, retries after 15 min when a refresh fails."""

    config_entry: EngieConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, cipher: EngieCipher) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL, config_entry=entry)
        self._cipher = cipher
        # Own session (and cookie jar) per account; closed on unload.
        self.session = async_create_clientsession(hass, auto_cleanup=False)
        self.history = ReadingHistory(hass, entry.entry_id)
        self._client: EngieClient | None = None

    def _client_or_auth_failed(self) -> EngieClient:
        if self._client is None:
            try:
                password = self._cipher.decrypt(self.config_entry.data[CONF_PASSWORD])
            except ValueError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            self._client = EngieClient(self.session, self.config_entry.data[CONF_USERNAME], password)
        return self._client

    async def _async_update_data(self) -> EngieData:
        self.update_interval = RETRY_INTERVAL
        client = self._client_or_auth_failed()
        try:
            data = await client.async_get_data()
        except EngieAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EngieError as err:
            raise UpdateFailed(str(err)) from err
        self.update_interval = UPDATE_INTERVAL
        await self.history.async_merge(data)
        async_update_statistics(self.hass, self.history.history, data)
        return data

    async def async_close(self) -> None:
        await self.session.close()
