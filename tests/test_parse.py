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


if __name__ == "__main__":
    unittest.main()
