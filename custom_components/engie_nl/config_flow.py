"""Config flow: sign in with the Mijn ENGIE username and password."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .api import EngieAuthError, EngieClient, EngieError, EngieMfaRequired
from .const import DOMAIN, NAME
from .crypto import EngieCipher

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="username")),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)


class EngieNLConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _async_try_login(self, username: str, password: str) -> dict[str, str]:
        session = async_create_clientsession(self.hass, auto_cleanup=False)
        try:
            client = EngieClient(session, username, password)
            await client.async_login()
            await client.async_get_data()
        except EngieMfaRequired:
            return {"base": "mfa_required"}
        except EngieAuthError as err:
            _LOGGER.warning("ENGIE sign-in refused: %s", err)
            return {"base": "invalid_auth"}
        except EngieError as err:
            _LOGGER.warning("ENGIE sign-in failed: %s", err)
            return {"base": "cannot_connect"}
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error signing in to Mijn ENGIE")
            return {"base": "unknown"}
        finally:
            await session.close()
        return {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()
            errors = await self._async_try_login(username, user_input[CONF_PASSWORD])
            if not errors:
                cipher = await EngieCipher.async_load(self.hass)
                return self.async_create_entry(
                    title=f"{NAME} ({username})",
                    data={CONF_USERNAME: username, CONF_PASSWORD: cipher.encrypt(user_input[CONF_PASSWORD])},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, {CONF_USERNAME: (user_input or {}).get(CONF_USERNAME, "")}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._async_try_login(entry.data[CONF_USERNAME], user_input[CONF_PASSWORD])
            if not errors:
                cipher = await EngieCipher.async_load(self.hass)
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: cipher.encrypt(user_input[CONF_PASSWORD])}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
            ),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )
