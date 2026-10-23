"""买点分析规则、价格线、时间与 HTML 安全回归（不依赖外网/数据库）。"""

import datetime as dt
import unittest
from unittest.mock import patch

import pandas as pd

from quant.analysis import assess_entry, technical_snapshot
from quant.analysis_view import empty_page, news_link, render_analysis


NOW = dt.datetime(2026, 10, 16, 14, 30)


def minute_frame(n=240, base=23.0):
    """构造通达信分时帧(price/vol)。"""
    step = (n - 1) / 80.0 if n > 1 else 1.0
    return pd.DataFrame({
        "price": [base + 0.5 * (i % 30) / 30.0 for i in range(n)],
        "vol": [1000.0 + i * 10 for i in range(n)],
    })


class patch_minute:
    """patch 通达信分时缓存入口(frame=None 表示降级)。"""

    def __init__(self, frame):
        self.frame = frame

    def __enter__(self):
        from quant.datasource import tdx
        self._cm = patch.object(tdx, "fetch_minute_cached",
                                return_value=self.frame)
        return self._cm.__enter__()

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


def facts(**changes):
    base = {
        "code": "002815", "name": "崇达技术", "spot_date": "2026-10-16",
        "quote_date": "2026-10-16", "quote_at": "2026-10-16T14:29:30",
        "quote_trade_at": "2026-10-16T14:29:10", "completed_at": "2026-10-16T14:29:55",
        "quote": {"last_price": 23.12, "price_change_ratio_pct": 1.0,
                  "high_price": 23.4, "turnover": 2e8},
        "tech": {"hist_date": "2026-10-15", "indicator_date": "2026-10-16",
                 "quote_merged": True, "ma10": 23.00, "ma20": 22.70, "rsi14": 53,
                 "dif": 1.2, "dea": 1.1, "vr": 2.0, "high20_prev": 24.0},
        "sector": {"name": "元件", "source": "THS指数实时", "pct": 2,
                   "resonant": True}, "sector_at": "2026-10-16T14:29:30",
        "ths_flow": {"net": 2e8}, "ths_flow_at": "2026-10-16T14:29:20",
        "em_flow": {"main_net": 1e8, "last_time": "2026-10-16 14:29"},
        "news_status": "ok", "news": [
            {"time": "2026-10-16 12:00", "title": "崇达技术订单公告", "source": "交易所", "tag": "中性",
             "url": "https://example.org/notice"}],
    }
    base.update(changes)
    return base


