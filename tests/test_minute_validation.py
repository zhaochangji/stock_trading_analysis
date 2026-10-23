"""单只股票分钟成交时刻校验测试；网络调用全部模拟。"""

from unittest import TestCase
from unittest.mock import Mock, patch

from quant.datasource.minute_validation import last_minute_bar


class TestMinuteValidation(TestCase):
    @patch("quant.datasource.minute_validation.requests.get")
    def test_skips_zero_volume_and_returns_last_traded_minute(self, get):
        response = Mock()
        response.json.return_value = {"data": {"code": "002815", "trends": [
            "2026-10-16 14:59,23.47,23.47,23.47,23.47,0,0.00,24.539",
            "2026-10-16 15:00,23.47,23.47,23.47,23.47,11144,26154968.00,24.533",
        ]}}
        get.return_value = response
        self.assertEqual(last_minute_bar("002815"),
                         {"time": "2026-10-16T15:00", "close": 23.47})
        self.assertEqual(get.call_args.kwargs["params"]["secid"], "0.002815")

    @patch("quant.datasource.minute_validation.requests.get")
    def test_rejects_wrong_stock_or_missing_trade(self, get):
        response = Mock()
        response.json.return_value = {"data": {"code": "999999", "trends": [
            "2026-10-16 15:00,23.47,23.47,23.47,23.47,11144,26154968.00,24.533"]}}
        get.return_value = response
        self.assertIsNone(last_minute_bar("002815"))
        response.json.return_value["data"]["code"] = "002815"
        response.json.return_value["data"]["trends"] = [
            "2026-10-16 14:59,23.47,23.47,23.47,23.47,0,0.00,24.539"]
        self.assertIsNone(last_minute_bar("002815"))
