"""Pure helpers for the Mijn ENGIE payloads (no Home Assistant, no aiohttp).

Kept free of Home Assistant imports so they can be unit tested on their own.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

# Register names as Mijn ENGIE reports them in /api/meterstands, mapped to stable keys.
REGISTER_KEYS = {
    ("E", "laag"): "electricity_low",
    ("E", "hoog"): "electricity_normal",
    ("E", "normaal"): "electricity_normal",
    ("E", "enkel"): "electricity_single",
    ("E", "teruglevering laag"): "return_low",
    ("E", "teruglevering normaal"): "return_normal",
    ("E", "teruglevering hoog"): "return_normal",
    ("E", "teruglevering enkel"): "return_single",
    ("G", "hoog"): "gas",
    ("G", "laag"): "gas",
    ("G", "normaal"): "gas",
    ("G", "enkel"): "gas",
}

# Which tariff prices a register's usage (all-in, incl. VAT and energy tax).
REGISTER_TARIFF = {
    "electricity_low": ("ELK", "tariff_low"),
    "electricity_normal": ("ELK", "tariff_normal"),
    "electricity_single": ("ELK", "tariff_single"),
    "return_low": ("ELK", "return_tariff_low"),
    "return_normal": ("ELK", "return_tariff_normal"),
    "return_single": ("ELK", "return_tariff_single"),
    "gas": ("GAS", "tariff_single"),
}

_BUILD_ID_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def extract_build_id(html: str) -> Optional[str]:
    """The Next.js buildId from a Mijn ENGIE HTML page, or None."""
    match = _BUILD_ID_RE.search(html or "")
    if not match:
        return None
    try:
        return json.loads(match.group(1)).get("buildId")
    except ValueError:
        return None


@dataclass
class HtmlForm:
    """A <form> with its hidden/visible input values."""

    action: str
    method: str
    inputs: Dict[str, str] = field(default_factory=dict)


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: List[HtmlForm] = []
        self._current: Optional[HtmlForm] = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attr = {k: (v or "") for k, v in attrs}
        if tag == "form":
            self._current = HtmlForm(attr.get("action", ""), (attr.get("method") or "get").lower())
            self.forms.append(self._current)
        elif tag == "input" and self._current is not None and attr.get("name"):
            self._current.inputs[attr["name"]] = attr.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._current = None


def find_forms(html: str) -> List[HtmlForm]:
    parser = _FormParser()
    parser.feed(html or "")
    return parser.forms


# Fields that mark an OIDC/SAML auto-submit form (Okta form_post response, Sitecore relays).
_AUTO_SUBMIT_FIELDS = {"code", "id_token", "access_token", "state", "SAMLResponse", "wresult"}


def auto_submit_form(html: str) -> Optional[HtmlForm]:
    """The OIDC form_post form on a page, if any (the one a browser submits by script)."""
    for form in find_forms(html):
        if form.method == "post" and _AUTO_SUBMIT_FIELDS & set(form.inputs):
            return form
    return None


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> Optional[date]:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


@dataclass
class MeteringPoint:
    """One connection (electricity or gas) with its contract and tariffs."""

    kind: str  # "ELK" or "GAS"
    ean: str
    smart: bool
    single_tariff: bool
    product_name: Optional[str]
    product_type: Optional[str]
    contract_start: Optional[date]
    contract_end: Optional[date]
    prepayment_amount: Optional[float]
    next_prepayment_amount: Optional[float]
    next_meter_request_date: Optional[date]
    grid_operator: Optional[str]
    yearly_estimate: Dict[str, Optional[float]]
    tariffs: Dict[str, Optional[float]]


def parse_metering_points(user: Dict[str, Any]) -> List[MeteringPoint]:
    """Metering points from the `user` object in the Next.js page data."""
    address = user.get("selectedAddress") or (user.get("delivery_addresses") or [{}])[0] or {}
    points = []
    for mp in address.get("metering_points") or []:
        product = mp.get("current_product") or {}
        points.append(
            MeteringPoint(
                kind=str(mp.get("type") or "").upper(),
                ean=str(mp.get("ean") or ""),
                smart=bool(mp.get("smart")),
                single_tariff=bool(mp.get("single_tariff")),
                product_name=product.get("name"),
                product_type=product.get("product_type"),
                contract_start=_parse_date(product.get("start_date") or mp.get("start_date")),
                contract_end=_parse_date(product.get("end_date") or mp.get("end_date")),
                prepayment_amount=_num(mp.get("prepayment_amount")),
                next_prepayment_amount=_num(mp.get("next_prepayment_amount")),
                next_meter_request_date=_parse_date(mp.get("next_meter_request_date")),
                grid_operator=mp.get("grid_owner_name"),
                yearly_estimate={
                    k: _num(mp.get(f"sjv_{k}"))
                    for k in ("normal", "low", "single", "return_normal", "return_low", "return_single")
                },
                tariffs={k: _num(v) for k, v in (mp.get("tariffs") or {}).items()},
            )
        )
    return points


@dataclass
class Reading:
    date: date
    value: float
    source: Optional[str] = None


def parse_meter_readings(payload: Any) -> Dict[str, List[Reading]]:
    """`/api/meterstands` → {register key: readings oldest first}."""
    result: Dict[str, List[Reading]] = {}
    for meter in payload or []:
        for register in meter.get("data") or []:
            key = REGISTER_KEYS.get(
                (str(register.get("type") or "").upper()[:1], str(register.get("name") or "").strip().lower())
            )
            if key is None:
                continue
            for raw in register.get("readings") or []:
                day, value = _parse_date(raw.get("date")), _num(raw.get("value"))
                if day is None or value is None:
                    continue
                result.setdefault(key, []).append(Reading(day, value, raw.get("source")))
    for key in result:
        # One value per day (the latest listed wins), oldest first.
        by_day = {r.date: r for r in result[key]}
        result[key] = [by_day[d] for d in sorted(by_day)]
    return result


def merge_readings(
    history: Dict[str, List[List[Any]]], new: Dict[str, List[Reading]]
) -> Dict[str, List[List[Any]]]:
    """Merge fresh readings into the stored history ({key: [[iso date, value], ...]}).

    Mijn ENGIE only lists the last couple of readings, so the integration keeps
    its own history to build long-term statistics from.
    """
    merged: Dict[str, List[List[Any]]] = {}
    for key in set(history) | set(new):
        by_day: Dict[str, float] = {d: v for d, v in history.get(key, [])}
        for reading in new.get(key, []):
            by_day[reading.date.isoformat()] = reading.value
        merged[key] = [[d, by_day[d]] for d in sorted(by_day)]
    return merged


def cumulative_points(
    readings: List[List[Any]], price: Optional[float] = None
) -> List[Tuple[date, float, float, Optional[float]]]:
    """(day, meter value, usage sum since the first reading, cost sum) per reading, oldest first.

    A reading lower than the one before it (meter replaced or corrected) starts a new
    baseline instead of producing negative usage. Costs use the current tariff for
    every period, which is an approximation for months before a tariff change.
    """
    points: List[Tuple[date, float, float, Optional[float]]] = []
    total = 0.0
    cost: Optional[float] = 0.0 if price is not None else None
    previous: Optional[float] = None
    for day_iso, value in readings:
        value = float(value)
        if previous is not None and value >= previous:
            delta = value - previous
            total = round(total + delta, 3)
            if cost is not None and price is not None:
                cost = round(cost + delta * price, 4)
        previous = value
        points.append((date.fromisoformat(day_iso), value, total, cost))
    return points


# ------------------------------------------------------------------ Okta IDX

_STATE_TOKEN_RE = re.compile(r'stateToken["\']?\s*[:=]\s*["\']([^"\']+)["\']')


def extract_state_token(html: str) -> Optional[str]:
    """The Okta stateToken embedded in the hosted sign-in page (JS-escaped as \\xNN)."""
    match = _STATE_TOKEN_RE.search(html or "")
    if not match:
        return None
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), match.group(1))


@dataclass
class IdxAction:
    """What to do next in an Okta Identity Engine (IDX) sign-in."""

    kind: str  # success | identify | password | code | select | send_email | error | unsupported
    href: Optional[str] = None
    authenticator_id: Optional[str] = None
    method: Optional[str] = None
    with_password: bool = False
    message: Optional[str] = None


def _remediations(response: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    items = ((response or {}).get("remediation") or {}).get("value") or []
    return {r.get("name"): r for r in items if isinstance(r, dict)}


def _field(remediation: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    return next((f for f in remediation.get("value") or [] if f.get("name") == name), None)


def _form_values(node: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    form = ((node or {}).get("form") or {}).get("value") or []
    return {f.get("name"): f.get("value") for f in form if isinstance(f, dict)}


def _select_options(remediation: Dict[str, Any]) -> Dict[str, str]:
    """{methodType: authenticator id} from a select-authenticator remediation."""
    field = _field(remediation, "authenticator") or {}
    options: Dict[str, str] = {}
    for option in field.get("options") or []:
        values = _form_values(option.get("value"))
        if values.get("methodType") and values.get("id"):
            options[str(values["methodType"])] = str(values["id"])
    return options


def _current_authenticator(response: Dict[str, Any]) -> Optional[str]:
    for key in ("currentAuthenticatorEnrollment", "currentAuthenticator"):
        value = ((response or {}).get(key) or {}).get("value") or {}
        if value.get("type"):
            return str(value["type"])
    return None


def idx_error(response: Dict[str, Any]) -> Optional[str]:
    """Okta's error message(s) in an IDX response, if any."""
    messages = ((response or {}).get("messages") or {}).get("value") or []
    errors = [str(m.get("message")) for m in messages if isinstance(m, dict) and m.get("class") == "ERROR"]
    return "; ".join(errors) or None