class TestEntryAnalysis(unittest.TestCase):
    def test_all_confirmations_and_plan(self):
        result = assess_entry(facts(), now=NOW, total_assets=30000)
        self.assertTrue(result["ready"])
        self.assertEqual((result["plan"]["low"], result["plan"]["high"]),
                         (23.0, 23.46))
        self.assertLess(result["plan"]["stop"], result["plan"]["reference"])
        self.assertGreater(result["plan"]["tp1"], result["plan"]["reference"])
        self.assertGreater(result["plan"]["tp2"], result["plan"]["tp1"])

    def test_missing_assets_blocks_confirmed_buy(self):
        result = assess_entry(facts(), now=NOW)
        self.assertFalse(result["ready"])
        self.assertIn("总资产未配置", result["verdict"])

    def test_after_close_is_never_today_buy(self):
        result = assess_entry(facts(), now=dt.datetime(2026, 10, 16, 15, 1))
        self.assertFalse(result["ready"])
        self.assertIn("今日不可买入", result["verdict"])

    def test_after_close_last_traded_minute_may_support_research_plan(self):
        after_close = facts(quote_at="2026-10-16T16:00:00",
                            quote_trade_at="2026-10-16T15:00")
        result = assess_entry(after_close, now=dt.datetime(2026, 10, 16, 16, 0))
        self.assertFalse(result["ready"])
        self.assertIsNotNone(result["plan"])
        self.assertTrue(next(x for x in result["checks"]
                             if x["label"] == "可核对的成交时刻")["ok"])

    def test_next_day_same_stale_quote_must_not_offer_plan(self):
        result = assess_entry(facts(quote_at="2026-10-17T10:00:00",
                                    quote_trade_at="2026-10-16T15:00"),
                              now=dt.datetime(2026, 10, 17, 10, 0))
        self.assertIsNone(result["plan"])

    def test_quote_without_trade_timestamp_blocks_confirmed_buy(self):
        result = assess_entry(facts(quote_trade_at=None), now=NOW)
        self.assertFalse(result["ready"])
        self.assertIsNone(result["plan"])
        self.assertFalse(next(x for x in result["checks"]
                              if x["label"] == "可核对的成交时刻")["ok"])

    def test_unverified_quote_date_does_not_offer_price(self):
        result = assess_entry(facts(quote_date=None), now=NOW)
        self.assertFalse(result["ready"])
        self.assertIsNone(result["plan"])

    def test_old_sample_and_history_do_not_offer_price(self):
        old = facts(quote_at="2026-10-15T14:29:30", spot_date="2026-10-15")
        self.assertIsNone(assess_entry(old, now=NOW)["plan"])

    def test_conflicting_funds_and_weak_sector_block_buy(self):
        f = facts(ths_flow={"net": -2e8},
                  sector={"name": "元件", "source": "THS指数实时", "resonant": False})
        result = assess_entry(f, now=NOW)
        self.assertFalse(result["ready"])
        self.assertIn("THS 全单 -2.00", next(x["detail"] for x in result["checks"]
                                               if x["label"] == "资金双口径"))

    def test_losing_holding_cannot_add(self):
        result = assess_entry(facts(), now=NOW, holding={"cost": 24.0})
        self.assertFalse(result["ready"])
        self.assertIsNotNone(result["holding_levels"])

    def test_news_missing_or_negative_blocks_buy(self):
        self.assertFalse(assess_entry(facts(news_status="error", news=[]), now=NOW)["ready"])
        negative = [{"time": "2026-10-16 12:00", "title": "崇达技术减持", "tag": "利空"}]
        self.assertFalse(assess_entry(facts(news=negative), now=NOW)["ready"])

    def test_unrelated_market_news_cannot_confirm_stock_news(self):
        unrelated = [{"time": "2026-10-16 12:00", "title": "大盘上涨", "tag": "中性"}]
        result = assess_entry(facts(news=unrelated), now=NOW)
        self.assertFalse(next(x for x in result["checks"]
                              if x["label"] == "消息核对")["ok"])

    def test_chinext_without_confirmed_permission_does_not_buy(self):
        self.assertFalse(assess_entry(facts(code="300001"), now=NOW)["ready"])

    def test_technical_snapshot_rejects_mismatched_previous_close(self):
        rows = [{"date": (dt.date(2026, 7, 1) + dt.timedelta(days=i)).isoformat(),
                 "close": 10 + i * .02, "high": 10.1 + i * .02,
                 "low": 9.9 + i * .02, "volume": 1000000} for i in range(80)]
        quote = {"last_price": 12, "prev_price": 99, "volume": 2000000}
        snapshot = technical_snapshot(rows, quote, dt.date(2026, 10, 16))
        self.assertFalse(snapshot["quote_merged"])

    def test_bad_news_url_does_not_break_render_or_create_link(self):
        self.assertNotIn("href=", news_link({"title": "崇达技术公告", "url": "https://example.com:bad"}))
        self.assertNotIn("href=", news_link({"title": "崇达技术公告", "url": "https://user@example.com/x"}))
        self.assertIn("href=", news_link({"title": "崇达技术公告", "url": "https://example.com/x"}))

    def test_untrusted_news_and_search_input_are_escaped(self):
        f = facts(news=[{"time": "2026-10-16 10:00", "source": "<script>x</script>",
                         "title": "<img onerror=alert(1)>", "tag": "中性",
                         "url": "javascript:alert(1)"}])
        # 分时卡走通达信(不连网):patch 缓存入口,防测试真连行情服务器。
        with patch_minute(None):
            markup = render_analysis(f, now=NOW)
        self.assertNotIn("<script>x</script>", markup)
        self.assertNotIn("<img onerror=alert(1)>", markup)
        self.assertNotIn('href="javascript:', markup)
        self.assertIn("&lt;img", markup)
        self.assertIn("&lt;script&gt;", empty_page("<script>"))
        # 分时降级文案也不得破坏页面结构。
        self.assertIn("分时走势", markup)


class TestMinuteChartSection(unittest.TestCase):
    """分析页分时卡:SVG 渲染、昨收基准、降级(不连网)。"""

    def test_renders_svg_with_prev_close_base(self):
        from quant.analysis_view import minute_chart_section
        today = dt.date.today().strftime("%Y-%m-%d")
        with patch_minute(minute_frame(240)), \
                patch("quant.analysis_view._spot_prev_close",
                      return_value=(23.0, today)):
            markup = minute_chart_section("002815", "崇达技术")
        self.assertIn("<svg", markup)
        self.assertIn("polyline", markup)          # 价格线 + 均价线
        self.assertIn("昨收 23.00", markup)         # 基准虚线标注
        self.assertIn("15:00", markup)              # 240 条 → 收盘刻度
        self.assertIn("均价", markup)
        self.assertIn("通达信分时", markup)

    def test_prev_close_not_today_omits_base_line(self):
        from quant.analysis_view import minute_chart_section
        with patch_minute(minute_frame(120)), \
                patch("quant.analysis_view._spot_prev_close",
                      return_value=(23.0, "2020-01-01")):
            markup = minute_chart_section("002815", "崇达技术")
        self.assertIn("<svg", markup)
        self.assertNotIn("昨收 23.00", markup)      # 非当日快照不画基准线标注
        self.assertNotIn("stroke-dasharray", markup)
        self.assertIn("未画基准线", markup)

    def test_degrades_without_breaking_page(self):
        from quant.analysis_view import minute_chart_section
        with patch_minute(None):
            markup = minute_chart_section("002815", "崇达技术")
        self.assertIn("分时走势", markup)
        self.assertIn("暂不可用", markup)
        self.assertNotIn("<svg", markup)
        # 采样不足同样降级
        frame = pd.DataFrame({"price": [23.0], "vol": [100.0]})
        with patch_minute(frame):
            markup = minute_chart_section("002815")
        self.assertIn("采样不足", markup)


if __name__ == "__main__":
    unittest.main()
