"""当前时段实时决策(核心条件可买候选 / 持仓卖出信号)的单元与接口测试；不访问真实行情。"""

import datetime as dt
import json
import unittest
from unittest.mock import patch

import pandas as pd
import tornado.testing

import quant.selection.live as live
from quant.analysis_view import empty_page, live_reco_control, render_analysis
import scripts.quant_web as quant_web


SCAN_DF = pd.DataFrame([
    {"code": "600111", "name": "甲股", "grade": "S", "horizon": "短线",
     "score10": 8.5, "evidence": "5日吸筹+MACD金叉"},
    {"code": "000222", "name": "乙股", "grade": "A", "horizon": "波段",
     "score10": 7.2, "evidence": "站上MA10+量比2.1"},
    {"code": "600777", "name": "丙股", "grade": "A", "horizon": "波段",
     "score10": 6.5, "evidence": "量比2.0"},
    {"code": "300333", "name": "创业板股", "grade": "S", "horizon": "短线",
     "score10": 9.0, "evidence": "非主板"},
    {"code": "600444", "name": "已持仓股", "grade": "S", "horizon": "短线",
     "score10": 8.8, "evidence": "已持有"},
    {"code": "600555", "name": "涨幅出界股", "grade": "A", "horizon": "短线",
     "score10": 6.0, "evidence": "候选"},
    {"code": "600666", "name": "不活跃股", "grade": "B", "horizon": "波段",
     "score10": 5.0, "evidence": "候选"},
])

REALTIME_SNAPS = {
    "600111": {"code": "600111", "last_price": 10.1, "price_change_ratio_pct": 1.0,
               "prev_price": 10.0, "turnover": 3e8, "high_price": 10.5},
    "000222": {"code": "000222", "last_price": 5.5, "price_change_ratio_pct": 1.0,
               "prev_price": 5.4, "turnover": 1.5e8, "high_price": 5.6},
    "600777": {"code": "600777", "last_price": 7.0, "price_change_ratio_pct": 1.0,
               "prev_price": 6.9, "turnover": 2e8, "high_price": 7.2},
    "300333": {"code": "300333", "last_price": 20.0, "price_change_ratio_pct": 3.0,
               "prev_price": 19.4, "turnover": 2e8, "high_price": 20.5},
    "600444": {"code": "600444", "last_price": 8.0, "price_change_ratio_pct": 1.0,
               "prev_price": 7.9, "turnover": 1.2e8, "high_price": 8.2},
    "600555": {"code": "600555", "last_price": 9.0, "price_change_ratio_pct": 8.0,
               "prev_price": 8.3, "turnover": 2e8, "high_price": 9.3},
    "600666": {"code": "600666", "last_price": 4.0, "price_change_ratio_pct": 1.0,
               "prev_price": 3.9, "turnover": 0.5e8, "high_price": 4.1},
}

# 600111: 价 10.1 ≥ MA10 10.0 ≥ MA20 9.8、DIF≥DEA、RSI 55 → 趋势过;
#         回踩区 10.00～10.20 含 10.1,涨幅 1%、额 3 亿、离高回撤 3.8% → 位置过 → 4/4。
# 000222: 趋势过;回踩区 5.20～5.30 不含 5.5 → 位置未过 → 3/4(通过大部分)。
TECHS = {
    "600111": {"ma10": 10.0, "ma20": 9.8, "rsi14": 55.0, "dif": 0.2, "dea": 0.1,
               "vr": 2.0, "high20_prev": 11.5, "hist_date": "2026-10-20",
               "indicator_date": "2026-10-20"},
    "000222": {"ma10": 5.2, "ma20": 5.1, "rsi14": 55.0, "dif": 0.1, "dea": 0.05,
               "vr": 2.0, "high20_prev": 5.9, "hist_date": "2026-10-20",
               "indicator_date": "2026-10-20"},
}


def fake_snapshot(hist, quote, today):
    code = str(quote.get("code") or str(quote.get("thscode") or "").split(".")[0] or "")
    return TECHS.get(code)


def hist_loader(code):
    if code == "600777":
        raise RuntimeError("K线读取失败")  # 历史K不足/异常 → 如实跳过
    return [{"date": "2026-10-19", "close": 10.0, "high": 10.2,
             "low": 9.9, "volume": 123456}]


class CurrentSlotTest(unittest.TestCase):
    def test_slots_follow_the_clock(self):
        def at(hour, minute):
            return live.current_slot(dt.datetime(2026, 10, 20, hour, minute))
        self.assertEqual(at(9, 20)["slot"], "auction")
        self.assertEqual(at(9, 20)["mode"], "auction")
        self.assertEqual(at(10, 30)["slot"], "intraday")
        self.assertEqual(at(10, 30)["label"], "盘中")
        self.assertEqual(at(12, 0)["label"], "午间休市")
        self.assertEqual(at(13, 30)["label"], "盘中")
        self.assertEqual(at(14, 40)["slot"], "tail")
        self.assertEqual(at(15, 30)["slot"], "closed")
        self.assertEqual(at(8, 0)["slot"], "closed")

    def test_closed_slot_keeps_realtime_mode_for_recheck(self):
        slot = live.closed_slot("非交易日")
        self.assertEqual(slot["slot"], "closed")
        self.assertEqual(slot["mode"], "realtime")


class ConfirmBuyCandidatesTest(unittest.TestCase):
    def test_realtime_mode_filters_mainboard_held_and_confirm_rules(self):
        rows = live.confirm_buy_candidates(
            SCAN_DF, REALTIME_SNAPS, {"600444"}, "realtime")

        self.assertEqual([r["code"] for r in rows], ["600111", "000222", "600777"])
        self.assertEqual(rows[0]["name"], "甲股")
        self.assertEqual(rows[0]["price"], "10.10")
        self.assertEqual(rows[0]["pct"], "+1.00")
        self.assertEqual(rows[0]["amount_yi"], "3.0")
        self.assertIn("净额缺失", rows[0]["confirm"])  # 资金软条件如实披露
        self.assertIn("5日吸筹", rows[0]["evidence"])

    def test_auction_mode_uses_auction_fields(self):
        snaps = {"600111": {"auction_price": 10.9, "auction_pct": 1.5,
                            "auction_volume_ratio": 1.8, "auction_amount": 2e7}}
        rows = live.confirm_buy_candidates(SCAN_DF, snaps, set(), "auction")

        self.assertEqual([r["code"] for r in rows], ["600111"])
        self.assertEqual(rows[0]["price"], "10.90")
        self.assertIn("竞价量比", rows[0]["confirm"])

    def test_missing_snapshot_or_empty_scan_returns_nothing(self):
        self.assertEqual(
            live.confirm_buy_candidates(SCAN_DF, {}, set(), "realtime"), [])
        self.assertEqual(
            live.confirm_buy_candidates(None, REALTIME_SNAPS, set(), "realtime"), [])


class EntryPlanTest(unittest.TestCase):
    def test_pullback_band_matches_assess_entry_formula(self):
        plan = live.entry_plan(
            {"ma10": 10.0, "ma20": 9.8, "high20_prev": 11.5}, 10.1)
        self.assertEqual(plan["setup"], "回踩均线")
        self.assertEqual(plan["low"], 10.0)
        self.assertEqual(plan["high"], 10.2)
        self.assertEqual(plan["reference"], 10.10)
        # 止损 = max(参考价-7%, 较近的 MA20-0.5% 与参考价-1%) = 9.75
        self.assertEqual(plan["stop"], 9.75)
        self.assertEqual(plan["tp1"], 11.11)
        self.assertEqual(plan["tp2"], 12.12)

    def test_breakout_band_uses_high20_prev(self):
        plan = live.entry_plan(
            {"ma10": 10.0, "ma20": 9.8, "high20_prev": 10.0}, 10.5)
        self.assertEqual(plan["setup"], "突破前20日高")
        self.assertEqual(plan["low"], 10.0)
        self.assertEqual(plan["high"], 10.2)

    def test_missing_ma_returns_none(self):
        self.assertIsNone(live.entry_plan({"ma10": 10.0, "ma20": None}, 10.0))
        self.assertIsNone(live.entry_plan({"ma10": 10.0, "ma20": 9.8}, None))


class AssessBuyCandidatesTest(unittest.TestCase):
    def test_keeps_full_and_majority_pass_with_price_plan(self):
        with patch("quant.analysis.technical_snapshot", side_effect=fake_snapshot):
            rows, skipped = live.assess_buy_candidates(
                SCAN_DF, REALTIME_SNAPS, {"600444"}, "realtime", hist_loader)

        # 600777 历史K读取失败被如实跳过;600111 4/4 排在 000222 3/4 之前。
        self.assertEqual([r["code"] for r in rows], ["600111", "000222"])
        self.assertEqual(skipped, 1)
        top = rows[0]
        self.assertEqual(top["core_pass"], 4)
        self.assertEqual(top["core_total"], 4)
        self.assertEqual(top["verdict_tag"], "核心条件全过")
        self.assertEqual(top["failed"], "—")
        self.assertEqual(top["setup"], "回踩均线")
        self.assertEqual(top["plan_low"], 10.0)
        self.assertEqual(top["plan_high"], 10.2)
        self.assertEqual(top["stop"], 9.75)
        self.assertEqual(top["tp1"], 11.11)
        self.assertEqual(top["tp2"], 12.12)
        self.assertEqual(top["kline_date"], "2026-10-20")
        self.assertEqual(rows[1]["core_pass"], 3)
        self.assertEqual(rows[1]["verdict_tag"], "通过大部分")
        self.assertEqual(rows[1]["failed"], "买入位置")

    def test_below_majority_is_not_shown(self):
        scan = pd.DataFrame([
            {"code": "600888", "name": "低通过股", "grade": "A", "horizon": "短线",
             "score10": 9.0, "evidence": "趋势破位"}])
        snaps = {"600888": {"code": "600888", "last_price": 5.0,
                            "price_change_ratio_pct": 1.0, "prev_price": 4.9,
                            "turnover": 2e8, "high_price": 5.1}}
        techs = {"600888": {"ma10": 6.0, "ma20": 5.8, "rsi14": 55.0,
                            "dif": 0.1, "dea": 0.05, "vr": 2.0,
                            "high20_prev": 6.5, "hist_date": "2026-10-20"}}
        with patch("quant.analysis.technical_snapshot",
                   side_effect=lambda h, q, t: techs.get(q["code"])):
            rows, skipped = live.assess_buy_candidates(
                scan, snaps, set(), "realtime", hist_loader)

        self.assertEqual(rows, [])  # 2/4 低于「大部分通过」,不硬凑
        self.assertEqual(skipped, 0)

    def test_empty_inputs_are_safe(self):
        self.assertEqual(
            live.assess_buy_candidates(None, {}, set(), "realtime", hist_loader),
            ([], 0))


