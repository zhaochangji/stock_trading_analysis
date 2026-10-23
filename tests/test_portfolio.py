"""账户资产实时估值测试。"""

import unittest

from quant.portfolio import calculate_portfolio, infer_legacy_cash_balance


class TestPortfolioValuation(unittest.TestCase):
    def setUp(self):
        self.holdings = [{"code": "000001", "cost": 10, "shares": 100}]

    def test_market_price_changes_total_assets(self):
        first = calculate_portfolio(self.holdings, {"000001": 10}, cash_balance=1000)
        second = calculate_portfolio(self.holdings, {"000001": 11}, cash_balance=1000)

        self.assertEqual(first["total_assets"], 2000)
        self.assertEqual(second["total_assets"], 2100)
        self.assertEqual(second["positions"]["000001"]["market_value"], 1100)

    def test_missing_quote_does_not_fabricate_total_assets(self):
        result = calculate_portfolio(self.holdings, {}, cash_balance=1000)

        self.assertIsNone(result["total_assets"])
        self.assertEqual(result["missing_codes"], ["000001"])
        self.assertIsNone(result["market_value"])

    def test_legacy_total_is_migrated_to_cash_once(self):
        cash = infer_legacy_cash_balance(
            self.holdings, {"000001": 10}, legacy_total_assets=2000)

        self.assertEqual(cash, 1000)
        later = calculate_portfolio(self.holdings, {"000001": 11}, cash_balance=cash)
        self.assertEqual(later["total_assets"], 2100)

    def test_legacy_migration_requires_complete_valid_quotes(self):
        self.assertIsNone(infer_legacy_cash_balance(
            self.holdings, {}, legacy_total_assets=2000))
        self.assertIsNone(infer_legacy_cash_balance(
            self.holdings, {"000001": 10}, legacy_total_assets=500))

    def test_unrealized_profit_uses_market_value_minus_cost(self):
        result = calculate_portfolio(self.holdings, {"000001": 12}, cash_balance=0)

        self.assertEqual(result["unrealized_pnl"], 200)
        self.assertAlmostEqual(result["positions"]["000001"]["profit_pct"], 20)


if __name__ == "__main__":
    unittest.main()