def idx_next(response: Dict[str, Any], password_used: bool) -> IdxAction:
    """Pick the next IDX step: prefer the password, use email only when Okta insists."""
    error = idx_error(response)
    if error:
        return IdxAction("error", message=error)
    success = (response or {}).get("success") or {}
    if success.get("href"):
        return IdxAction("success", href=success["href"])
    rem = _remediations(response)
    if "identify" in rem:
        fields = {f.get("name") for f in rem["identify"].get("value") or []}
        return IdxAction("identify", href=rem["identify"].get("href"), with_password="credentials" in fields)
    if "challenge-authenticator" in rem:
        kind = _current_authenticator(response)
        href = rem["challenge-authenticator"].get("href")
        if kind == "password":
            return IdxAction("password", href=href)
        if kind == "email":
            return IdxAction("code", href=href, method="email")
        return IdxAction("unsupported", message=f"authenticator {kind}")
    select = rem.get("select-authenticator-authenticate")
    if select:
        options = _select_options(select)
        if "password" in options and not password_used:
            return IdxAction("select", href=select.get("href"), authenticator_id=options["password"], method="password")
        if "email" in options:
            if "authenticator-verification-data" in rem:
                data = rem["authenticator-verification-data"]
                values = _form_values(_field(data, "authenticator"))
                if values.get("id") == options["email"]:
                    return IdxAction("send_email", href=data.get("href"), authenticator_id=options["email"], method="email")
            return IdxAction("select", href=select.get("href"), authenticator_id=options["email"], method="email")
        return IdxAction("unsupported", message=f"authenticators {sorted(options)}")
    if "authenticator-verification-data" in rem:
        data = rem["authenticator-verification-data"]
        values = _form_values(_field(data, "authenticator"))
        return IdxAction("send_email", href=data.get("href"), authenticator_id=values.get("id"), method="email")
    return IdxAction("unsupported", message=f"steps {sorted(n for n in rem if n)}")
