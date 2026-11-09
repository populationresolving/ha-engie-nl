"""Unofficial async client for Mijn ENGIE (ENGIE Nederland).

ENGIE NL has no public API. This client signs in the way the website does and
then calls the JSON endpoints the Mijn ENGIE web app itself uses:

1. Okta Authn API (``login.engie.nl/api/v1/authn``) exchanges username/password
   for a one-time ``sessionToken``.
2. The Sitecore site (``www.engie.nl/identity/externallogin``) answers with the
   Okta authorize URL; adding ``sessionToken`` skips the login page.
3. Okta replies with an auto-submit ``form_post`` to the site's callback, which
   sets the session cookies; the chain ends on ``mijn.engie.nl``.
4. ``mijn.engie.nl/api/*`` and ``/_next/data/<buildId>/*.json`` then work with
   those cookies. A 401 means the session expired: sign in again once.

Credentials, tokens and cookies are never logged.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin, urlsplit

import aiohttp

from .parse import (
    MeteringPoint,
    Reading,
    auto_submit_form,
    extract_build_id,
    parse_meter_readings,
    parse_metering_points,
)

_LOGGER = logging.getLogger(__name__)

LOGIN_BASE = "https://login.engie.nl"
WWW_BASE = "https://www.engie.nl"
MIJN_BASE = "https://mijn.engie.nl"
AUTHN_URL = f"{LOGIN_BASE}/api/v1/authn"
EXTERNAL_LOGIN_URL = (
    f"{WWW_BASE}/identity/externallogin?authenticationType=OktaB2C"
    "&ReturnUrl=%2fidentity%2fexternallogincallback%3fReturnUrl%3d%252fauth%252fexternallysignedin"
    "%253fdeeplink%253d%26sc_site%3dengie%26authenticationSource%3dDefault&sc_site=engie"
)
USER_AGENT = "HomeAssistant-engie_nl/0.1 (+https://github.com/populationresolving/ha-engie-nl)"
TIMEOUT = aiohttp.ClientTimeout(total=45)
MAX_FORM_HOPS = 6


class EngieError(Exception):
    """Base error."""


class EngieConnectionError(EngieError):
    """Network trouble or an unexpected server answer; worth retrying later."""


class EngieAuthError(EngieError):
    """The credentials were refused or the session cannot be established."""


class EngieMfaRequired(EngieAuthError):
    """The account asks for a second factor, which this client does not support."""


@dataclass
class EngieData:
    """Everything one refresh returns."""

    metering_points: List[MeteringPoint]
    readings: Dict[str, List[Reading]]
    consumption: Optional[Dict[str, Any]] = None
    estimations: Optional[Dict[str, Any]] = None
    transactions: List[Dict[str, Any]] = field(default_factory=list)
    payments_info: Optional[Dict[str, Any]] = None
    flags: Dict[str, Any] = field(default_factory=dict)

    def point(self, kind: str) -> Optional[MeteringPoint]:
        return next((p for p in self.metering_points if p.kind == kind), None)


def _where(url: Any) -> str:
    """Host + path of a URL, without the query (which can carry tokens)."""
    parts = urlsplit(str(url))
    return f"{parts.hostname}{parts.path}"


class EngieClient:
    """One Mijn ENGIE account. Owns its own cookie jar via the given session."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._build_id: Optional[str] = None
        self._logged_in = False
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- login

    async def async_login(self) -> None:
        """Sign in from scratch (clears earlier cookies)."""
        async with self._lock:
            self._logged_in = False
            self._session.cookie_jar.clear()
            token = await self._authn()

            try:
                async with self._session.post(
                    EXTERNAL_LOGIN_URL, allow_redirects=False, headers=self._headers(), timeout=TIMEOUT
                ) as resp:
                    authorize = resp.headers.get("Location")
                    status = resp.status
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise EngieConnectionError(f"www.engie.nl not reachable: {err}") from err
            if status not in (301, 302, 303, 307, 308) or not authorize or "/authorize" not in authorize:
                raise EngieConnectionError(f"Unexpected answer from the ENGIE login start ({status})")
            authorize = urljoin(EXTERNAL_LOGIN_URL, authorize)
            authorize +=("&" if "?" in authorize else "?") + "sessionToken=" + quote(token, safe="")

            url, html = await self._get_html(authorize)
            url, html = await self._follow_forms(url, html)
            # Land on Mijn ENGIE; it may run its own short redirect/form chain.
            url, html = await self._get_html(f"{MIJN_BASE}/")
            url, html = await self._follow_forms(url, html)
            if urlsplit(url).netloc != urlsplit(MIJN_BASE).netloc:
                raise EngieAuthError(f"Sign-in did not reach Mijn ENGIE (ended at {_where(url)})")
            self._build_id = extract_build_id(html)
            self._logged_in = True
        # Proves the API session works (outside the lock: it may not trigger a re-login).
        await self._api_json("/api/app-user", relogin=False)
        _LOGGER.debug("Signed in to Mijn ENGIE")

    async def _authn(self) -> str:
        body = {
            "username": self._username,
            "password": self._password,
            "options": {"warnBeforePasswordExpired": False, "multiOptionalFactorEnroll": False},
        }
        try:
            async with self._session.post(
                AUTHN_URL,
                json=body,
                headers={**self._headers(), "Accept": "application/json"},
                timeout=TIMEOUT,
            ) as resp:
                status = resp.status
                data = await resp.json(content_type=None) if resp.content_length != 0 else {}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise EngieConnectionError(f"login.engie.nl not reachable: {err}") from err
        if status == 401 or (isinstance(data, dict) and data.get("errorCode") == "E0000004"):
            raise EngieAuthError("ENGIE refused the username or password")
        if status == 429:
            raise EngieConnectionError("Too many sign-in attempts; ENGIE asks to wait")
        if status != 200 or not isinstance(data, dict):
            raise EngieConnectionError(f"Unexpected answer from the ENGIE sign-in ({status})")
        state = data.get("status")
        if state == "SUCCESS" and data.get("sessionToken"):
            return str(data["sessionToken"])
        if state in ("MFA_REQUIRED", "MFA_ENROLL", "MFA_CHALLENGE"):
            raise EngieMfaRequired("This ENGIE account asks for a second sign-in step")
        if state in ("LOCKED_OUT", "PASSWORD_EXPIRED", "PASSWORD_RESET"):
            raise EngieAuthError(f"ENGIE account needs attention: {state}")
        raise EngieAuthError(f"ENGIE sign-in did not finish (status {state})")

    async def _get_html(self, url: str) -> "tuple[str, str]":
        try:
            async with self._session.get(url, headers=self._headers(), timeout=TIMEOUT) as resp:
                return str(resp.url), await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise EngieConnectionError(f"{_where(url)} not reachable: {err}") from err

    async def _follow_forms(self, url: str, html: str) -> "tuple[str, str]":
        """Submit OIDC auto-submit forms the way a browser's script would."""
        for _ in range(MAX_FORM_HOPS):
            form = auto_submit_form(html)
            if form is None:
                return url, html
            target = urljoin(url, form.action) if form.action else url
            try:
                async with self._session.post(
                    target, data=form.inputs, headers=self._headers(), timeout=TIMEOUT
                ) as resp:
                    url, html = str(resp.url), await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise EngieConnectionError(f"{_where(target)} not reachable: {err}") from err
        raise EngieAuthError("Too many sign-in redirects")

    def _headers(self) -> Dict[str, str]:
        return {"User-Agent": USER_AGENT, "Accept-Language": "nl-NL,nl;q=0.9"}

    # ------------------------------------------------------------------ API

    async def _api_json(self, path: str, *, relogin: bool = True) -> Any:
        if not self._logged_in and relogin:
            await self.async_login()
        for attempt in range(3):
            try:
                async with self._session.get(
                    f"{MIJN_BASE}{path}",
                    allow_redirects=False,
                    headers={**self._headers(), "Accept": "application/json"},
                    timeout=TIMEOUT,
                ) as resp:
                    status = resp.status
                    data = await resp.json(content_type=None) if status == 200 else None
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                raise EngieConnectionError(f"Mijn ENGIE not reachable: {err}") from err
            name = path.split("?")[0]
            if status in (401, 403) or 300 <= status < 400:
                if relogin and attempt == 0:
                    await self.async_login()
                    continue
                raise EngieAuthError(f"Mijn ENGIE session refused ({name} → {status})")
            if status >= 500 and attempt < 2:
                await asyncio.sleep(2)
                continue
            if status != 200:
                raise EngieConnectionError(f"Mijn ENGIE {name} → {status}")
            return data
        raise EngieConnectionError("Mijn ENGIE kept failing")

    async def _page_data(self, page: str) -> Dict[str, Any]:
        """`/_next/data/<buildId>/<page>.json`; refreshes the buildId after a site deploy."""
        for attempt in range(2):
            if not self._build_id:
                _, html = await self._get_html(f"{MIJN_BASE}/")
                self._build_id = extract_build_id(html)
                if not self._build_id:
                    if attempt == 0:
                        await self.async_login()
                        continue
                    raise EngieConnectionError("Could not find the Mijn ENGIE build id")
            try:
                return await self._api_json(f"/_next/data/{self._build_id}/{page}.json")
            except EngieConnectionError as err:
                if attempt == 0 and "→ 404" in str(err):
                    self._build_id = None
                    continue
                raise
        raise EngieConnectionError("Mijn ENGIE page data unavailable")

    async def async_get_data(self) -> EngieData:
        """One full refresh. Optional parts that fail are left empty, not fatal."""
        if not self._logged_in:
            await self.async_login()
        page = await self._page_data("contract")
        user = ((page or {}).get("pageProps") or {}).get("data", {}).get("user") or {}
        points = parse_metering_points(user)
        if not points:
            raise EngieConnectionError("No connections found on this Mijn ENGIE account")
        eans = "&".join(f"eans[]={quote(p.ean)}" for p in points if p.ean)
        address_id = (user.get("selectedAddress") or {}).get("id")
        prepayment = sum(p.prepayment_amount or 0 for p in points)

        data = EngieData(
            metering_points=points,
            readings={},
            flags={
                k: user.get(k)
                for k in ("hasFixedContract", "hasDynamicProduct", "hasSmartMeter", "hasGas", "hasPower")
            },
        )
        data.readings = parse_meter_readings(await self._api_json(f"/api/meterstands?{eans}"))

        async def optional(path: str) -> Any:
            try:
                return await self._api_json(path)
            except EngieAuthError:
                raise
            except EngieError as err:
                _LOGGER.debug("Mijn ENGIE %s skipped: %s", path.split("?")[0], err)
                return None

        consumption_path = f"/api/consumption?{eans}"
        if address_id:
            consumption_path = f"/api/consumption?deliveryAddressId={quote(str(address_id))}&{eans}"
        data.consumption = await optional(consumption_path)
        data.estimations = await optional(f"/api/estimations?amount={round(prepayment)}&{eans}")
        transactions = await optional("/api/transactions")
        data.transactions = (transactions or {}).get("transactions") or []
        data.payments_info = await optional("/api/payments-info")
        return data
