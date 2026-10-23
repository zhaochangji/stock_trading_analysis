# -*- coding: utf-8 -*-
"""模拟盘(资金账户/纪律决策)测试。"""

import datetime as dt
import os
import sys
import unittest
from unittest.mock import patch

import tornado.testing

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (ROOT, os.path.join(ROOT, "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

import quant.sim as sim  # noqa: E402


class SlippageAndLimitTest(unittest.TestCase):
    def test_slippage(self):
        self.assertEqual(sim.slippage_price(10.0, "buy"), 10.02)
        self.assertEqual(sim.slippage_price(10.0, "sell"), 9.98)

    def test_limit_up_unbuyable(self):
        self.assertTrue(sim.limit_up_unbuyable(9.95, 11.0, None)[0])
        why = sim.limit_up_unbuyable(9.0, 10.9, 11.0)[0]   # 价贴近高点
        self.assertTrue(why)
        self.assertFalse(sim.limit_up_unbuyable(3.2, 10.1, 11.0)[0])
        self.assertFalse(sim.limit_up_unbuyable(None, 10.0, None)[0])


class PlanBuysTest(unittest.TestCase):
    ROWS = [
        {"code": "002184", "name": "海得控制", "confidence": 60, "tag": "中高",
         "methods": "M3共振、板块优质", "price": 10.30, "pct": 1.59,
         "evidence": "双战法共振", "announce": {"status": "pass"}},
        {"code": "002164", "name": "宁波东力", "confidence": 51, "tag": "中高",
         "methods": "M3共振、龙头战法", "price": 5.00, "pct": 9.90,
         "evidence": "涨停", "announce": {"status": "pass"}},
        {"code": "001229", "name": "魅视科技", "confidence": 44, "tag": "谨慎",
         "methods": "M3共振", "price": 39.36, "pct": 1.0,
         "evidence": "低可信", "announce": {"status": "pass"}},
        {"code": "600001", "name": "公告剔除", "confidence": 80, "tag": "高",
         "methods": "M3", "price": 8.0, "pct": 1.0,
         "evidence": "x", "announce": {"status": "blocked"}},
    ]

    def test_normal_market_buys_best_with_cap(self):
        plans, note = sim.plan_buys(self.ROWS, cash=25000, net=25000,
                                    sentiment_score=65, held_codes=set())
        # 宁波东力 +9.9% 涨停买不进;公告剔除不进;44 分不够;只剩海得控制
        codes = [p[0] for p in plans]
        self.assertEqual(codes, ["002184"])
        code, name, deal, qty, method, conf, reason = plans[0]
        self.assertEqual(deal, 10.32)                    # 10.30×1.002
        self.assertEqual(qty % 100, 0)
        self.assertLessEqual(deal * qty, 25000 * sim.MAX_POSITION_RATIO)
        self.assertEqual(conf, 60)
        self.assertIn("双战法共振", reason)

    def test_cautious_sentiment_halves_position(self):
        plans, note = sim.plan_buys(self.ROWS, cash=25000, net=25000,
                                    sentiment_score=44, held_codes=set())
        cap_active = 25000 * sim.MAX_POSITION_RATIO
        cap_cautious = cap_active * 0.5
        self.assertLessEqual(plans[0][2] * plans[0][3], cap_cautious + 1)
        self.assertIn("减半", note)

    def test_iceberg_sentiment_no_new_positions(self):
        plans, _ = sim.plan_buys(self.ROWS, 25000, 25000, 25, set())
        self.assertEqual(plans, [])

    def test_min_confidence_and_held_filter(self):
        low = [{"code": "002183", "name": "x", "confidence": 40, "price": 9.0,
                "methods": "", "evidence": "", "announce": {"status": "pass"}}]
        plans, _ = sim.plan_buys(low, 25000, 25000, 60, set())
        self.assertEqual(plans, [])                      # <50 不开仓
        held = sim.plan_buys(self.ROWS, 25000, 25000, 65, {"002184"})[0]
        self.assertEqual(held, [])                       # 已持仓不重复买


class PlanSellsTest(unittest.TestCase):
    def _hold(self, code="600001", qty=300, cost=10.0, stop=None,
              buy_date="2026-10-14"):
        return {"code": code, "name": "甲", "qty": qty, "cost": cost,
                "buy_date": buy_date, "stop": stop, "method": "M3"}

    def test_stop_loss_triggers_full_exit(self):
        plans = sim.plan_sells([self._hold(stop=9.5)], {"600001": 9.4}, {})
        self.assertEqual(plans[0][0], "600001")
        self.assertEqual(plans[0][1], 1.0)
        self.assertEqual(plans[0][2], round(9.4 * 0.998, 2))
        self.assertIn("止损", plans[0][3])

    def test_take_profits_half_then_full(self):
        plans = sim.plan_sells([self._hold(cost=10.0, qty=200)],
                               {"600001": 11.5}, {})     # +15% → 减半
        self.assertEqual(plans[0][1], 0.5)
        plans = sim.plan_sells([self._hold()], {"600001": 12.5}, {})  # +25% → 清仓
        self.assertEqual(plans[0][1], 1.0)

    def test_break_ma20_exits(self):
        klines = {"600001": [("2026-10-01", float(10 + i)) for i in range(20)]}
        ma20 = sum(10 + i for i in range(20)) / 20.0      # = 10.95 左右
        plans = sim.plan_sells([self._hold(cost=10.5)],
                               {"600001": ma20 - 0.3}, klines)
        self.assertEqual(len(plans), 1)
        self.assertIn("MA20", plans[0][3])

    def test_t_plus_1_blocks_same_day_buy(self):
        hold = self._hold(buy_date=str(dt.date.today()))
        plans = sim.plan_sells([hold], {"600001": 5.0}, {})
        self.assertEqual(plans, [])                      # 当日买入不可卖

    def test_no_signal_when_no_price(self):
        self.assertEqual(sim.plan_sells([self._hold()], {}, {}), [])


class AccountInitTest(unittest.TestCase):
    class _Cur:
        def __init__(self, results):
            self.results, self._i = list(results), 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, args=None):
            self._rows = self.results[min(self._i, len(self.results) - 1)]
            self._i += 1

        def fetchone(self):
            return self._rows

    class _Conn:
        def __init__(self, results):
            self._cur = AccountInitTest._Cur(results)

        def cursor(self):
            return self._cur

        def close(self):
            pass

        def commit(self):
            pass

    def test_init_account_idempotent(self):
        conn = self._Conn([[0]])                          # COUNT=0 → INSERT
        with patch.object(sim, "ensure_tables"), \
                patch.object(sim.qdb, "get_connection", return_value=conn):
            self.assertTrue(sim.init_account(start_cash=25000, target=30000,
                                             as_of=dt.date(2026, 10, 20)))
        conn2 = self._Conn([[1]])                         # 已有账户 → False
        with patch.object(sim, "ensure_tables"), \
                patch.object(sim.qdb, "get_connection", return_value=conn2):
            self.assertFalse(sim.init_account())


class SimPageRouteTest(unittest.TestCase):
    def test_routes_registered_and_navigation_links(self):
        import scripts.quant_web as qw

        app = qw.make_app()
        paths = {r.matcher.regex.pattern for r in app.wildcard_router.rules}
        self.assertTrue(any("/sim$" in p for p in paths))
        nav = qw._wrap("X")
        self.assertIn('href="/sim"', nav)
        self.assertIn("模拟盘", nav)


class SimPageHandlerTest(tornado.testing.AsyncHTTPTestCase):
    """SimPage 渲染:未初始化提示 / 账户+持仓+流水+净值+纪律+免责。"""

    def get_app(self):
        import scripts.quant_web as qw

        return qw.tornado.web.Application([(r"/sim", qw.SimPage)])

    def test_uninitialized_page_hint(self):
        with patch("quant.sim.latest_account", return_value=None), \
                patch("quant.sim.positions", return_value=[]), \
                patch("quant.sim.recent_trades", return_value=[]), \
                patch("quant.sim.net_history", return_value=[]), \
                patch("quant.sim.snapshot_prices", return_value={}), \
                patch("quant.sim.recent_decisions", return_value=[]):
            response = self.fetch("/sim")
        self.assertEqual(response.code, 200)
        self.assertIn("尚未初始化", response.body.decode("utf-8"))

    def test_render_full_account(self):
        account = {"as_of": dt.date(2026, 10, 20), "cash": 20000.0,
                   "market_value": 5000.0, "net": 25000.0,
                   "start_cash": 25000.0, "target": 30000.0, "note": ""}
        positions = [{"code": "002184", "name": "海得控制", "qty": 400,
                      "cost": 10.32, "buy_date": dt.date(2026, 10, 20),
                      "stop": 9.6, "method": "M3共振、板块优质"}]
        trades = [{"ts": "2026-10-20 14:40", "trade_date":
                   dt.date(2026, 10, 20), "code": "002184",
                   "name": "海得控制", "action": "buy", "price": 10.32,
                   "qty": 400, "amount": 4128.0,
                   "method": "M3共振、板块优质", "confidence": 60,
                   "reason": "中高;双战法共振"}]
        history = [{"as_of": dt.date(2026, 10, 20), "net": 25000.0,
                    "cash": 20872.0, "market_value": 4128.0}]
        with patch("quant.sim.latest_account", return_value=account), \
                patch("quant.sim.positions", return_value=positions), \
                patch("quant.sim.recent_trades", return_value=trades), \
                patch("quant.sim.net_history", return_value=history), \
                patch("quant.sim.snapshot_prices", return_value={}), \
                patch("quant.sim.recent_decisions",
                      return_value=[{"ts": "2026-10-20 14:40:00",
                                     "trade_date": dt.date(2026, 10, 20),
                                     "side": "buy", "code": "-",
                                     "name": "全榜", "price": None,
                                     "status": "skip",
                                     "detail": "情绪冰点(21/100)<30,"
                                               "空仓守则,今日不买入"}]):
            response = self.fetch("/sim")
        body = response.body.decode("utf-8")
        self.assertEqual(response.code, 200)
        self.assertIn("25,000", body)                     # 起始/目标线
        self.assertIn("挑战线,非承诺", body)
        self.assertIn("海得控制", body)
        self.assertIn("买入", body)
        self.assertIn("执行纪律", body)                   # 纪律清单
        self.assertIn("每日决策留痕", body)               # 决策留痕块
        self.assertIn("情绪冰点(21/100)&lt;30", body)      # 未买原因可见(转义)
        self.assertIn("未买", body)                      # 徽章
        self.assertIn("不构成投资建议", body)               # 免责
        self.assertIn("目标进度", body)


class PlanBuysVerboseTest(unittest.TestCase):
    """未买原因逐条留痕(为什么今天没买)。"""

    ROWS = PlanBuysTest.ROWS

    def test_iceberg_logs_blanket_skip(self):
        plans, note, skips = sim.plan_buys_verbose(
            self.ROWS, 25000, 25000, 25, set())
        self.assertEqual(plans, [])
        self.assertEqual(skips[0][0], "-")                 # 整体说明行
        self.assertIn("情绪冰点", skips[0][3])
        self.assertIn("不买入", skips[0][3])

    def test_limit_up_and_blocked_candidates_in_skips(self):
        # 真实榜单为降序;fixture 手排非降序,先按可信度排序再评估
        rows = sorted(self.ROWS, key=lambda r: -(r["confidence"] or 0))
        plans, _, skips = sim.plan_buys_verbose(
            rows, 25000, 25000, 65, set())
        by_code = {row[0]: row for row in skips}
        self.assertIn("002164", by_code)                    # 涨停买不进
        self.assertIn("买不进", by_code["002164"][3])
        self.assertIn("600001", by_code)                    # 公告 blocked
        self.assertIn("blocked", by_code["600001"][3])
        self.assertEqual([p[0] for p in plans], ["002184"])  # 只有海得可买

    def test_min_confidence_break_logs_headline(self):
        low = [{"code": "002183", "name": "x", "confidence": 44, "price": 9.0,
                "methods": "", "evidence": "", "announce": {"status": "pass"}}]
        plans, _, skips = sim.plan_buys_verbose(low, 25000, 25000, 60, set())
        self.assertEqual(plans, [])
        self.assertIn("可信度 44<50", skips[0][3])          # 榜首即不足

    def test_held_and_cash_shortage_in_skips(self):
        plans, _, skips = sim.plan_buys_verbose(
            self.ROWS, 25000, 25000, 65, {"002184"})
        by_code = {row[0]: row for row in skips}
        self.assertIn("已持仓,不重复加仓", by_code["002184"][3])
        tiny = sim.plan_buys_verbose(self.ROWS, 500, 25000, 65, set())[2]
        self.assertTrue(any("不足一手" in r[3] for r in tiny))

    def test_daily_cap_two_positions(self):
        rows = [{"code": f"60000{i}", "name": f"股{i}", "confidence": 70,
                 "tag": "中高", "methods": "M3", "evidence": "x", "price": 5.0,
                 "pct": 1.0, "announce": {"status": "pass"}}
                for i in (1, 2, 3)]
        plans, _, skips = sim.plan_buys_verbose(rows, 50000, 50000, 65, set())
        self.assertEqual(len(plans), 2)                    # 单日最多 2 笔
        self.assertTrue(any("已满" in r[3] for r in skips))


class HoldReasonTest(unittest.TestCase):
    """持仓今日未触发卖出的人话原因(为什么今天没卖)。"""

    def _hold(self, code="600001", qty=300, cost=10.0, stop=None,
              buy_date="2026-10-14"):
        return {"code": code, "name": "甲", "qty": qty, "cost": cost,
                "buy_date": buy_date, "stop": stop, "method": "M3"}

    def test_t_plus_one_not_sellable(self):
        h = self._hold(buy_date=str(dt.date.today()))
        self.assertIn("T+1 不可卖", sim.hold_reason(h, 10.5))

    def test_missing_price_deferred(self):
        self.assertIn("快照价缺失", sim.hold_reason(self._hold(), None))

    def test_profit_below_take_profit_line(self):
        text = sim.hold_reason(self._hold(stop=9.0), 10.5)
        self.assertIn("止损 9.00 未破", text)
        self.assertIn("未达 +10% 止盈线", text)

    def test_loss_above_stop(self):
        text = sim.hold_reason(self._hold(cost=10.0, stop=9.0), 9.8)
        self.assertIn("浮亏 -2.0%", text)
        self.assertIn("未触及止损线", text)

    def test_trend_intact_with_ma20(self):
        kl = [("2026-10-%02d" % (i + 1), 10.0) for i in range(20)]
        text = sim.hold_reason(self._hold(stop=9.0, cost=10.0), 10.4, kl)
        self.assertIn("MA20", text)
        self.assertIn("趋势完好", text)


class DecisionsLogTest(unittest.TestCase):
    class _Cur:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, args=None):
            self.calls.append((sql, args))

    class _Conn:
        def __init__(self):
            self._cur = DecisionsLogTest._Cur()

        def cursor(self):
            return self._cur

        def close(self):
            pass

        def commit(self):
            pass

    def test_log_decisions_replaces_idempotently(self):
        conn = self._Conn()
        rows = [("buy", "-", "全榜", None, "skip",
                 "情绪冰点(21/100)<30,空仓守则,今日不买入"),
                ("sell", "002184", "海得控制", 10.5, "hold",
                 "止损 9.60 未破;浮盈 +1.8% 未达 +10% 止盈线")]
        with patch.object(sim, "ensure_tables"), \
                patch.object(sim.qdb, "get_connection", return_value=conn):
            self.assertEqual(sim.log_decisions(
                rows, trade_date=dt.date(2026, 10, 22)), 2)
        sqls = [c[0] for c in conn._cur.calls]
        self.assertTrue(all("REPLACE INTO quant_sim_decisions" in s
                            for s in sqls))
        args = [c[1] for c in conn._cur.calls]
        self.assertEqual(args[0][2], "buy")                 # side
        self.assertEqual(args[0][3], "-")                   # code 占位
        self.assertEqual(args[1][3], "002184")
        self.assertEqual(args[1][7],
                         "止损 9.60 未破;浮盈 +1.8% 未达 +10% 止盈线")

    def test_empty_rows_no_write(self):
        with patch.object(sim, "ensure_tables"), \
                patch.object(sim.qdb, "get_connection") as gc:
            self.assertEqual(sim.log_decisions([]), 0)
            gc.assert_not_called()


if __name__ == "__main__":
    unittest.main()

