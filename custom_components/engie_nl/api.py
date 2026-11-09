"""Unofficial async client for Mijn ENGIE (ENGIE Nederland).

ENGIE NL has no public API. This client signs in the way the website does and
then calls the JSON endpoints the Mijn ENGIE web app itself uses:

1. The Sitecore site (``www.engie.nl/identity/externallogin``) answers with the
   Okta authorize URL.
2. If Okta still knows this client (session cookies), it answers with the
   auto-submit ``form_post`` right away. Otherwise its hosted sign-in page holds a
   ``stateToken`` for the Okta Identity Engine (IDX) API: identify → password →
   (if the policy asks) the email code → success redirect → ``form_post``.
3. The ``form_post`` to the site's callback sets the session cookies; the chain
   ends on ``mijn.engie.nl``, whose ``/api/*`` and ``/_next/data/<buildId>/*.json``
   then work with those cookies. A 401 means the session expired: sign in again.

The email code needs a person, so :meth:`EngieClient.async_login` raises
:class:`EngieCodeRequired` and :meth:`EngieClient.async_submit_code` continues.
Cookies can be exported/imported so Okta recognises the client next time.

Credentials, codes, tokens and cookies are never logged.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin, urlsplit

import aiohttp
from yarl import URL

from .parse import (
    IdxAction,
    MeteringPoint,
    Reading,
    auto_submit_form,
    extract_build_id,
    extract_state_token,
    idx_next,
    parse_meter_readings,
    parse_metering_points,
)

_LOGGER = logging.getLogger(__name__)

WWW_BASE = "https://www.engie.nl"
MIJN_BASE = "https://mijn.engie.nl"
EXTERNAL_LOGIN_URL = (
    f"{WWW_BASE}/identity/externallogin?authenticationType=OktaB2C"
    "&ReturnUrl=%2fidentity%2fexternallogincallback%3fReturnUrl%3d%252fauth%252fexternallysignedin"
    "%253fdeeplink%253d%26sc_site%3dengie%26authenticationSource%3dDefault&sc_site=engie"
)
USER_AGENT = "HomeAssistant-engie_nl/0.2 (+https://github.com/populationresolving/ha-engie-nl)"
TIMEOUT = aiohttp.ClientTimeout(total=45)
MAX_FORM_HOPS = 6
MAX_IDX_STEPS = 10
ION = "application/ion+json; okta-version=1.0.0"
IDX_JSON = "application/json; okta-version=1.0.0"


class EngieError(Exception):
    """Base error."""


class EngieConnectionError(EngieError):
    """Network trouble or an unexpected server answer; worth retrying later."""


class EngieAuthError(EngieError):
    """The credentials were refused or the session cannot be established."""


class EngieCodeRequired(EngieAuthError):
    """Okta wants the email code. ``sent`` tells whether it was just emailed."""

    def __init__(self, sent: bool) -> None:
        super().__init__("ENGIE asks for the code it emails" + (" (sent)" if sent else ""))
        self.sent = sent


# Kept for callers of 0.1.x; an email code is the only second step supported.
EngieMfaRequired = EngieCodeRequired


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


@dataclass
class _IdxState:
    """An IDX sign-in waiting for the email code."""

    base: str
    response: Dict[str, Any]
    password_used: bool
    password_answers: int = 0


class EngieClient:
    """One Mijn ENGIE account. Owns its own cookie jar via the given session."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._build_id: Optional[str] = None
        self._logged_in = False
        self._pending: Optional[_IdxState] = None
        self._lock = asyncio.Lock()

    # -------------------------------------------------------------- cookies

    def export_cookies(self) -> List[Dict[str, Any]]:
        """All cookies (Okta session/device, site session) to persist, never to log."""
        cookies = []
        for morsel in self._session.cookie_jar:
            cookies.append(
                {
                    "name": morsel.key,
                    "value": morsel.value,
                    "domain": morsel["domain"],
                    "path": morsel["path"] or "/",
                    "expires": morsel["expires"],
                    "secure": bool(morsel["secure"]),
                }
            )
        return cookies

    def import_cookies(self, cookies: List[Dict[str, Any]]) -> None:
        """Restore exported cookies; assumes the Mijn ENGIE session may still be valid."""
        restored = False
        for item in cookies or []:
            domain = str(item.get("domain") or "").lstrip(".")
            if not domain or not item.get("name"):
                continue
            jar = SimpleCookie()
            jar[item["name"]] = item.get("value", "")
            morsel = jar[item["name"]]
            morsel["domain"] = item["domain"]
            morsel["path"] = item.get("path") or "/"
            if item.get("expires"):
                morsel["expires"] = item["expires"]
            if item.get("secure"):
                morsel["secure"] = True
            self._session.cookie_jar.update_cookies(jar, URL(f"https://{domain}/"))
            restored = True
        self._logged_in = restored

    # ---------------------------------------------------------------- login

    async def async_login(self, *, send_code: bool = True) -> None:
        """Sign in. Raises EngieCodeRequired when Okta wants the email code.

        With ``send_code=False`` it stops before Okta emails a code (for unattended
        refreshes, so a background retry never floods the inbox).
        """
        async with self._lock:
            self._logged_in = False
            self._pending = None
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

            url, html = await self._get_html(authorize)
            if auto_submit_form(html) is None:
                token = extract_state_token(html)
                if not token:
                    raise EngieConnectionError(f"ENGIE sign-in page not understood (at {_where(url)})")
                base = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
                response = await self._idx_post(f"{base}/idp/idx/introspect", {"stateToken": token})
                url, html = await self._idx_run(_IdxState(base, response, False), send_code=send_code)
            await self._finish(url, html)
        await self._api_json("/api/app-user", relogin=False)
        _LOGGER.debug("Signed in to Mijn ENGIE")

    async def async_submit_code(self, code: str) -> None:
        """Continue a sign-in that stopped for the email code."""
        async with self._lock:
            state = self._pending
            if state is None:
                raise EngieAuthError("No ENGIE sign-in is waiting for a code; start again")
            action = idx_next(state.response, state.password_used)
            if action.kind != "code" or not action.href:
                raise EngieAuthError("The ENGIE sign-in is no longer waiting for a code; start again")
            response = await self._idx_post(
                urljoin(state.base, action.href),
                {"credentials": {"passcode": code.strip()}, "stateHandle": state.response.get("stateHandle")},
            )
            if idx_next(response, state.password_used).kind == "error":
                # Wrong or expired code: keep waiting so the user can try again.
                state.response = {k: v for k, v in response.items() if k != "messages"}
                raise EngieAuthError("ENGIE refused the code")
            state.response = response
            self._pending = None
            url, html = await self._idx_run(state, send_code=True)
            await self._finish(url, html)
        await self._api_json("/api/app-user", relogin=False)

    async def _idx_run(self, state: _IdxState, *, send_code: bool) -> "tuple[str, str]":
        """Walk the IDX remediations until success; park the state when a code is needed."""
        for _ in range(MAX_IDX_STEPS):
            action: IdxAction = idx_next(state.response, state.password_used)
            handle = state.response.get("stateHandle")
            if action.kind == "success" and action.href:
                return await self._get_html(urljoin(state.base, action.href))
            if action.kind == "error":
                raise EngieAuthError(f"ENGIE refused the sign-in: {action.message}")
            if action.kind == "code":
                self._pending = state
                raise EngieCodeRequired(sent=True)
            if not action.href:
                raise EngieConnectionError(f"ENGIE sign-in step without address ({action.kind})")
            href = urljoin(state.base, action.href)
            if action.kind == "identify":
                body: Dict[str, Any] = {"identifier": self._username, "stateHandle": handle, "rememberMe": True}
                if action.with_password:
                    body["credentials"] = {"passcode": self._password}
                    state.password_used = True
            elif action.kind == "password":
                if state.password_answers >= 1:
                    raise EngieAuthError("ENGIE keeps asking for the password")
                body = {"credentials": {"passcode": self._password}, "stateHandle": handle}
                state.password_used = True
                state.password_answers += 1
            elif action.kind in ("select", "send_email"):
                if action.method == "email" and not send_code:
                    raise EngieCodeRequired(sent=False)
                body = {"authenticator": {"id": action.authenticator_id, "methodType": action.method}, "stateHandle": handle}
            else:
                raise EngieAuthError(f"ENGIE asks for a sign-in step this integration doesn't support ({action.message})")
            state.response = await self._idx_post(href, body)
        raise EngieAuthError("Too many ENGIE sign-in steps")

    async def _idx_post(self, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            async with self._session.post(
                url,
                json=body,
                headers={**self._headers(), "Content-Type": IDX_JSON, "Accept": ION},
                timeout=TIMEOUT,
            ) as resp:
                status = resp.status
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    data = None
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise EngieConnectionError(f"login.engie.nl not reachable: {err}") from err
        if status == 429:
            raise EngieConnectionError("Too many sign-in attempts; ENGIE asks to wait")
        if not isinstance(data, dict):
            raise EngieConnectionError(f"Unexpected answer from the ENGIE sign-in (HTTP {status})")
        if status >= 400 and not (data.get("messages") or data.get("remediation")):
            raise EngieConnectionError(f"ENGIE sign-in error (HTTP {status})")
        return data

    async def _finish(self, url: str, html: str) -> None:
        """From Okta's form_post to a working Mijn ENGIE session."""
        url, html = await self._follow_forms(url, html)
        url, html = await self._get_html(f"{MIJN_BASE}/")
        url, html = await self._follow_forms(url, html)
        if urlsplit(url).netloc != urlsplit(MIJN_BASE).netloc:
            raise EngieAuthError(f"Sign-in did not reach Mijn ENGIE (ended at {_where(url)})")
        self._build_id = extract_build_id(html)
        self._logged_in = True

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
            await self.async_login(send_code=False)
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
                    await self.async_login(send_code=False)
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
                        await self.async_login(send_code=False)
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
            await self.async_login(send_code=False)
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
