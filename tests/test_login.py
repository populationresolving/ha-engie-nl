"""The sign-in chain (Sitecore + Okta IDX + email code) and API calls against a fake ENGIE."""

import json
import unittest

from aiohttp import ClientSession, CookieJar, web

from _load import load
from test_parse import METERSTANDS, USER

api = load("api")

BUILD = "build-1"
PASSWORD = "good-pw"
CODE = "123456"
EMAIL_ID = "aut-email"
PASSWORD_ID = "aut-password"


def select_remediation(options):
    return {
        "name": "select-authenticator-authenticate",
        "href": "/idp/idx/challenge",
        "value": [
            {"name": "authenticator", "options": [
                {"label": label, "value": {"form": {"value": [{"name": "id", "value": aid}, {"name": "methodType", "value": m}]}}}
                for label, aid, m in options
            ]},
            {"name": "stateHandle", "value": "h", "visible": False},
        ],
    }


AVD = {"name": "authenticator-verification-data", "href": "/idp/idx/challenge",
       "value": [{"name": "authenticator", "form": {"value": [{"name": "id", "value": EMAIL_ID},
                                                               {"name": "methodType", "options": [{"value": "email"}]}]}}]}


def challenge(kind):
    return {"stateHandle": "h", "currentAuthenticatorEnrollment": {"value": {"type": kind}},
            "remediation": {"value": [{"name": "challenge-authenticator", "href": "/idp/idx/challenge/answer",
                                       "value": [{"name": "credentials"}, {"name": "stateHandle"}]}]}}