class CollectSellSignalsTest(unittest.TestCase):
    def test_classifies_sell_actions_with_evidence(self):
        holdings = [
            {"code": "600977", "name": "中国电影", "cost": 14.72},
            {"code": "600199", "name": "金种子酒"},
            {"code": "000001", "name": "平安银行"},
        ]

        def analyze(h):
            if h["code"] == "000001":
                raise RuntimeError("行情异常")
            return {"name": h["name"], "now": 10.0 if h["code"] == "600977" else 8.0,
                    "pct": -5.0 if h["code"] == "600977" else 1.0}

        def decide(r):
            if r["name"] == "中国电影":
                return ("减仓至轻仓", "收盘在20日线下方 5.0%,趋势破位", "止损位 9.50")
            return ("躺平持有", "趋势位上方,均线多头", "支撑 7.80")

        sells, quiet, failed = live.collect_sell_signals(holdings, analyze, decide)

        self.assertEqual([s["code"] for s in sells], ["600977"])
        self.assertEqual(sells[0]["action"], "减仓至轻仓")
        self.assertIn("趋势破位", sells[0]["why"])
        self.assertEqual(sells[0]["pl_pct"], "-32.07")  # 10/14.72-1
        self.assertEqual(quiet, ["金种子酒"])
        self.assertEqual(failed[0]["code"], "000001")
        self.assertEqual(failed[0]["error"], "RuntimeError")

    def test_quote_error_is_reported_not_hidden(self):
        holdings = [{"code": "600977", "name": "中国电影"}]
        sells, quiet, failed = live.collect_sell_signals(
            holdings, lambda h: {"name": h["name"], "error": "实时行情缺失"},
            decide_fn=lambda r: r)
        self.assertEqual(sells, [])
        self.assertEqual(failed[0]["error"], "实时行情缺失")

    def test_no_holdings_is_safe(self):
        self.assertEqual(live.collect_sell_signals([], lambda h: {}, lambda r: r),
                         ([], [], []))


class LiveRecoControlTest(unittest.TestCase):
    def test_button_script_link_back_and_mutex_present(self):
        control = live_reco_control()
        self.assertIn("live-reco-btn", control)         # 脚本绑定搜索行的按钮
        self.assertIn("live-reco-panel", control)
        self.assertIn("/recommend/live", control)
        self.assertIn("live-reco-back", control)          # 返回个股分析
        self.assertIn("analysis-result", control)         # 与搜索结果互斥
        self.assertIn("建议买入区", control)               # 价格计划列
        self.assertIn("encodeURIComponent", control)      # 股票可点击进入单股分析
        self.assertIn("/analyze?query=", control)
        # 筛选项已合并进统一搜索框:面板控件不再带独立输入框/按钮
        self.assertNotIn("sector-pick-input", control)
        self.assertNotIn("sector-pick-btn", control)

    def test_search_form_has_unified_input_and_inline_buttons(self):
        from quant.analysis_view import search_form

        form = search_form("002815")
        self.assertIn('id="analysis-query"', form)
        self.assertIn("股票代码、名称或板块名", form)      # 统一搜索框提示
        self.assertIn("分析买点</button>", form)
        self.assertIn("id='live-reco-btn'", form)         # 实时决策与「分析买点」同行
        self.assertIn("type='button'", form)              # 不触发表单提交

    def test_auto_sector_page_triggers_pick_on_load(self):
        from quant.analysis_view import sector_page

        page = sector_page("大金融", ["银行", "证券"])
        self.assertIn("识别为板块", page)
        self.assertIn("银行、证券", page)
        self.assertIn("data-sector='大金融'", page)       # 加载后自动筛选
        self.assertIn("/sector/pick", page)
        control = live_reco_control("大金融")
        self.assertIn("data-sector='大金融'", control)
        bare = live_reco_control()
        self.assertIn("data-sector=''", bare)             # 无板块时留空属性

    def test_pages_wrap_results_in_analysis_result_container(self):
        page = empty_page("002815")
        self.assertIn("当前时段实时决策", page)          # 按钮在搜索行
        self.assertIn("<div id='analysis-result'>", page)
        self.assertTrue(page.endswith("</div>"))

        facts = {"code": "600000", "name": "浦发银行", "quote": {}, "tech": {},
                 "sector": {}, "ths_flow": {}, "em_flow": {}, "news": [],
                 "quote_at": None, "quote_date": None, "quote_trade_at": None,
                 "spot_date": None, "sector_at": None, "ths_flow_at": None,
                 "completed_at": None, "source_errors": {}}
        analysis = render_analysis(facts, now=dt.datetime(2026, 10, 20, 10, 0))
        self.assertIn("<div id='analysis-result'>", analysis)
        self.assertTrue(analysis.endswith("</div>"))
        self.assertIn("id='live-reco-btn'", analysis)     # 两页都有实时决策按钮
        self.assertIn('id="analysis-query"', analysis)   # 统一搜索框仍在容器外


class RecommendLiveHandlerTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        return quant_web.tornado.web.Application([
            (r"/recommend/live", quant_web.RecommendLive)])

    def test_endpoint_composes_core_scores_plans_and_sells(self):
        slot = {"slot": "tail", "label": "尾盘", "mode": "realtime", "note": "尾盘时段"}
        holdings = [{"code": "600444", "name": "已持仓股", "cost": 8.5},
                    {"code": "600977", "name": "中国电影", "cost": 14.72}]
        items = [
            {"thscode": f"{code}.SH" if code.startswith("6") else f"{code}.SZ",
             **REALTIME_SNAPS[code]}
            for code in REALTIME_SNAPS
        ]

        def analyze(h):
            return {"name": h["name"], "now": 10.0, "pct": -5.0}

        def decide(r):
            return ("减仓至轻仓", "收盘在20日线下方 5.0%,趋势破位", "止损位 9.50")

        with patch("quant.selection.live.current_slot", return_value=slot), \
                patch.object(quant_web, "cached_stale",
                             return_value=(SCAN_DF, {})) as cached, \
                patch.object(quant_web, "_announce_audit", return_value={
                    "status": "pending", "hits": [], "n": 0,
                    "summary": "巨潮公告核验中(稍候自动更新)"}), \
                patch.object(quant_web, "_load_config",
                             return_value={"holdings": holdings}), \
                patch.object(quant_web.ths, "prices_snapshot",
                             return_value={"item": items}) as snapshot, \
                patch.object(quant_web, "_live_hist", side_effect=hist_loader), \
                patch("quant.analysis.technical_snapshot", side_effect=fake_snapshot), \
                patch.object(quant_web.hd, "analyze", side_effect=analyze), \
                patch.object(quant_web.hd, "decide", side_effect=decide):
            response = self.fetch("/recommend/live")

        self.assertEqual(response.code, 200)
        payload = json.loads(response.body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["slot_label"], "尾盘")
        self.assertFalse(payload["buys_loading"])
        self.assertEqual([b["code"] for b in payload["buys"]], ["600111", "000222"])
        top = payload["buys"][0]
        self.assertEqual(top["core_pass"], 4)
        self.assertEqual(top["verdict_tag"], "核心条件全过")
        self.assertEqual(top["plan_low"], 10.0)
        self.assertEqual(top["plan_high"], 10.2)
        self.assertEqual(top["stop"], 9.75)
        self.assertEqual(top["tp1"], 11.11)
        self.assertEqual(top["setup"], "回踩均线")
        self.assertEqual(payload["buys"][1]["failed"], "买入位置")
        self.assertIn("历史K不足", payload["buy_note"])   # 600777 如实披露
        self.assertEqual([s["code"] for s in payload["sells"]],
                         ["600444", "600977"])
        self.assertIn("趋势破位", payload["sells"][0]["why"])
        self.assertIn("其余", payload["sell_note"])
        # 选股与快照请求都必须复用缓存/轻量单次调用,不做全市场抓取
        self.assertEqual(cached.call_args[0][0], "scan")
        snapshot.assert_called_once()

    def test_cold_scan_reports_loading_without_fabricating(self):
        slot = {"slot": "intraday", "label": "盘中", "mode": "realtime", "note": "盘中"}
        with patch("quant.selection.live.current_slot", return_value=slot), \
                patch.object(quant_web, "cached_stale",
                             return_value=(pd.DataFrame(), {})), \
                patch.object(quant_web, "_load_config", return_value={}), \
                patch.object(quant_web, "_live_hist", side_effect=hist_loader), \
                patch.object(quant_web.hd, "analyze", side_effect=RuntimeError), \
                patch.object(quant_web.hd, "decide", side_effect=RuntimeError):
            response = self.fetch("/recommend/live")

        payload = json.loads(response.body)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["buys_loading"])
        self.assertEqual(payload["buys"], [])
        self.assertEqual(payload["sell_note"], "当前无录入持仓。")


