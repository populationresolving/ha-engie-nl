"""The headless sign-in chain and API calls against a fake ENGIE (Okta + Sitecore + Mijn ENGIE)."""

import asyncio
import json
import unittest

from aiohttp import ClientSession, CookieJar, web

from _load import load
from test_parse import METERSTANDS, USER

api = load("api")

BUILD = "build-1"


class FakeEngie:
    def __init__(self) -> None:
        self.sessions = set()
        self.mfa = False
        self.authn_calls = 0
        self.app = web.Application()
        r = self.app.router
        r.add_post("/api/v1/authn", self.authn)
        r.add_post("/identity/externallogin", self.externallogin)
        r.add_get("/oauth2/default/v1/authorize", self.authorize)
        r.add_post("/authorization-code/okta/callback", self.callback)
        r.add_get("/", self.root)
        r.add_get("/api/app-user", self.guard(lambda q: {"is_app_user": False}))
        r.add_get(f"/_next/data/{BUILD}/contract.json", self.guard(lambda q: {"pageProps": {"data": {"user": USER}}}))
        r.add_get("/api/meterstands", self.guard(lambda q: METERSTANDS))
        r.add_get("/api/consumption", self.guard(lambda q: {"year": 2026, "month": 11, "totalConsumptionPower": 120,
                                                            "totalRedeliveryPower": 3, "totalConsumptionGas": 60}))
        r.add_get("/api/estimations", self.guard(lambda q: {"total_estimated_nota_amount": 1500, "amount_seen": q.get("amount")}))
        r.add_get("/api/transactions", self.guard(lambda q: {"transactions": [{"id": "1", "date": "2026-10-01", "amount": 130}]}))
        r.add_get("/api/payments-info", self.guard(lambda q: {"current_debt_amount": 0}))

    async def authn(self, request):
        self.authn_calls += 1
        body = await request.json()
        if body.get("password") != "good-pw":
            return web.json_response({"errorCode": "E0000004", "errorSummary": "Authentication failed"}, status=401)
        if self.mfa:
            return web.json_response({"status": "MFA_REQUIRED"})
        return web.json_response({"status": "SUCCESS", "sessionToken": "tok+/="})

    async def externallogin(self, request):
        raise web.HTTPFound("/oauth2/default/v1/authorize?client_id=c&response_mode=form_post&state=s1")

    async def authorize(self, request):
        if request.query.get("sessionToken") != "tok+/=":
            return web.Response(text="<html><form method='post'><input name='identifier'></form></html>", content_type="text/html")
        html = ("<html><body onload='document.forms[0].submit()'>"
                "<form method='POST' action='/authorization-code/okta/callback'>"
                "<input type='hidden' name='code' value='code-1'/><input type='hidden' name='state' value='s1'/>"
                "</form></body></html>")
        return web.Response(text=html, content_type="text/html")

    async def callback(self, request):
        form = await request.post()
        if form.get("code") != "code-1" or form.get("state") != "s1":
            raise web.HTTPBadRequest()
        sid = f"sid{len(self.sessions) + 1}"
        self.sessions.add(sid)
        resp = web.HTTPFound("/")
        resp.set_cookie("sess", sid)
        raise resp

    def logged_in(self, request) -> bool:
        return request.cookies.get("sess") in self.sessions

    async def root(self, request):
        if not self.logged_in(request):
            raise web.HTTPTemporaryRedirect("/authenticatie/inloggen")
        data = json.dumps({"buildId": BUILD, "page": "/"})
        return web.Response(text=f'<html><script id="__NEXT_DATA__" type="application/json">{data}</script></html>',
                            content_type="text/html")

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
        self._saved = (api.AUTHN_URL, api.EXTERNAL_LOGIN_URL, api.MIJN_BASE)
        api.AUTHN_URL = f"{base}/api/v1/authn"
        api.EXTERNAL_LOGIN_URL = f"{base}/identity/externallogin?authenticationType=OktaB2C"
        api.MIJN_BASE = base
        self.session = ClientSession(cookie_jar=CookieJar(unsafe=True))

    async def asyncTearDown(self):
        api.AUTHN_URL, api.EXTERNAL_LOGIN_URL, api.MIJN_BASE = self._saved
        await self.session.close()
        await self.runner.cleanup()

    async def test_full_refresh(self):
        client = api.EngieClient(self.session, "me@example.com", "good-pw")
        data = await client.async_get_data()
        self.assertEqual([p.kind for p in data.metering_points], ["ELK", "GAS"])
        self.assertEqual(data.point("ELK").tariffs["tariff_normal"], 0.30)
        self.assertEqual(data.readings["electricity_low"][-1].value, 1100.0)
        self.assertEqual(data.consumption["totalConsumptionGas"], 60)
        self.assertEqual(data.estimations["amount_seen"], "130")  # sum of the advance payments
        self.assertEqual(data.transactions[0]["amount"], 130)
        self.assertEqual(data.flags, {k: None for k in data.flags})
        self.assertEqual(self.fake.authn_calls, 1)

    async def test_expired_session_signs_in_again_once(self):
        client = api.EngieClient(self.session, "me@example.com", "good-pw")
        await client.async_get_data()
        self.fake.sessions.clear()  # server forgets the session
        data = await client.async_get_data()
        self.assertEqual(len(data.metering_points), 2)
        self.assertEqual(self.fake.authn_calls, 2)

    async def test_wrong_password(self):
        client = api.EngieClient(self.session, "me@example.com", "bad")
        with self.assertRaises(api.EngieAuthError) as ctx:
            await client.async_login()
        self.assertNotIsInstance(ctx.exception, api.EngieMfaRequired)
        self.assertNotIn("bad", str(ctx.exception))

    async def test_mfa(self):
        self.fake.mfa = True
        client = api.EngieClient(self.session, "me@example.com", "good-pw")
        with self.assertRaises(api.EngieMfaRequired):
            await client.async_login()


if __name__ == "__main__":
    unittest.main()
