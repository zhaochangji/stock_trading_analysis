"""Tests for lightweight portfolio endpoints and persistence."""

"""Tests for the lightweight positions-data endpoint."""

import json
import os
import threading
import unittest
from unittest.mock import patch

import pandas as pd
import tornado.testing

import scripts.quant_web as quant_web


class PortfolioCacheTest(unittest.TestCase):
    def test_invalidated_background_refresh_cannot_write_stale_value(self):
        key = "cache-generation-test"
        started, release = threading.Event(), threading.Event()
        with quant_web._CACHE_LOCK:
            quant_web._CACHE.pop(key, None)
            quant_web._CACHE_GENERATION.pop(key, None)
            quant_web._CACHE_RETRY_AFTER.pop(key, None)
            quant_web._CACHE_REFRESHING.discard(key)
        future = None
        try:
            def fetch():
                started.set()
                release.wait(timeout=2)
                return "stale"

            with quant_web._CACHE_LOCK:
                generation = quant_web._CACHE_GENERATION.get(key, 0)
                quant_web._CACHE_REFRESHING.add(key)
            future = quant_web._CACHE_EXECUTOR.submit(
                quant_web._refresh_cache, key, fetch, generation)
            self.assertTrue(started.wait(timeout=2))
            quant_web.invalidate_cache(key)
            release.set()
            future.result(timeout=2)
            self.assertNotIn(key, quant_web._CACHE)
        finally:
            release.set()
            if future is not None:
                future.result(timeout=2)
            with quant_web._CACHE_LOCK:
                quant_web._CACHE.pop(key, None)
                quant_web._CACHE_GENERATION.pop(key, None)
                quant_web._CACHE_RETRY_AFTER.pop(key, None)
                quant_web._CACHE_REFRESHING.discard(key)

    def test_snapshot_refresh_invalidates_derived_page_caches(self):
        keys = ("snap", "cards", "main", "positions", "stock:000001")
        with quant_web._CACHE_LOCK:
            for key in keys:
                quant_web._CACHE[key] = (0, "old")
                quant_web._CACHE_GENERATION.setdefault(key, 0)
        try:
            generation = quant_web._CACHE_GENERATION["snap"]
            quant_web._refresh_cache("snap", lambda: "new", generation)
            with quant_web._CACHE_LOCK:
                self.assertEqual(quant_web._CACHE["snap"][1], "new")
                for key in keys[1:]:
                    self.assertNotIn(key, quant_web._CACHE)
        finally:
            with quant_web._CACHE_LOCK:
                for key in keys:
                    quant_web._CACHE.pop(key, None)
                    quant_web._CACHE_GENERATION.pop(key, None)
                    quant_web._CACHE_REFRESHING.discard(key)


class PaperDashboardHelpersTest(unittest.TestCase):
    def test_slot_result_and_win_rate_labels(self):
        self.assertEqual(quant_web._paper_slot_label("auction"), "竞价")
        self.assertEqual(quant_web._paper_slot_label("auction_back"), "竞价备选")
        self.assertEqual(quant_web._paper_slot_label("tail"), "尾盘")
        self.assertEqual(quant_web._paper_result_label(1), "胜")
        self.assertEqual(quant_web._paper_result_label(0), "负")
        self.assertEqual(quant_web._paper_result_label(None), "待更新")
        self.assertEqual(quant_web._paper_row_result(None, 1), "备选(未买入)")
        self.assertEqual(quant_web._paper_row_result(6.94, None), "待更新")

        frame = pd.DataFrame({"win_t1": [1, 0, None]})
        summary = quant_web._paper_t1_summary(frame)
        self.assertIn("50.0%", summary)
        self.assertIn("已完成 2 条", summary)
        self.assertIn("暂无已完成记录",
                      quant_web._paper_t1_summary(pd.DataFrame({"win_t1": [None]})))
        self.assertIn("暂无已完成记录", quant_web._paper_t1_summary(pd.DataFrame()))

    def test_fetch_paper_reads_detail_table_not_missing_view(self):
        frame = pd.DataFrame({"push_slot": ["tail"], "close_t1": [7.1], "win_t1": [1]})
        with patch("quant.paper.ensure_table"), \
                patch.object(quant_web.pd, "read_sql", return_value=frame) as read_sql:
            result = quant_web._fetch_paper()

        self.assertEqual(result.iloc[0]["push_slot"], "tail")
        sql = read_sql.call_args.args[0]
        self.assertIn("quant_paper_trades", sql)
        self.assertIn("LIMIT 20", sql)
        self.assertNotIn("quant_paper_today", sql)


class PositionsDataHandlerTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        return quant_web.tornado.web.Application([
            (r"/positions/data", quant_web.PositionsData),
        ])

    def test_endpoint_returns_recomputed_assets_and_position_quotes(self):
        snapshot = pd.DataFrame(
            [{"price": 11.0, "pct": 10.0}], index=pd.Index(["000001"], name="code"))
        valuation = {
            "total_assets": 2100.0,
            "market_value": 1100.0,
            "cash_balance": 1000.0,
            "cash_known": True,
            "cash_source": "configured",
            "missing_codes": [],
            "unrealized_pnl": 100.0,
            "complete": True,
            "positions": {
                "000001": {"price": 11.0, "market_value": 1100.0,
                            "profit_pct": 10.0, "shares": 100.0},
            },
        }
        with patch.object(quant_web, "cached_stale", return_value=snapshot), \
                patch.object(quant_web, "_portfolio_context",
                             return_value=({}, [{"code": "000001"}], {"000001": 11.0}, valuation)):
            response = self.fetch("/positions/data")

        self.assertEqual(response.code, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload["total_assets"], "2100.00")
        self.assertEqual(payload["positions"]["000001"]["market_value"], "1100.00")
        self.assertEqual(payload["positions"]["000001"]["position_ratio"], "52.38%")
        self.assertIn("更新现金", payload["note"])

    def test_incomplete_quote_is_explicit_in_endpoint(self):
        snapshot = pd.DataFrame(columns=["price", "pct"]).rename_axis("code")
        valuation = {
            "total_assets": None,
            "market_value": None,
            "cash_balance": 1000.0,
            "cash_known": True,
            "cash_source": "configured",
            "missing_codes": ["000001"],
            "unrealized_pnl": None,
            "complete": False,
            "positions": {"000001": {"price": None, "market_value": None,
                                      "profit_pct": None, "shares": 100.0}},
        }
        with patch.object(quant_web, "cached_stale", return_value=snapshot), \
                patch.object(quant_web, "_portfolio_context",
                             return_value=({}, [{"code": "000001"}], {}, valuation)):
            response = self.fetch("/positions/data")

        payload = json.loads(response.body)
        self.assertEqual(payload["total_assets"], "数据不完整")
        self.assertIn("000001", payload["note"])


class BrokerSnapshotDisplayTest(unittest.TestCase):
    def test_page_displays_broker_snapshot_separately_from_live_assets(self):
        snapshot = pd.DataFrame(columns=["price", "pct"]).rename_axis("code")
        valuation = {
            "total_assets": 23039.59,
            "market_value": 6519.00,
            "market_value_partial": 6519.00,
            "cash_balance": 16520.59,
            "cash_known": True,
            "cash_source": "configured",
            "missing_codes": [],
            "unrealized_pnl": 0.0,
            "complete": True,
            "positions": {},
        }
        config = {"portfolio": {"broker_snapshot": {
            "total_assets": 24424.70,
            "total_market_value": 6519.00,
            "total_profit": -351.07,
            "today_profit": -716.00,
            "today_profit_pct": -2.85,
            "available_funds": 16520.59,
            "withdrawable_funds": 11735.06,
            "captured_at": "2026-10-19T19:22:00+08:00",
            "screenshot_time": "19:20",
        }}}
        with patch.object(quant_web, "cached_stale", return_value=snapshot), \
                patch.object(quant_web, "_portfolio_context",
                             return_value=(config, [], {}, valuation)), \
                patch.object(quant_web.qdb, "get_engine", return_value=object()), \
                patch("quant.rules.assess_portfolio", return_value=([], None)), \
                patch("quant.rules.pending_discipline", return_value=pd.DataFrame()), \
                patch("quant.datasource.index.fetch_index_daily", return_value=[]), \
                patch("quant.strategies.leader.leader_scan", return_value=None):
            page = quant_web.build_positions_page()

        self.assertIn("账户总资产", page)
        self.assertIn("23039.59", page)
        self.assertIn("券商总资产快照", page)
        self.assertIn("24424.70", page)
        self.assertIn("券商当前总盈亏", page)
        self.assertIn("-351.07", page)
        self.assertIn("券商当日盈亏", page)
        self.assertIn("-716.00", page)
        self.assertIn("-2.85%", page)
        self.assertIn("19:20", page)
        self.assertIn("检测到券商对账差额", page)
        self.assertIn("不推测差额来源", page)


class PositionsSaveHandlerTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        return quant_web.tornado.web.Application([
            (r"/positions/save", quant_web.PositionsSave),
        ])

    def test_json_save_updates_cash_and_holdings_without_redirect(self):
        config = {
            "portfolio": {"total_assets": 25000.0, "cash_balance": 12000.0,
                          "cash_balance_estimated": True},
            "holdings": [{"code": "000001", "name": "平安银行", "cost": 10.0,
                          "shares": 100, "buy_date": "2026-01-01"}],
        }
        with patch.object(quant_web, "_load_config", return_value=config), \
                patch.object(quant_web, "_save_config") as save_config, \
                patch.object(quant_web, "invalidate_cache"), \
                patch.object(quant_web, "build_positions_page",
                              return_value="<h2>持仓明细(重建)</h2>"), \
                patch("quant.analysis.search_directory",
                      return_value=[{"code": "000002", "name": "万科A",
                                      "industry": "房地产"}]):
            response = self.fetch(
                "/positions/save", method="POST",
                headers={"Accept": "application/json"},
                body="cash_balance=9000&cost_000001=10.5&shares_000001=200&new_code=000002"
                     "&new_name=%E4%B8%87%E7%A7%91A&new_cost=12&new_shares=100")

        self.assertEqual(response.code, 200)
        payload = json.loads(response.body)
        self.assertTrue(payload["ok"])
        saved = save_config.call_args.args[0]
        self.assertEqual(saved["portfolio"]["cash_balance"], 9000.0)
        self.assertFalse(saved["portfolio"]["cash_balance_estimated"])
        self.assertEqual(saved["holdings"][0]["shares"], 200.0)
        self.assertEqual(saved["holdings"][0]["cost"], 10.5)
        self.assertEqual(saved["holdings"][1]["code"], "000002")
        self.assertNotIn("html", payload)   # 保存响应不携带整页(改由后台重建+partial 拉取)
        self.assertIsNone(response.headers.get("Location"))

    def test_invalid_cash_does_not_write_config(self):
        with patch.object(quant_web, "_load_config", return_value={"portfolio": {}, "holdings": []}), \
                patch.object(quant_web, "_save_config") as save_config:
            response = self.fetch(
                "/positions/save", method="POST",
                headers={"Accept": "application/json"}, body="cash_balance=-1")

        self.assertEqual(response.code, 400)
        self.assertFalse(json.loads(response.body)["ok"])
        save_config.assert_not_called()


class PositionsUploadImgHandlerTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        return quant_web.tornado.web.Application([
            (r"/positions/upload_img", quant_web.PositionsUploadImg),
        ])

    def test_upload_preview_prefills_account_summary_and_holdings(self):
        parsed = {
            "account": {
                "total_assets": 24424.70,
                "total_market_value": 6519.00,
                "total_profit": -351.07,
                "today_profit": -716.00,
                "today_profit_pct": -2.85,
                "available_funds": 16520.59,
                "withdrawable_funds": 11735.06,
            },
            "holdings": [
                {"code": "600693", "name": "东百集团", "cost": "9.875", "shares": "200"},
            ],
        }
        boundary = "----stock-screenshot-test"
        body = (f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="img"; filename="account.jpg"\r\n'
                "Content-Type: image/jpeg\r\n\r\n"
                "not-a-real-image\r\n"
                f"--{boundary}--\r\n").encode()
        with patch("quant.datasource.ocr_holdings.ocr_image", return_value="recognized text"), \
                patch("quant.datasource.ocr_holdings.parse_portfolio_screenshot", return_value=parsed), \
                patch.object(quant_web.pd, "read_sql",
                             return_value=pd.DataFrame(columns=["code", "name"])):
            response = self.fetch(
                "/positions/upload_img", method="POST", body=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})

        page = response.body.decode("utf-8")
        self.assertEqual(response.code, 200)
        self.assertIn("name='account_total_assets' value='24424.70'", page)
        self.assertIn("name='account_total_market_value' value='6519.00'", page)
        self.assertIn("name='account_total_profit' value='-351.07'", page)
        self.assertIn("name='account_today_profit' value='-716.00'", page)
        self.assertIn("name='account_available_funds' value='16520.59'", page)
        self.assertIn('value="600693"', page)
        self.assertIn("确认写入账户与持仓", page)


class PositionsUploadConfirmHandlerTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        return quant_web.tornado.web.Application([
            (r"/positions/upload_confirm", quant_web.PositionsUploadConfirm),
        ])

    def test_confirm_saves_snapshot_cash_and_merges_holdings(self):
        import urllib.parse

        config = {
            "portfolio": {"cash_balance": 9000.0, "cash_balance_estimated": True,
                          "unrelated": "preserve"},
            "holdings": [
                {"code": "000001", "name": "原持仓", "cost": 10.0,
                 "shares": 100, "buy_date": "2026-01-01"},
                {"code": "000002", "name": "未出现在截图的持仓", "cost": 20.0, "shares": 100},
            ],
        }
        form = urllib.parse.urlencode([
            ("account_total_assets", "24424.70"),
            ("account_total_market_value", "6519.00"),
            ("account_total_profit", "-351.07"),
            ("account_today_profit", "-716.00"),
            ("account_today_profit_pct", "-2.85"),
            ("account_available_funds", "16520.59"),
            ("account_withdrawable_funds", "11735.06"),
            ("account_captured_at", "2026-10-19T19:22:00+08:00"),
            ("account_screenshot_time", "19:20"),
            ("code", "000001"), ("name", "更新名称"), ("cost", "11.5"), ("shares", "200"),
            ("code", "600693"), ("name", "东百集团"), ("cost", "9.875"), ("shares", "200"),
        ])
        with patch.object(quant_web, "_load_config", return_value=config), \
                patch.object(quant_web, "_save_config") as save_config, \
                patch.object(quant_web, "invalidate_cache"):
            response = self.fetch(
                "/positions/upload_confirm", method="POST", follow_redirects=False,
                headers={"Content-Type": "application/x-www-form-urlencoded"}, body=form)

        self.assertEqual(response.code, 302)
        saved = save_config.call_args.args[0]
        portfolio = saved["portfolio"]
        snapshot = portfolio["broker_snapshot"]
        self.assertEqual(snapshot["total_assets"], 24424.70)
        self.assertEqual(snapshot["total_market_value"], 6519.00)
        self.assertEqual(snapshot["total_profit"], -351.07)
        self.assertEqual(snapshot["today_profit"], -716.00)
        self.assertEqual(snapshot["today_profit_pct"], -2.85)
        self.assertEqual(snapshot["available_funds"], 16520.59)
        self.assertEqual(snapshot["withdrawable_funds"], 11735.06)
        self.assertEqual(snapshot["screenshot_time"], "19:20")
        self.assertEqual(portfolio["cash_balance"], 16520.59)
        self.assertFalse(portfolio["cash_balance_estimated"])
        self.assertEqual(portfolio["unrelated"], "preserve")
        self.assertEqual(len(saved["holdings"]), 3)
        positions = {item["code"]: item for item in saved["holdings"]}
        self.assertEqual(positions["000001"]["cost"], 11.5)
        self.assertEqual(positions["000001"]["buy_date"], "2026-01-01")
        self.assertIn("000002", positions)
        self.assertEqual(positions["600693"]["shares"], 200)

    def test_invalid_account_amount_is_rejected_without_write(self):
        with patch.object(quant_web, "_load_config", return_value={"portfolio": {}, "holdings": []}), \
                patch.object(quant_web, "_save_config") as save_config:
            response = self.fetch(
                "/positions/upload_confirm", method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                body="account_available_funds=-1")

        self.assertEqual(response.code, 400)
        save_config.assert_not_called()


class AnalyzeRoutesTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        return quant_web.make_app()

    def test_analyze_routes_are_registered(self):
        paths = {rule.matcher.regex.pattern for rule in self._app.wildcard_router.rules}
        self.assertTrue(any("/analyze$" in path for path in paths))
        self.assertTrue(any("/analyze/status$" in path for path in paths))
        self.assertTrue(any("/analyze/refresh$" in path for path in paths))
        self.assertTrue(any("/recommend/live$" in path for path in paths))
        self.assertTrue(any("/sector/pick$" in path for path in paths))
        self.assertTrue(any("/strategies$" in path for path in paths))
        self.assertTrue(any("/strategies/data$" in path for path in paths))
        self.assertTrue(any("/stock/search$" in path for path in paths))
        self.assertTrue(any("/trade$" in path for path in paths))