class MarketSentimentTest(unittest.TestCase):
    def test_score_and_label_are_transparent(self):
        # 75% 上涨 + 100 涨停 + 10% 大涨 → 0.5*75+0.3*1+0.2*1 = 87.5 → 亢奋
        hot = pd.DataFrame({"change_rate": [1.0] * 10 + [9.9] * 90})
        s = live.market_sentiment(hot)
        self.assertEqual(s["score"], 97)
        self.assertEqual(s["label"], "亢奋")
        # 30% 上涨 + 少量涨停 → 0.5*30+0.3*0.1+0.2*0.1 ≈ 15 → 冰点
        cold = pd.DataFrame({"change_rate": [1.0] * 30 + [-1.0] * 65 + [9.9] * 5})
        s2 = live.market_sentiment(cold)
        self.assertEqual(s2["label"], "冰点")
        self.assertEqual(s2["limit_up"], 5)

    def test_unavailable_input_is_honest(self):
        empty = live.market_sentiment(pd.DataFrame())
        self.assertIsNone(empty["score"])
        self.assertEqual(empty["label"], "不可用")


class MarketCycleTest(unittest.TestCase):
    @staticmethod
    def _rows(closes, start_day=1):
        return [{"date": f"2026-09-{i + start_day:02d}", "close": c}
                for i, c in enumerate(closes)]

    def test_rising_market_is_recovery_or_trend(self):
        cycle = live.market_cycle(self._rows(list(range(100, 116))))
        self.assertEqual(cycle["label"], "修复上行")
        self.assertEqual(cycle["streak"], 15)

    def test_sharp_fall_is_retreat(self):
        closes = [100.0] * 10 + [99.0, 98.5, 97.0, 95.0, 92.0]
        cycle = live.market_cycle(self._rows(closes))
        self.assertEqual(cycle["label"], "退潮下行")
        self.assertLessEqual(cycle["chg5"], -3)

    def test_spike_and_fade_is_overheat(self):
        closes = [100.0] * 11 + [105.0, 110.0, 118.0, 107.0]
        cycle = live.market_cycle(self._rows(closes))
        self.assertEqual(cycle["label"], "高位过热")
        self.assertGreaterEqual(cycle["chg5"], 6)

    def test_short_history_returns_none(self):
        self.assertIsNone(live.market_cycle(self._rows(list(range(100, 110)))))


class StockCycleTest(unittest.TestCase):
    @staticmethod
    def _hist(closes, low=9.0, high=10.05):
        return [{"date": f"2026-09-{i + 1:02d}", "close": c, "high": high, "low": low}
                for i, c in enumerate(closes)]

    def test_below_ma20_is_retreat(self):
        cyc = live.stock_cycle(self._hist([9.9] * 10), 9.5,
                               {"ma10": 10.5, "ma20": 10.0, "rsi14": 50})
        self.assertEqual(cyc["label"], "下行退潮")

    def test_near_high_with_big_5d_gain_is_overheat(self):
        hist = self._hist([10.0] * 10, low=8.0)
        tech = {"ma10": 10.0, "ma20": 9.8, "rsi14": 50}
        cyc = live.stock_cycle(hist, 11.3, tech)  # 近5日 +13%
        self.assertEqual(cyc["label"], "高位过热")

    def test_freshly_above_ma20_at_low_range_is_bottom_start(self):
        closes = [96.0] * 6 + [99.0, 99.5, 100.5, 101.0]  # 近4根里先在 MA20 下
        hist = self._hist(closes, low=95.0, high=110.0)
        tech = {"ma10": 100.5, "ma20": 100.0, "rsi14": 55}
        cyc = live.stock_cycle(hist, 101.5, tech)
        self.assertEqual(cyc["label"], "底部启动")

    def test_aligned_trend_is_main_rally(self):
        hist = self._hist([9.5, 9.6, 9.7, 9.8, 9.9, 10.0], low=9.4, high=10.3)
        tech = {"ma10": 10.1, "ma20": 9.9, "rsi14": 55}
        cyc = live.stock_cycle(hist, 10.2, tech)
        self.assertEqual(cyc["label"], "趋势主升")


