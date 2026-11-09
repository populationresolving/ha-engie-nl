"""Config flow: Mijn ENGIE username + password, then the emailed code if Okta asks."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .api import EngieAuthError, EngieClient, EngieCodeRequired, EngieError, EngieSessionError
from .const import CONF_COOKIES, DOMAIN, NAME
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
CODE_SCHEMA = vol.Schema(
    {vol.Required("code"): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="one-time-code"))}
)


class EngieNLConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._client: EngieClient | None = None
        self._username: str | None = None
        self._password: str | None = None

    # ------------------------------------------------------------ helpers

    async def _async_close(self) -> None:
        if self._session is not None:
            await self._session.close()
        self._session = None
        self._client = None

    def async_remove(self) -> None:
        """Flow aborted or finished: drop the HTTP session."""
        if self._session is not None:
            self.hass.async_create_task(self._session.close())
            self._session = None

    async def _async_start(self, username: str, password: str, cookies: list | None = None) -> dict[str, str] | None:
        """Sign in. Returns form errors, {} when done, or None when a code is needed."""
        await self._async_close()
        self._session = async_create_clientsession(self.hass, auto_cleanup=False)
        self._client = EngieClient(self._session, username, password)
        if cookies:
            self._client.import_cookies(cookies)
        self._username, self._password = username, password
        return await self._async_run(lambda: self._client.async_login(send_code=True))

    async def _async_run(self, step) -> dict[str, str] | None:
        try:
            await step()
            await self._client.async_get_data()
        except EngieCodeRequired:
            return None
        except EngieSessionError as err:
            _LOGGER.warning("ENGIE sign-in accepted, but Mijn ENGIE gave no session: %s", err)
            return {"base": "no_session"}
        except EngieAuthError as err:
            _LOGGER.warning("ENGIE sign-in refused: %s", err)
            return {"base": "invalid_auth"}
        except EngieError as err:
            _LOGGER.warning("ENGIE sign-in failed: %s", err)
            return {"base": "cannot_connect"}
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error signing in to Mijn ENGIE")
            return {"base": "unknown"}
        return {}

    async def _async_entry_data(self) -> dict[str, Any]:
        cipher = await EngieCipher.async_load(self.hass)
        data = {
            CONF_USERNAME: self._username,
            CONF_PASSWORD: cipher.encrypt(self._password or ""),
            CONF_COOKIES: cipher.encrypt(json.dumps(self._client.export_cookies())),
        }
        await self._async_close()
        return data

    async def _async_done(self) -> ConfigFlowResult:
        data = await self._async_entry_data()
        if self.source == "reauth":
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates=data)
        return self.async_create_entry(title=f"{NAME} ({self._username})", data=data)

    # -------------------------------------------------------------- steps

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()
            result = await self._async_start(username, user_input[CONF_PASSWORD])
            if result is None:
                return await self.async_step_code()
            if not result:
                return await self._async_done()
            errors = result
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, {CONF_USERNAME: (user_input or {}).get(CONF_USERNAME, "")}
            ),
            errors=errors,
        )

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and self._client is not None:
            result = await self._async_run(lambda: self._client.async_submit_code(user_input["code"]))
            if result is None:
                errors = {"base": "invalid_code"}  # Okta asked for another code
            elif not result:
                return await self._async_done()
            else:
                errors = {"base": "invalid_code"} if result.get("base") == "invalid_auth" else result
        return self.async_show_form(step_id="code", data_schema=CODE_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Sign in again; the password field may be left as stored."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            cipher = await EngieCipher.async_load(self.hass)
            password = user_input.get(CONF_PASSWORD)
            if not password:
                try:
                    password = cipher.decrypt(entry.data[CONF_PASSWORD])
                except ValueError:
                    errors = {"base": "invalid_auth"}
            if not errors:
                cookies = None
                try:
                    cookies = json.loads(cipher.decrypt(entry.data[CONF_COOKIES])) if entry.data.get(CONF_COOKIES) else None
                except ValueError:
                    cookies = None
                result = await self._async_start(entry.data[CONF_USERNAME], password, cookies)
                if result is None:
                    return await self.async_step_code()
                if not result:
                    return await self._async_done()
                errors = result
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Optional(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
            ),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )
