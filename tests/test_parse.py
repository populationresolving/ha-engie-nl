"""Payload parsing and statistics maths (synthetic data shaped like Mijn ENGIE's)."""

import unittest
from datetime import date

from _load import load

parse = load("parse")

USER = {
    "eans": ["111", "222"],
    "selectedAddress": {
        "id": "addr-1",
        "metering_points": [
            {
                "ean": "111", "type": "ELK", "smart": True, "single_tariff": False,
                "sjv_normal": 1500, "sjv_low": 1200, "sjv_single": 0,
                "tariffs": {"tariff_normal": 0.30, "tariff_low": 0.28, "fixed_charge": 0.2, "tax_credit": -1.7},
                "current_product": {"name": "Vast 1 jaar", "product_type": "nondynamisch",
                                     "start_date": "2026-01-01T00:00:00", "end_date": "2027-01-01T00:00:00"},
                "prepayment_amount": 80, "next_meter_request_date": "2026-12-01",
            },
            {
                "ean": "222", "type": "GAS", "smart": True, "single_tariff": True, "sjv_single": 900,
                "tariffs": {"tariff_single": 1.40, "fixed_charge": 0.2},
                "current_product": {"name": "Vast 1 jaar gas", "end_date": "2026-12-31"},
                "prepayment_amount": 50,
            },
        ],
    },
}

METERSTANDS = [
    {"ean": "111", "data": [
        {"type": "E", "name": "Laag", "readings": [
            {"date": "2026-11-01", "value": 1100.0, "source": "AUTOMATICALLY"},
            {"date": "2026-10-02", "value": 1000.0, "source": "AUTOMATICALLY"}]},
        {"type": "E", "name": "Hoog", "readings": [{"date": "2026-11-01", "value": 2000}]},
        {"type": "E", "name": "Teruglevering laag", "readings": [{"date": "2026-11-01", "value": 5}]},
        {"type": "E", "name": "Onbekend", "readings": [{"date": "2026-11-01", "value": 1}]},
    ]},
    {"ean": "222", "data": [{"type": "G", "name": "Hoog", "readings": [{"date": "2026-10-02", "value": "750.5"}]}]},
]


class ParseTests(unittest.TestCase):
    def test_metering_points(self):
        elk, gas = parse.parse_metering_points(USER)
        self.assertEqual((elk.kind, gas.kind), ("ELK", "GAS"))
        self.assertEqual(elk.tariffs["tariff_normal"], 0.30)
        self.assertEqual(elk.contract_end, date(2027, 1, 1))
        self.assertEqual(elk.yearly_estimate["normal"], 1500)
        self.assertEqual(gas.prepayment_amount, 50)
        self.assertEqual(elk.next_meter_request_date, date(2026, 12, 1))

    def test_readings_sorted_and_mapped(self):
        r = parse.parse_meter_readings(METERSTANDS)
        self.assertEqual(set(r), {"electricity_low", "electricity_normal", "return_low", "gas"})
        self.assertEqual([x.value for x in r["electricity_low"]], [1000.0, 1100.0])
        self.assertEqual(r["gas"][0].value, 750.5)

    def test_merge_keeps_history(self):
        first = parse.merge_readings({}, parse.parse_meter_readings(METERSTANDS))
        newer = parse.parse_meter_readings([{"data": [{"type": "E", "name": "Laag", "readings": [
            {"date": "2026-12-01", "value": 1190}, {"date": "2026-11-01", "value": 1100}]}]}])
        merged = parse.merge_readings(first, newer)
        self.assertEqual(merged["electricity_low"], [["2026-10-02", 1000.0], ["2026-11-01", 1100.0], ["2026-12-01", 1190.0]])
        self.assertEqual(merged["gas"], first["gas"])

    def test_cumulative_points_with_cost_and_reset(self):
        pts = parse.cumulative_points([["2026-10-01", 1000], ["2026-11-01", 1100], ["2026-12-01", 50], ["2027-01-01", 80]], 0.3)
        self.assertEqual([p[2] for p in pts], [0.0, 100.0, 100.0, 130.0])  # meter swap starts a new baseline
        self.assertAlmostEqual(pts[-1][3], 39.0)
        self.assertIsNone(parse.cumulative_points([["2026-10-01", 1]])[0][3])

    def test_daily_usage_from_month_totals(self):
        user = {"selectedAddress": {"metering_points": [
            dict(USER["selectedAddress"]["metering_points"][0], data_to="2026-11-10T00:00:00+01:00"),
            dict(USER["selectedAddress"]["metering_points"][1], data_to="2026-11-10T00:00:00+01:00")]}}
        points = parse.parse_metering_points(user)
        self.assertEqual(parse.covered_through(points, date(2026, 11, 10)), date(2026, 11, 9))
        self.assertEqual(parse.covered_through(parse.parse_metering_points(USER), date(2026, 11, 10)), date(2026, 11, 9))

        def snap(history, day, month, power, gas, back=0.0):
            payload = {"year": 2026, "month": month, "totalConsumptionPower": power,
                       "totalRedeliveryPower": back, "totalConsumptionGas": gas}
            return parse.merge_month_totals(history, payload, day)

        h = snap({}, date(2026, 11, 9), 11, 42.5, 11)
        self.assertEqual(parse.daily_usage(h["electricity"]), [])  # first snapshot = baseline only
        h = snap(h, date(2026, 11, 9), 11, 42.6, 11)               # same day again: latest wins
        self.assertEqual(len(h["electricity"]), 1)
        h = snap(h, date(2026, 11, 10), 11, 50.6, 14)
        h = snap(h, date(2026, 11, 13), 11, 59.6, 20)              # two days skipped → averaged over 3
        h = snap(h, date(2026, 11, 14), 11, 58.0, 20)              # correction downwards: no negative day
        h = parse.merge_month_totals(h, None, date(2026, 11, 15))  # failed call: nothing stored
        h = parse.merge_month_totals(h, {"year": 2026, "month": 11, "totalConsumptionPower": -1}, date(2026, 11, 15))
        h = snap(h, date(2026, 11, 30), 11, 90.0, 40)
        h = snap(h, date(2026, 11, 30), 12, 0.0, 0)                # 1 Dec: new month, nothing covered yet
        h = snap(h, date(2026, 12, 1), 12, 7.5, 3)
        self.assertEqual(h["electricity"][-2], ["2026-11-30", "2026-12", 0.0])
        days = {d.isoformat(): (v, n) for d, v, n in parse.daily_usage(h["electricity"])}
        self.assertEqual(days["2026-11-10"], (8.0, 1))
        self.assertEqual([days[f"2026-11-{d}"] for d in (11, 12, 13)], [(3.0, 3)] * 3)
        self.assertNotIn("2026-11-14", days)
        self.assertEqual(days["2026-11-30"], (2.0, 16))            # 32 kWh over 15..30 Nov
        self.assertEqual(days["2026-12-01"], (7.5, 1))
        self.assertEqual({d.isoformat(): v for d, v, _ in parse.daily_usage(h["gas"])}["2026-11-10"], 3.0)

    def test_daily_usage_month_rollover_with_gap(self):
        h = [["2026-11-28", "2026-11", 80.0], ["2026-12-02", "2026-12", 9.0]]
        days = {d.isoformat(): (v, n) for d, v, n in parse.daily_usage(h)}
        self.assertEqual(days, {"2026-12-01": (4.5, 2), "2026-12-02": (4.5, 2)})  # 29–30 Nov unknown, not invented

    def test_daily_costs(self):
        points = parse.parse_metering_points(USER)
        prices = parse.daily_prices(points)
        self.assertAlmostEqual(prices["electricity"], 0.29)
        self.assertAlmostEqual(prices["gas"], 1.40)
        self.assertAlmostEqual(prices["fixed"], 0.2 - 1.7 + 0.2)
        usage = {"electricity": [(date(2026, 11, 10), 10.0, 1), (date(2026, 11, 11), 5.0, 1)],
                 "gas": [(date(2026, 11, 10), 2.0, 1)]}
        costs = parse.daily_costs(usage, prices)
        self.assertEqual([d for d, _ in costs], [date(2026, 11, 10)])  # 11 Nov has no gas value → no half bill
        self.assertAlmostEqual(costs[0][1], 10 * 0.29 + 2 * 1.40 - 1.3)
        self.assertEqual(parse.running_sum([(date(2026, 11, 10), 1.5), (date(2026, 11, 11), 2.0)])[-1][2], 3.5)

    def test_build_id_and_forms(self):
        html = '<html><script id="__NEXT_DATA__" type="application/json">{"buildId":"abc-123","page":"/"}</script></html>'
        self.assertEqual(parse.extract_build_id(html), "abc-123")
        self.assertIsNone(parse.extract_build_id("<html></html>"))
        okta = ('<form method="POST" action="https://www.engie.nl/authorization-code/okta/callback">'
                '<input type="hidden" name="code" value="c&amp;1"/><input type="hidden" name="state" value="s"/></form>'
                '<form method="get" action="/x"><input name="q"></form>')
        form = parse.auto_submit_form(okta)
        self.assertEqual(form.action, "https://www.engie.nl/authorization-code/okta/callback")
        self.assertEqual(form.inputs, {"code": "c&1", "state": "s"})
        self.assertIsNone(parse.auto_submit_form('<form method="post"><input name="username"></form>'))


    def test_state_token_unescaped(self):
        html = '<script>var x={"stateToken":"02.id.ab\\x2Dcd"};</script>'
        self.assertEqual(parse.extract_state_token(html), "02.id.ab-cd")
        self.assertIsNone(parse.extract_state_token("<html></html>"))

    def test_idx_prefers_password_then_email(self):
        sel = {"name": "select-authenticator-authenticate", "href": "/c", "value": [{"name": "authenticator", "options": [
            {"value": {"form": {"value": [{"name": "id", "value": "e1"}, {"name": "methodType", "value": "email"}]}}},
            {"value": {"form": {"value": [{"name": "id", "value": "p1"}, {"name": "methodType", "value": "password"}]}}}]}]}
        avd = {"name": "authenticator-verification-data", "href": "/c", "value": [
            {"name": "authenticator", "form": {"value": [{"name": "id", "value": "e1"}, {"name": "methodType"}]}}]}
        resp = {"remediation": {"value": [avd, sel]}}
        first = parse.idx_next(resp, password_used=False)
        self.assertEqual((first.kind, first.authenticator_id, first.method), ("select", "p1", "password"))
        then = parse.idx_next(resp, password_used=True)
        self.assertEqual((then.kind, then.authenticator_id), ("send_email", "e1"))
        err = parse.idx_next({"messages": {"value": [{"class": "ERROR", "message": "Nope"}]}}, False)
        self.assertEqual((err.kind, err.message), ("error", "Nope"))
        ok = parse.idx_next({"success": {"href": "/login/token/redirect?x"}}, True)
        self.assertEqual(ok.kind, "success")
        code = parse.idx_next({"currentAuthenticatorEnrollment": {"value": {"type": "email"}},
                               "remediation": {"value": [{"name": "challenge-authenticator", "href": "/a"}]}}, True)
        self.assertEqual(code.kind, "code")


if __name__ == "__main__":
    unittest.main()
