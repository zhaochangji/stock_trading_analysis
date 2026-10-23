"""持仓纪律规则回归测试。"""

import unittest

from quant.rules import assess_holding, stop_take_prices


class TestStopLoss(unittest.TestCase):
    def setUp(self):
        self.holding = {"code": "002815", "name": "崇达技术",
                        "cost": 24.311, "shares": 200}

    def test_triggers_technical_stop_on_ma20_break(self):
        stop = stop_take_prices(self.holding, ma20=22.74)
        self.assertEqual(stop["stop"], 22.74)
        above = assess_holding(self.holding, 23.29, ma20=22.74)
        below = assess_holding(self.holding, 22.73, ma20=22.74)
        self.assertFalse(any(rule == "止损" for _, rule, _ in above))
        self.assertTrue(any(level == "DISCIPLINE" and rule == "止损"
                            for level, rule, _ in below))

    def test_hard_stop_still_triggers_without_technical_stop(self):
        stop = stop_take_prices(self.holding, ma20=21.5)
        self.assertEqual(stop["stop"], 22.61)
        findings = assess_holding(self.holding, 22.6, ma20=21.5)
        self.assertTrue(any(level == "DISCIPLINE" and rule == "止损"
                            for level, rule, _ in findings))

    def test_stop_calc_matches_assessment_when_price_below_ma20(self):
        for price in (23.29, 22.73, 22.60):
            for ma20 in (21.5, 22.74, 23.8):
                with self.subTest(price=price, ma20=ma20):
                    stop = stop_take_prices(self.holding, ma20=ma20)["stop"]
                    findings = assess_holding(self.holding, price, ma20=ma20)
                    triggered = any(rule == "止损" for _, rule, _ in findings)
                    self.assertEqual(triggered, price <= stop)


if __name__ == "__main__":
    unittest.main()