class TradePageHandlerTest(tornado.testing.AsyncHTTPTestCase):
    """交易台:买点分析 + 持仓管理合并一页(双 tab)。"""

    def get_app(self):
        return quant_web.tornado.web.Application([
            (r"/trade", quant_web.TradePage),
        ])

    def test_merges_analysis_and_positions_tabs(self):
        with patch.object(quant_web, "analysis_section",
                          return_value={"body": "<h2 id='analysis-result'>分析区</h2>",
                                        "status": None}), \
                patch.object(quant_web, "cached_stale", return_value="<h2>持仓区</h2>"):
            response = self.fetch("/trade")
        self.assertEqual(response.code, 200)
        body = response.body.decode("utf-8")
        self.assertIn("trade-tab-analysis", body)
        self.assertIn("trade-tab-positions", body)
        self.assertIn("分析区", body)
        self.assertIn("持仓区", body)
        self.assertIn("data-trade-tab", body)           # 切换按钮

    def test_analysis_links_stay_inside_trade_page(self):
        sec_body = ("<form class='analysis-search' action='/analyze' method='get'></form>"
                    '<a href="/analyze?query=002184">x</a>')
        with patch.object(quant_web, "analysis_section",
                          return_value={"body": sec_body, "status": None}), \
                patch.object(quant_web, "cached_stale", return_value="<h2>持仓区</h2>"):
            response = self.fetch("/trade?query=002184")
        body = response.body.decode("utf-8")
        self.assertIn("action='/trade'", body)          # 搜索留在合并页
        self.assertNotIn("action='/analyze'", body)
        self.assertIn('href="/trade?query=002184"', body)
        self.assertNotIn('href="/analyze?query=', body)

    def test_navigation_shows_merged_entry_only(self):
        nav = quant_web._wrap("X")
        self.assertIn('href="/trade"', nav)
        self.assertIn("交易台", nav)
        self.assertNotIn('href="/positions">持仓管理', nav)   # 旧双入口合并
        self.assertNotIn('href="/analyze">买点分析', nav)
        self.assertIn('href="/sim"', nav)                    # 其余导航保留


class NewHoldingResolveTest(unittest.TestCase):
    """新增行解析:六位代码直存(带出名称),名称唯一解析,多匹配提示点选。"""

    def _resolve(self, code, name, rows=None, error=None):
        def fake_search(_query, limit=10):
            if error is not None:
                raise error
            return rows or []
        with patch("quant.analysis.search_directory", side_effect=fake_search):
            return quant_web._resolve_new_holding(code, name)

    def test_six_digit_code_adopts_directory_name(self):
        rows = [{"code": "600977", "name": "中国电影", "industry": "影视"}]
        self.assertEqual(self._resolve("600977", "", rows),
                         ("600977", "中国电影", ""))

    def test_six_digit_code_survives_directory_miss(self):
        self.assertEqual(self._resolve("999999", ""), ("999999", "999999", ""))

    def test_six_digit_code_survives_directory_failure(self):
        code, name, msg = self._resolve("600977", "", error=RuntimeError("db down"))
        self.assertEqual((code, name, msg), ("600977", "600977", ""))

    def test_typed_name_still_wins_over_directory_name(self):
        rows = [{"code": "600977", "name": "中国电影", "industry": "影视"}]
        self.assertEqual(self._resolve("600977", "中影", rows),
                         ("600977", "中影", ""))

    def test_unique_name_resolves_to_code(self):
        rows = [{"code": "600977", "name": "中国电影", "industry": "影视"}]
        code, name, msg = self._resolve("", "中国电影", rows)
        self.assertEqual((code, msg), ("600977", ""))
        self.assertEqual(name, "中国电影")

    def test_exact_name_wins_over_multiple_candidates(self):
        rows = [{"code": "600977", "name": "中国电影", "industry": ""},
                {"code": "600150", "name": "中国船舶", "industry": ""}]
        code, _, msg = self._resolve("", "中国电影", rows)
        self.assertEqual((code, msg), ("600977", ""))

    def test_ambiguous_input_reports_and_skips(self):
        rows = [{"code": "600977", "name": "中国电影", "industry": ""},
                {"code": "600150", "name": "中国船舶", "industry": ""}]
        code, name, msg = self._resolve("中国", "", rows)
        self.assertIsNone(code)
        self.assertIsNone(name)
        self.assertIn("匹配到多只", msg)
        self.assertIn("下拉", msg)

    def test_name_directory_failure_degrades_with_hint(self):
        code, name, msg = self._resolve("中国电影", "", error=RuntimeError("db down"))
        self.assertIsNone(code)
        self.assertIn("六位代码", msg)

    def test_blank_inputs_mean_no_new_row(self):
        self.assertEqual(self._resolve("", ""), (None, None, ""))