class FakeEngie:
    def __init__(self) -> None:
        self.site_sessions = set()
        self.okta_sessions = set()
        self.require_email = True
        self.handover_works = True
        self.codes_sent = 0
        self.current = None
        self.password_ok = False
        self.app = web.Application()
        r = self.app.router
        r.add_post("/identity/externallogin", self.externallogin)
        r.add_get("/oauth2/default/v1/authorize", self.authorize)
        r.add_post("/idp/idx/introspect", self.introspect)
        r.add_post("/idp/idx/identify", self.identify)
        r.add_post("/idp/idx/challenge", self.challenge)
        r.add_post("/idp/idx/challenge/answer", self.answer)
        r.add_get("/login/token/redirect", self.token_redirect)
        r.add_post("/authorization-code/okta/callback", self.callback)
        r.add_get("/", self.root)
        r.add_get("/api/app-user", self.guard(lambda q: {"is_app_user": False}))
        r.add_get(f"/_next/data/{BUILD}/contract.json", self.guard(lambda q: {"pageProps": {"data": {"user": USER}}}))
        r.add_get("/api/meterstands", self.guard(lambda q: METERSTANDS))
        r.add_get("/api/consumption", self.guard(lambda q: {"totalConsumptionGas": 60}))
        r.add_get("/api/estimations", self.guard(lambda q: {"amount_seen": q.get("amount")}))
        r.add_get("/api/transactions", self.guard(lambda q: {"transactions": [{"id": "1", "date": "2026-10-01", "amount": 130}]}))
        r.add_get("/api/payments-info", self.guard(lambda q: {"current_debt_amount": 0}))

    # --- Sitecore + Okta
    async def externallogin(self, request):
        raise web.HTTPFound("/oauth2/default/v1/authorize?client_id=c&response_mode=form_post&state=s1")

    def form_post(self):
        return web.Response(content_type="text/html", text=(
            "<html><body onload='document.forms[0].submit()'><form method='POST' action='/authorization-code/okta/callback'>"
            "<input type='hidden' name='code' value='code-1'/><input type='hidden' name='state' value='s1'/></form></body></html>"))

    async def authorize(self, request):
        if request.cookies.get("idx") in self.okta_sessions:
            return self.form_post()
        self.current, self.password_ok = None, False
        return web.Response(content_type="text/html",
                            text='<script>var c={"baseUrl":"x","stateToken":"02.id.abc\\x2Ddef"};</script>')

    async def introspect(self, request):
        body = await request.json()
        assert body["stateToken"] == "02.id.abc-def", body
        return web.json_response({"stateHandle": "h", "remediation": {"value": [
            {"name": "identify", "href": "/idp/idx/identify", "value": [{"name": "identifier"}, {"name": "rememberMe"}]}]}})

    async def identify(self, request):
        body = await request.json()
        assert body["identifier"] == "me@example.com" and body["stateHandle"] == "h"
        return web.json_response({"stateHandle": "h", "currentAuthenticatorEnrollment": {"value": {"type": "email"}},
                                  "remediation": {"value": [AVD, select_remediation([("Email", EMAIL_ID, "email"), ("Password", PASSWORD_ID, "password")])]}})

    async def challenge(self, request):
        auth = (await request.json())["authenticator"]
        if auth["methodType"] == "email":
            assert auth["id"] == EMAIL_ID
            self.codes_sent += 1
        self.current = auth["methodType"]
        return web.json_response(challenge(self.current))

    def success(self):
        resp = web.json_response({"stateHandle": "h", "success": {"name": "success-redirect", "href": "/login/token/redirect?stateToken=t"}})
        sid = f"okta{len(self.okta_sessions) + 1}"
        self.okta_sessions.add(sid)
        resp.set_cookie("idx", sid)
        return resp

    async def answer(self, request):
        passcode = (await request.json())["credentials"]["passcode"]
        if self.current == "password":
            if passcode != PASSWORD:
                return web.json_response({"messages": {"value": [{"class": "ERROR", "message": "Password is incorrect"}]},
                                          **challenge("password")}, status=401)
            self.password_ok = True
            if not self.require_email:
                return self.success()
            return web.json_response({"stateHandle": "h", "currentAuthenticatorEnrollment": {"value": {"type": "email"}},
                                      "remediation": {"value": [AVD, select_remediation([("Email", EMAIL_ID, "email")])]}})
        if self.current == "email" and self.password_ok:
            if passcode != CODE:
                return web.json_response({"messages": {"value": [{"class": "ERROR", "message": "Invalid code"}]},
                                          **challenge("email")}, status=401)
            return self.success()
        raise web.HTTPBadRequest()

    async def token_redirect(self, request):
        return self.form_post()

    async def callback(self, request):
        form = await request.post()
        if form.get("code") != "code-1" or form.get("state") != "s1":
            raise web.HTTPBadRequest()
        sid = f"site{len(self.site_sessions) + 1}"
        if self.handover_works:
            self.site_sessions.add(sid)
        resp = web.HTTPFound("/")
        resp.set_cookie("sess", sid)
        raise resp

    # --- Mijn ENGIE
    def logged_in(self, request) -> bool:
        return request.cookies.get("sess") in self.site_sessions

    async def root(self, request):
        if not self.logged_in(request):
            raise web.HTTPTemporaryRedirect("/authenticatie/inloggen")
        data = json.dumps({"buildId": BUILD})
        return web.Response(content_type="text/html", text=f'<script id="__NEXT_DATA__" type="application/json">{data}</script>')

    def guard(self, payload):
        async def handler(request):
            if not self.logged_in(request):
                return web.json_response({"message": "Unauthenticated."}, status=401)
            return web.json_response(payload(request.query))
        return handler


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeEngie()
        self.runner = web.AppRunner(self.fake.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "localhost", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base = f"http://localhost:{port}"
        self._saved = (api.EXTERNAL_LOGIN_URL, api.MIJN_BASE)
        api.EXTERNAL_LOGIN_URL = f"{base}/identity/externallogin?authenticationType=OktaB2C"
        api.MIJN_BASE = base
        self.sessions = []

    async def asyncTearDown(self):
        api.EXTERNAL_LOGIN_URL, api.MIJN_BASE = self._saved
        for s in self.sessions:
            await s.close()
        await self.runner.cleanup()

    def client(self, password=PASSWORD):
        session = ClientSession(cookie_jar=CookieJar(unsafe=True))
        self.sessions.append(session)
        return api.EngieClient(session, "me@example.com", password)

    async def signed_in_with_code(self):
        client = self.client()
        with self.assertRaises(api.EngieCodeRequired) as ctx:
            await client.async_login(send_code=True)
        self.assertTrue(ctx.exception.sent)
        await client.async_submit_code(CODE)
        return client

    async def test_password_only_policy(self):
        self.fake.require_email = False
        client = self.client()
        await client.async_login()
        data = await client.async_get_data()
        self.assertEqual(data.point("ELK").tariffs["tariff_normal"], 0.30)
        self.assertEqual(data.estimations["amount_seen"], "130")
        self.assertEqual(self.fake.codes_sent, 0)

    async def test_email_code_then_data(self):
        client = self.client()
        with self.assertRaises(api.EngieCodeRequired):
            await client.async_login(send_code=True)
        self.assertEqual(self.fake.codes_sent, 1)
        with self.assertRaises(api.EngieAuthError):
            await client.async_submit_code("000000")  # typo
        await client.async_submit_code(" 123456 ")  # retry works, whitespace ignored
        data = await client.async_get_data()
        self.assertEqual(len(data.metering_points), 2)

    async def test_background_relogin_never_emails(self):
        client = self.client()
        with self.assertRaises(api.EngieCodeRequired) as ctx:
            await client.async_get_data()  # not signed in → unattended login
        self.assertFalse(ctx.exception.sent)
        self.assertEqual(self.fake.codes_sent, 0)

    async def test_okta_session_skips_the_code(self):
        client = await self.signed_in_with_code()
        self.fake.site_sessions.clear()  # Mijn ENGIE session expires, Okta session lives on
        data = await client.async_get_data()
        self.assertEqual(len(data.metering_points), 2)
        self.assertEqual(self.fake.codes_sent, 1)

    async def test_cookies_carry_over_to_a_new_session(self):
        first = await self.signed_in_with_code()
        cookies = first.export_cookies()
        self.assertTrue({"sess", "idx"} <= {c["name"] for c in cookies})
        second = self.client()
        second.import_cookies(json.loads(json.dumps(cookies)))  # as stored in the entry
        await second.async_get_data()  # Mijn ENGIE session still valid: no login at all
        self.fake.site_sessions.clear()
        await second.async_get_data()  # re-login through the Okta session, no code
        self.assertEqual(self.fake.codes_sent, 1)

    async def test_wrong_password(self):
        client = self.client(password="bad")
        with self.assertRaises(api.EngieAuthError) as ctx:
            await client.async_login()
        self.assertNotIsInstance(ctx.exception, api.EngieCodeRequired)
        self.assertIn("Password is incorrect", str(ctx.exception))
        self.assertNotIn("bad", str(ctx.exception).replace("incorrect", ""))
        self.assertEqual(self.fake.codes_sent, 0)


    async def test_handover_failure_is_not_blamed_on_the_code(self):
        self.fake.handover_works = False
        client = self.client()
        with self.assertRaises(api.EngieCodeRequired):
            await client.async_login(send_code=True)
        with self.assertLogs("engie_nl.api", level="WARNING") as logs:
            with self.assertRaises(api.EngieSessionError):
                await client.async_submit_code(CODE)
        text = "\n".join(logs.output)
        self.assertIn("Hops:", text)
        self.assertIn("authorization-code/okta/callback", text)
        self.assertIn("sess@", text)  # cookie names, ...
        self.assertNotIn("site1", text)  # ... never values
        self.assertNotIn("code-1", text)


if __name__ == "__main__":
    unittest.main()