class MatchSectorsTest(unittest.TestCase):
    STATS = pd.DataFrame([
        {"index_code": "885001", "index_name": "自动化设备",
         "avg_pct": 2.43, "n": 54},
        {"index_code": "885002", "index_name": "其他自动化设备",
         "avg_pct": 3.02, "n": 13},
        {"index_code": "885003", "index_name": "汽车零部件",
         "avg_pct": 1.10, "n": 80},
        {"index_code": "885010", "index_name": "银行",
         "avg_pct": 0.80, "n": 42},
        {"index_code": "885011", "index_name": "证券",
         "avg_pct": 1.50, "n": 48},
        {"index_code": "885012", "index_name": "保险",
         "avg_pct": 0.60, "n": 5},
        {"index_code": "885013", "index_name": "多元金融",
         "avg_pct": 0.90, "n": 26},
        {"index_code": "885014", "index_name": "机器人",
         "avg_pct": 2.78, "n": 12},
    ])

    def test_exact_then_contains_match(self):
        codes, names, _ = live.match_sectors(self.STATS, "自动化设备")
        self.assertEqual(codes, ["885001"])
        codes2, names2, _ = live.match_sectors(self.STATS, "自动化")
        self.assertEqual(set(names2), {"自动化设备", "其他自动化设备"})

    def test_common_alias_big_finance_maps_to_ths_sectors(self):
        codes, names, _ = live.match_sectors(self.STATS, "大金融")
        self.assertEqual(set(names), {"银行", "证券", "保险", "多元金融"})
        # 「金融」与「大金融」等价
        _, names2, _ = live.match_sectors(self.STATS, "金融")
        self.assertEqual(set(names2), {"银行", "证券", "保险", "多元金融"})

    def test_no_match_returns_empty_with_suggestions(self):
        codes, names, suggest = live.match_sectors(self.STATS, "不存在板块")
        self.assertEqual(codes, [])
        self.assertIn("其他自动化设备", suggest)  # 热门按当日涨幅排序

    def test_unmatched_word_gets_related_sliding_window_hint(self):
        # 板块库里没有「大金融」字面名,但滑窗子串「金融」能提示相关板块
        _, _, suggest = live.match_sectors(self.STATS, "机器人产业")
        self.assertIn("机器人", suggest)


class SectorPrefilterTest(unittest.TestCase):
    MEMBERS = pd.DataFrame([
        {"code": "600111", "name": "甲股"}, {"code": "600222", "name": "乙股"},
        {"code": "000333", "name": "丙股"}, {"code": "300444", "name": "创业板"},
        {"code": "600990", "name": "ST退"}, {"code": "600555", "name": "已持仓"},
    ])
    SPOT = pd.DataFrame([
        {"code": "600111", "name": "甲股", "change_rate": 1.5,
         "deal_amount": 3e8, "new_price": 10.1, "high_price": 10.5},
        {"code": "600222", "name": "乙股", "change_rate": 6.5,
         "deal_amount": 2e8, "new_price": 8.0, "high_price": 8.2},
        {"code": "000333", "name": "丙股", "change_rate": 3.0,
         "deal_amount": 1.5e8, "new_price": 5.5, "high_price": 5.6},
        {"code": "300444", "name": "创业板", "change_rate": 2.0,
         "deal_amount": 2e8, "new_price": 20.0, "high_price": 20.5},
        {"code": "600990", "name": "ST退", "change_rate": 2.0,
         "deal_amount": 2e8, "new_price": 3.0, "high_price": 3.1},
        {"code": "600555", "name": "已持仓", "change_rate": 2.0,
         "deal_amount": 2e8, "new_price": 7.0, "high_price": 7.2},
    ])
    CAUTIOUS = {"score": 40, "label": "谨慎"}

    def test_cautious_market_tightens_pct_cap_and_filters(self):
        cands, note = live.sector_prefilter(
            self.MEMBERS, self.SPOT, self.CAUTIOUS, {"600555"})
        # 创业板/ST/已持仓/涨幅 6.5%>5% 全部剔除
        self.assertEqual(cands["code"].tolist(), ["600111", "000333"])
        self.assertIn("涨幅上限 5%", note)
        self.assertIn("谨慎", note)

    def test_neutral_market_keeps_7pct_cap(self):
        cands, _ = live.sector_prefilter(
            self.MEMBERS, self.SPOT, {"score": 55, "label": "中性"}, {"600555"})
        self.assertIn("600222", cands["code"].tolist())

    def test_empty_members_is_safe(self):
        cands, note = live.sector_prefilter(
            pd.DataFrame(), self.SPOT, self.CAUTIOUS, set())
        self.assertEqual(len(cands), 0)
        self.assertIn("情绪门控", note)