class StockSearchHandlerTest(tornado.testing.AsyncHTTPTestCase):
    """/stock/search:输入代码或名称的候选过滤(供编辑表单下拉)。"""

    def get_app(self):
        return quant_web.tornado.web.Application([
            (r"/stock/search", quant_web.StockSearch),
        ])

    def test_query_returns_items_json(self):
        rows = [{"code": "002184", "name": "海得控制", "industry": "自动化"}]
        with patch("quant.analysis.search_directory", return_value=rows) as search:
            response = self.fetch("/stock/search?q=%E6%B5%B7%E5%BE%97")  # 海得
        self.assertEqual(response.code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        payload = json.loads(response.body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["items"][0]["code"], "002184")
        self.assertEqual(payload["items"][0]["name"], "海得控制")
        search.assert_called_once_with("海得", limit=10)

    def test_blank_query_returns_empty_items_without_directory(self):
        with patch("quant.analysis.search_directory") as search:
            response = self.fetch("/stock/search")
        self.assertEqual(response.code, 200)
        self.assertEqual(json.loads(response.body), {"ok": True, "items": []})
        search.assert_not_called()

    def test_directory_failure_returns_503(self):
        with patch("quant.analysis.search_directory", side_effect=RuntimeError("db down")):
            response = self.fetch("/stock/search?q=%E4%B8%AD")
        self.assertEqual(response.code, 503)
        self.assertFalse(json.loads(response.body)["ok"])


class PositionsSuggestUiTest(unittest.TestCase):
    """编辑表单:代码或名称输入框 + 下拉过滤脚本。"""

    def test_edit_form_renders_filter_widget_and_script(self):
        snapshot = pd.DataFrame(columns=["price", "pct"]).rename_axis("code")
        valuation = {
            "total_assets": 23039.59, "market_value": 0.0,
            "cash_balance": 23039.59, "cash_known": True,
            "cash_source": "configured", "missing_codes": [],
            "unrealized_pnl": 0.0, "complete": True, "positions": {},
        }
        with patch.object(quant_web, "cached_stale", return_value=snapshot), \
                patch.object(quant_web, "_portfolio_context",
                             return_value=({"portfolio": {}}, [], {}, valuation)), \
                patch.object(quant_web.qdb, "get_engine", return_value=object()), \
                patch("quant.rules.assess_portfolio", return_value=([], None)), \
                patch("quant.rules.pending_discipline", return_value=pd.DataFrame()), \
                patch("quant.datasource.index.fetch_index_daily", return_value=[]), \
                patch("quant.strategies.leader.leader_scan", return_value=None):
            page = quant_web.build_positions_page()
        self.assertIn("id=new-code", page)
        self.assertIn("代码或名称,输入即过滤", page)
        self.assertIn("id=new-code-list", page)
        self.assertIn("名称(点选自动带出)", page)
        self.assertIn('fetch("/stock/search?q="+encodeURIComponent(q),', page)
        self.assertIn("result.message", page)          # 保存提示透传(多匹配不再静默丢行)
        # 热更新:保存成功即时反馈 + 1.2s 后拉 partial 原位注入,不再整页刷新
        self.assertIn("function applyBody(", page)
        self.assertIn("wrap.innerHTML=html", page)
        self.assertIn("fetch('/positions?partial=1',{cache:'no-store'})", page)
        self.assertIn("applyBody(idx,wasOpen,actName,y,t,result.message)", page)
        self.assertIn("if(window.__posRefresh)window.__posRefresh();", page)
        # loading 判据哨兵拆开拼接,防止脚本源码自我命中(完整页被误判成 loading)
        self.assertIn("'data-pos-'+'loading'", page)
        self.assertNotIn("result.html", page)          # 保存响应不再携带整页 HTML
        # 20 秒轮询防重:热更重放脚本时不叠加 interval / visibility 监听
        self.assertIn("if(window.__posTimer)clearInterval(window.__posTimer)", page)
        self.assertIn("window.__posVisBound", page)
        # 状态恢复:折叠区展开状态 + 输入焦点 + 滚动位置
        self.assertIn("dl.open=wasOpen", page)
        self.assertIn("actName", page)
        self.assertIn("window.scrollTo(0,scrollY)", page)


class PositionsPartialTest(tornado.testing.AsyncHTTPTestCase):
    """/positions?partial=1 返回裸 body(热更注入用);整页含 _wrap 壳。"""

    def get_app(self):
        return quant_web.tornado.web.Application([
            (r"/positions", quant_web.PositionsPage),
        ])

    def test_partial_returns_bare_body_and_full_returns_shell(self):
        with patch.object(quant_web, "cached_stale",
                          return_value="<h2>真实持仓页</h2>"):
            full = self.fetch("/positions")
            part = self.fetch("/positions?partial=1")
        self.assertEqual(full.code, 200)
        self.assertEqual(part.code, 200)
        full_text, part_text = full.body.decode("utf-8"), part.body.decode("utf-8")
        self.assertIn("<html", full_text)               # 整页有壳
        self.assertIn("真实持仓页", full_text)
        self.assertNotIn("<html", part_text)            # partial 无壳裸 body
        self.assertIn("真实持仓页", part_text)
        self.assertNotIn("data-pos-loading", part_text)

    def test_partial_loading_body_carries_sentinel(self):
        with patch.object(quant_web, "cached_stale",
                          return_value=quant_web._POSITIONS_LOADING_BODY):
            response = self.fetch("/positions?partial=1")
        self.assertEqual(response.code, 200)
        self.assertIn("data-pos-loading", response.body.decode("utf-8"))
        self.assertEqual(response.headers.get("Retry-After"), "2")


class InlineScriptsSyntaxTest(unittest.TestCase):
    """页面全部内联 JS 必须语法有效——2026-10-21 曾因缺一个 '}' 让整段脚本
    静默不执行:保存按钮退回原生提交、20 秒行情刷新全部失效且无任何报错。
    node 不可用时跳过;可用时逐段 node --check。"""

    def _check_scripts(self, html_text, label):
        import re
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node 不可用,跳过内联脚本语法检查")
        scripts = re.findall(r"<script>(.*?)</script>", html_text, re.S)
        self.assertTrue(scripts, f"{label} 应至少含一段内联脚本")
        for i, code in enumerate(scripts):
            with tempfile.NamedTemporaryFile("w", suffix=".js",
                                             delete=False) as fh:
                fh.write(code)
                path = fh.name
            try:
                result = subprocess.run([node, "--check", path],
                                         capture_output=True, text=True,
                                         timeout=15)
                self.assertEqual(
                    result.returncode, 0,
                    f"{label} 第 {i} 段脚本语法错误: "
                    f"{(result.stderr or '')[-200:]}")
            finally:
                os.unlink(path)

    def test_positions_page_scripts_are_valid_js(self):
        snapshot = pd.DataFrame(columns=["price", "pct"]).rename_axis("code")
        valuation = {
            "total_assets": 1.0, "market_value": 0.0,
            "cash_balance": 1.0, "cash_known": True,
            "cash_source": "configured", "missing_codes": [],
            "unrealized_pnl": 0.0, "complete": True, "positions": {},
        }
        with patch.object(quant_web, "cached_stale", return_value=snapshot), \
                patch.object(quant_web, "_portfolio_context",
                             return_value=({"portfolio": {}}, [], {}, valuation)), \
                patch.object(quant_web.qdb, "get_engine", return_value=object()), \
                patch("quant.rules.assess_portfolio", return_value=([], None)), \
                patch("quant.rules.pending_discipline", return_value=pd.DataFrame()), \
                patch("quant.datasource.index.fetch_index_daily", return_value=[]), \
                patch("quant.strategies.leader.leader_scan", return_value=None):
            page = quant_web._wrap(quant_web.build_positions_page())
        self._check_scripts(page, "/positions 整页")

    def test_loading_fallback_script_is_valid_js(self):
        self._check_scripts(quant_web._wrap(quant_web._POSITIONS_LOADING_BODY),
                            "loading fallback")

    def test_strategies_shell_script_is_valid_js(self):
        # 2026-10-22 改 stab 高亮时结尾括号写错(}}); 应为 ));),
        # 整个战法页脚本静默不执行且无 console 报错,故纳入 node --check 守门。
        from quant.analysis_view import strategies_shell
        self._check_scripts(quant_web._wrap(strategies_shell()),
                            "/strategies 整页")

    def test_trade_page_scripts_are_valid_js(self):
        html = ("<div class='seg'><button type=button data-trade-tab='analysis' "
                "class='active'>a</button><button type=button "
                "data-trade-tab='positions'>b</button></div>"
                "<div id='trade-tab-analysis'>x</div>"
                "<div id='trade-tab-positions'>正在后台加载</div>"
                + quant_web._TRADE_TAB_SCRIPT + quant_web._TRADE_POS_POLL)
        self._check_scripts(quant_web._wrap(html), "/trade 页签脚本")

    def test_search_form_suggest_script_is_valid_js(self):
        # 2026-10-23 分析搜索框加输入联想(/stock/search 下拉),纳入 node --check 守门。
        from quant.analysis_view import search_form
        self._check_scripts(quant_web._wrap(search_form("002815")),
                            "搜索联想脚本")


class NewHoldingSaveFlowTest(tornado.testing.AsyncHTTPTestCase):
    """PositionsSave 新增行:只输名称可存,多匹配有提示且不误绑。"""

    def get_app(self):
        return quant_web.tornado.web.Application([
            (r"/positions/save", quant_web.PositionsSave),
        ])

    def test_name_only_new_row_resolves_via_directory(self):
        config = {"portfolio": {}, "holdings": []}
        rows = [{"code": "600977", "name": "中国电影", "industry": "影视"}]
        with patch.object(quant_web, "_load_config", return_value=config), \
                patch.object(quant_web, "_save_config") as save_config, \
                patch.object(quant_web, "invalidate_cache"), \
                patch.object(quant_web, "build_positions_page", return_value="<h2>ok</h2>"), \
                patch("quant.analysis.search_directory", return_value=rows):
            response = self.fetch(
                "/positions/save", method="POST",
                headers={"Accept": "application/json"},
                body="new_code=%E4%B8%AD%E5%9B%BD%E7%94%B5%E5%BD%B1"
                     "&new_cost=12&new_shares=100")
        self.assertEqual(response.code, 200)
        payload = json.loads(response.body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["message"], "")
        saved = save_config.call_args.args[0]
        self.assertEqual(saved["holdings"][0]["code"], "600977")
        self.assertEqual(saved["holdings"][0]["name"], "中国电影")

    def test_ambiguous_new_row_reports_message_without_binding(self):
        config = {"portfolio": {}, "holdings": []}
        rows = [{"code": "600977", "name": "中国电影", "industry": ""},
                {"code": "600150", "name": "中国船舶", "industry": ""}]
        with patch.object(quant_web, "_load_config", return_value=config), \
                patch.object(quant_web, "_save_config") as save_config, \
                patch.object(quant_web, "invalidate_cache"), \
                patch.object(quant_web, "build_positions_page", return_value="<h2>ok</h2>"), \
                patch("quant.analysis.search_directory", return_value=rows):
            response = self.fetch(
                "/positions/save", method="POST",
                headers={"Accept": "application/json"},
                body="new_code=%E4%B8%AD%E5%9B%BD&new_cost=12&new_shares=100")  # 中国
        self.assertEqual(response.code, 200)
        payload = json.loads(response.body)
        self.assertTrue(payload["ok"])
        self.assertIn("匹配到多只", payload["message"])
        saved = save_config.call_args.args[0]
        self.assertEqual(saved["holdings"], [])       # 不误绑任何一只

    def test_duplicate_new_code_reports_message(self):
        config = {"portfolio": {}, "holdings": [
            {"code": "600977", "name": "中国电影", "cost": 12.0, "shares": 100}]}
        rows = [{"code": "600977", "name": "中国电影", "industry": ""}]
        with patch.object(quant_web, "_load_config", return_value=config), \
                patch.object(quant_web, "_save_config") as save_config, \
                patch.object(quant_web, "invalidate_cache"), \
                patch.object(quant_web, "build_positions_page", return_value="<h2>ok</h2>"), \
                patch("quant.analysis.search_directory", return_value=rows):
            response = self.fetch(
                "/positions/save", method="POST",
                headers={"Accept": "application/json"},
                body="new_code=600977")
        self.assertEqual(response.code, 200)
        payload = json.loads(response.body)
        self.assertIn("已在持仓中", payload["message"])
        saved = save_config.call_args.args[0]
        self.assertEqual(len(saved["holdings"]), 1)


if __name__ == "__main__":
    unittest.main()