class SectorPickTest(unittest.TestCase):
    CANDS = pd.DataFrame([
        {"code": "600111", "name": "甲股", "name_spot": "甲股",
         "change_rate": 1.5, "deal_amount": 6e8, "new_price": 10.1,
         "high_price": 10.5},
        {"code": "000333", "name": "丙股", "name_spot": "丙股",
         "change_rate": 3.0, "deal_amount": 1.5e8, "new_price": 5.5,
         "high_price": 5.6},
    ])
    TECHS = {
        "600111": {"ma10": 10.0, "ma20": 9.8, "rsi14": 55.0, "dif": 0.2,
                   "dea": 0.1, "vr": 2.0, "high20_prev": 11.5,
                   "hist_date": "2026-10-20"},
        "000333": {"ma10": 5.3, "ma20": 5.2, "rsi14": 55.0, "dif": 0.1,
                   "dea": 0.05, "vr": 2.0, "high20_prev": 5.9,
                   "hist_date": "2026-10-20"},
    }

    @staticmethod
    def _hist(code):
        closes = [9.0] * 30 + [9.2, 9.4, 9.5, 9.6, 9.8]
        return [{"date": f"2026-09-{i + 1:02d}", "close": c,
                 "high": 10.05, "low": 8.9} for i, c in enumerate(closes)]

    def _fake_tech(self, techs):
        """按快照里的 code 返回伪造 tech;无快照(喂表价路径)的给 000333。"""
        def fake(hist, quote, today):
            quote = quote or {}
            code = str(quote.get("code")
                       or str(quote.get("thscode") or "").split(".")[0] or "")
            if code:
                return techs.get(code, self.TECHS.get(code))
            return self.TECHS["000333"]  # 无实时快照的候选走喂表价
        return fake

    def test_full_quality_with_sentiment_and_cycle_in_evidence(self):
        sentiment = {"score": 50, "label": "中性"}
        cycle = {"label": "修复上行", "chg5": 2.1, "streak": 3}
        sector_stat = {"names": "自动化设备", "avg_pct": 2.43,
                       "up_ratio": 0.9, "rank": 3, "total": 320}
        snaps = {"600111": {"code": "600111", "last_price": 10.1,
                            "high_price": 10.5}}
        with patch("quant.analysis.technical_snapshot",
                   side_effect=self._fake_tech(self.TECHS)):
            rows, meta = live.sector_pick(
                self.CANDS, snaps, sentiment, cycle, sector_stat, self._hist)

        # 600111: 趋势2+位置2+周期主升1+额≥5亿 0.5+板块热 0.5 = 6.0
        self.assertEqual(rows[0]["code"], "600111")
        self.assertEqual(rows[0]["quality"], "6.0")
        self.assertTrue(rows[0]["realtime"])
        self.assertIn("周期趋势主升", rows[0]["evidence"])
        self.assertIn("市场中性(50/100)", rows[0]["evidence"])
        self.assertIn("大盘修复上行", rows[0]["evidence"])
        self.assertEqual(rows[0]["plan_low"], 10.0)
        self.assertEqual(rows[0]["stop"], 9.75)
        # 000333: 无快照 → 喂表价;位置未过(5.5>5.406) → 2+0+1+0+0.5=3.5
        self.assertEqual(rows[1]["quality"], "3.5")
        self.assertFalse(rows[1]["realtime"])
        self.assertEqual(meta, {"skipped": 0, "dropped_hot": 0,
                                "dropped_retreat": 0, "below_min": 0})

    def test_overheat_cycle_is_dropped_not_listed(self):
        techs = {"600111": dict(self.TECHS["600111"], rsi14=80.0)}
        snaps = {"600111": {"code": "600111", "last_price": 10.1,
                            "high_price": 10.5}}
        with patch("quant.analysis.technical_snapshot",
                   side_effect=self._fake_tech(techs)):
            rows, meta = live.sector_pick(
                self.CANDS, snaps, {"score": 50, "label": "中性"}, None,
                {"names": "x", "avg_pct": 2.0, "up_ratio": 0.9}, self._hist)
        # 600111 rsi 80 + 20日高位 → 高位过热剔除,如实计数
        self.assertEqual([r["code"] for r in rows], ["000333"])
        self.assertEqual(meta["dropped_hot"], 1)


class SectorPickHandlerTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        return quant_web.tornado.web.Application([
            (r"/sector/pick", quant_web.SectorPick)])

    class _Cur:
        def __init__(self, results):
            self.results, self._i = list(results), 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, args=None):
            self._rows = self.results[min(self._i, len(self.results) - 1)]
            self._i += 1

        def fetchall(self):
            return self._rows

    class _Conn:
        def __init__(self, results):
            self.results = results

        def cursor(self):
            return SectorPickHandlerTest._Cur(self.results)

        def close(self):
            pass

    def test_endpoint_returns_quality_rows_with_sentiment(self):
        stats = pd.DataFrame([{"index_code": "885001", "index_name": "自动化设备",
                               "avg_pct": 2.43, "up_ratio": 0.9,
                               "amount": 1.2e9, "n": 54}])
        conn = self._Conn([
            [("600111",)],  # 成分表只有 code,名字由 spot 表 pandas 合并
            [("600111", "甲股", 1.5, 6e8, 10.1, 10.5)],
        ])
        cycle = {"label": "修复上行", "chg5": 2.1, "streak": 3, "close": 3300.0,
                 "ma5": 3290.0, "ma10": 3280.0, "as_of": "2026-10-20"}
        tech = {"ma10": 10.0, "ma20": 9.8, "rsi14": 55.0, "dif": 0.2, "dea": 0.1,
                "vr": 2.0, "high20_prev": 11.5, "hist_date": "2026-10-20"}
        snaps = {"item": [{"thscode": "600111.SH", "last_price": 10.1,
                           "high_price": 10.5}]}

        with patch.object(quant_web, "_sector_stats", return_value=stats), \
                patch.object(quant_web, "_index_cycle", return_value=cycle), \
                patch.object(quant_web, "_load_config", return_value={}), \
                patch.object(quant_web, "_announce_audit", return_value={
                    "status": "pending", "hits": [], "n": 0,
                    "summary": "巨潮公告核验中(稍候自动更新)"}), \
                patch.object(quant_web.qdb, "get_connection", return_value=conn), \
                patch.object(quant_web.ths, "prices_snapshot", return_value=snaps), \
                patch.object(quant_web, "_live_hist",
                             side_effect=lambda code: [
                                 {"date": "2026-10-20", "close": 10.0,
                                  "high": 10.05, "low": 8.9, "volume": 1}]), \
                patch("quant.analysis.technical_snapshot", return_value=tech):
            response = self.fetch("/sector/pick?sector=" + "自动化设备")

        self.assertEqual(response.code, 200)
        payload = json.loads(response.body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["sector"]["names"], "自动化设备")
        self.assertEqual(payload["sector"]["rank"], 1)
        self.assertEqual(payload["sentiment"]["label"], "中性")  # 1/1 上涨 → 50
        self.assertEqual(payload["cycle"]["label"], "修复上行")
        self.assertIn("情绪门控", payload["gate_note"])
        self.assertEqual(payload["rows"][0]["code"], "600111")
        self.assertEqual(payload["rows"][0]["quality"], "6.0")
        self.assertTrue(payload["rows"][0]["realtime"])
        self.assertIn("大盘修复上行", payload["rows"][0]["evidence"])

    def test_unknown_sector_suggests_hot_names(self):
        stats = pd.DataFrame([{"index_code": "885001", "index_name": "自动化设备",
                               "avg_pct": 2.43, "up_ratio": 0.9,
                               "amount": 1e9, "n": 54}])
        with patch.object(quant_web, "_sector_stats", return_value=stats), \
                patch.object(quant_web, "_load_config", return_value={}):
            response = self.fetch("/sector/pick?sector=不存在的板块")

        payload = json.loads(response.body)
        self.assertFalse(payload["ok"])
        self.assertIn("未匹配到板块", payload["error"])
        self.assertIn("自动化设备", payload["error"])


class AnalyzePageSectorRouteTest(tornado.testing.AsyncHTTPTestCase):
    """统一搜索框分流:无股票匹配时识别板块,股票候选优先。"""

    def get_app(self):
        return quant_web.tornado.web.Application([
            (r"/analyze", quant_web.AnalyzePage)])

    def test_query_without_stock_match_routes_to_sector_page(self):
        import urllib.parse

        stats = pd.DataFrame([{"index_code": "885001", "index_name": "银行",
                               "avg_pct": 0.8, "up_ratio": 0.4,
                               "amount": 1e9, "n": 42}])
        with patch("quant.analysis.resolve_stock",
                   return_value=(None, [], "最新股票目录未匹配到该股票")), \
                patch.object(quant_web, "_sector_stats", return_value=stats), \
                patch("quant.selection.live.match_sectors",
                      return_value=(["885001"], ["银行"], [])):
            response = self.fetch("/analyze?query=" + urllib.parse.quote("大金融"))

        page = response.body.decode("utf-8")
        self.assertEqual(response.code, 200)
        self.assertIn("识别为板块", page)
        self.assertIn("data-sector='大金融'", page)
        self.assertIn("银行", page)
        self.assertIn("id='live-reco-btn'", page)        # 搜索行的实时决策按钮仍在

    def test_stock_candidates_still_take_priority_over_sector(self):
        candidates = [{"code": "600777", "name": "丙股"}]
        with patch("quant.analysis.resolve_stock",
                   return_value=(None, candidates, "匹配到多只股票")):
            response = self.fetch("/analyze?query=科技")

        page = response.body.decode("utf-8")
        self.assertIn("匹配候选", page)                   # 股票优先,不进板块分流
        self.assertNotIn("识别为板块", page)

    def test_unknown_query_falls_back_to_empty_page(self):
        with patch("quant.analysis.resolve_stock",
                   return_value=(None, [], "未匹配到该股票")), \
                patch.object(quant_web, "_sector_stats",
                             return_value=pd.DataFrame()), \
                patch("quant.selection.live.match_sectors",
                      return_value=([], [], [])):
            response = self.fetch("/analyze?query=不存在的关键词")

        page = response.body.decode("utf-8")
        self.assertIn("analysis-empty", page)
        self.assertNotIn("识别为板块", page)


if __name__ == "__main__":
    unittest.main()
