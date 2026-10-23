# -*- coding: utf-8 -*-
"""战法选股(M3 共振/龙头梯队/板块优质聚合,按可信度排序)测试。"""

import json
import os
import sys
import unittest
from unittest.mock import patch

import pandas as pd
import tornado.testing

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (ROOT, os.path.join(ROOT, "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

import quant.strategies.pick as pick  # noqa: E402
import scripts.quant_web as quant_web  # noqa: E402
from quant.analysis_view import strategies_shell  # noqa: E402


def _m3_df():
    return pd.DataFrame([
        {"code": "002164", "name": "宁波东力", "grade": "A", "score10": 7.5,
         "evidence": "资金与题材共振", "close": 5.0},
        {"code": "001229", "name": "魅视科技", "grade": "S", "score10": 8.0,
         "evidence": "板块共振[THS聚合]", "close": 12.0},
        {"code": "300001", "name": "创业板股", "grade": "S", "score10": 9.0,
         "evidence": "", "close": 9.0},          # 非主板 → 剔除
        {"code": "600980", "name": "已持仓股", "grade": "S", "score10": 9.0,
         "evidence": "", "close": 8.0},          # 持仓 → 剔除
    ])


def _leader():
    return {
        "theme": "低空安全", "height": 4, "count": 1,
        "ladder": [
            {"code": "002164", "name": "宁波东力", "boards": 2, "state": "封板",
             "seal_yi": 0.52, "limit_time": "09:34"},
            {"code": "600001", "name": "甲股", "boards": 2, "state": "封板",
             "seal_yi": 1.0, "limit_time": "09:30"},
            {"code": "600002", "name": "乙股", "boards": 3, "state": "封板",
             "seal_yi": 2.75, "limit_time": "09:25"},
            {"code": "600003", "name": "高标股", "boards": 6, "state": "封板",
             "seal_yi": 5.0, "limit_time": "09:25"},   # ≥5 板只看不追
            {"code": "300002", "name": "创业龙头", "boards": 2, "state": "封板",
             "seal_yi": 1.0, "limit_time": "09:31"},    # 非主板 → 剔除
        ]}


class ConfidenceRowsTest(unittest.TestCase):
    SENTIMENT = {"score": 44, "label": "谨慎"}

    def _rows(self, sentiment=None, cycle=None, sector_rows=None,
              held=None, leader=None, m3=None):
        rows, env_note = pick.confidence_rows(
            m3 if m3 is not None else _m3_df(),
            leader if leader is not None else _leader(),
            sector_rows or [], sentiment or self.SENTIMENT, cycle,
            held or {"600980"}, limit=12)
        return rows, env_note

    def test_multi_strategy_stack_beats_single_and_sorts_desc(self):
        rows, env_note = self._rows(cycle=None)
        codes = [r["code"] for r in rows]
        # 高标/创业板/持仓均已剔除,只余主板可买标的
        self.assertEqual(sorted(codes), sorted(["002164", "001229",
                                                "600001", "600002"]))
        confidences = [r["confidence"] for r in rows]
        self.assertEqual(confidences, sorted(confidences, reverse=True))
        # 002164 双战法:M3 7.5*4+5=35,龙头 2板 38+梯队4=42 → max42+12=54;
        # 情绪44谨慎 -3 → 51(高于任何单战法标的)
        first = rows[0]
        self.assertEqual(first["code"], "002164")
        self.assertEqual(first["confidence"], 51)
        self.assertTrue(first["multi"])
        self.assertIn("M3共振", first["methods"])
        self.assertIn("龙头战法", first["methods"])
        self.assertEqual(first["tag"], "中高")
        # 乙股 3板: 28+4+封单2.75亿+3=35;甲股 2板: 38+4=42 → 甲先于乙
        order = {r["code"]: i for i, r in enumerate(rows)}
        self.assertLess(order["600001"], order["600002"])
        # 龙头股价格如实标注涨停,T+1 参考
        leader_row = next(r for r in rows if r["code"] == "600001")
        self.assertEqual(leader_row["price"], "—")
        self.assertIn("T+1", leader_row["price_from"])
        self.assertIn("封板", leader_row["evidence"])
        self.assertIn("情绪谨慎", env_note)

    def test_m3_single_with_grade_and_resonance_bonus(self):
        # 001229: 8.0*4 + S级10 + 板块共振5 = 47;情绪谨慎-3 → 44
        rows, _ = self._rows(cycle=None)
        row = next(r for r in rows if r["code"] == "001229")
        self.assertEqual(row["confidence"], 44)
        self.assertEqual(row["tag"], "谨慎参考")
        self.assertFalse(row["multi"])
        self.assertEqual(row["price"], 12.0)
        self.assertEqual(row["price_from"], "最近收盘")

    def test_sentiment_and_index_cycle_adjustments(self):
        # 情绪活跃 +5
        rows, note = self._rows(sentiment={"score": 65, "label": "活跃"})
        row = next(r for r in rows if r["code"] == "001229")
        self.assertEqual(row["confidence"], 47 + 5)
        self.assertIn("情绪活跃", note)
        # 情绪冰点 -10 与大盘退潮 -8 叠加 → 001229 47-10-8=29
        rows, note = self._rows(sentiment={"score": 20, "label": "冰点"},
                               cycle={"label": "退潮下行"})
        row = next(r for r in rows if r["code"] == "001229")
        self.assertEqual(row["confidence"], 29)
        self.assertIn("大盘退潮下行-8", note)
        # 大盘趋势上行 +5
        rows, _ = self._rows(cycle={"label": "趋势上行"})
        row = next(r for r in rows if r["code"] == "001229")
        self.assertEqual(row["confidence"], 47 - 3 + 5)

    def test_sector_quality_row_with_stock_cycle_adjust(self):
        sector_rows = [
            {"code": "600111", "name": "优质股", "quality": 4.0,
             "cycle_label": "趋势主升", "evidence": "趋势+位置过",
             "price": 10.1, "pct": "+1.5%", "setup": "回踩企稳",
             "plan_low": 10.0, "plan_high": 10.2, "stop": 9.75,
             "tp1": 11.1, "tp2": 12.1},
            {"code": "600222", "name": "过热股", "quality": 5.0,
             "cycle_label": "高位过热", "evidence": "x", "price": 6.0},
        ]
        rows, _ = self._rows(sector_rows=sector_rows)
        good = next(r for r in rows if r["code"] == "600111")
        # 优质 4*7=28 + 主升3 = 31 + 情绪-3 + 个股主升+4 → 32
        self.assertEqual(good["confidence"], 32)
        self.assertIn("板块优质", good["methods"])
        self.assertEqual(good["plan_low"], 10.0)
        self.assertEqual(good["stop"], 9.75)
        self.assertEqual(good["cycle_label"], "趋势主升")
        hot = next(r for r in rows if r["code"] == "600222")
        # 5.0*7=35 + 情绪-3 - 个股高位过热-15 → 17
        self.assertEqual(hot["confidence"], 17)

    def test_limit_and_confidence_cap(self):
        # 构造 15 只单战法标的验证 limit 截断与 100 封顶
        m3 = pd.DataFrame([
            {"code": f"6001{i:02d}", "name": f"股{i}", "grade": "S",
             "score10": 9.0, "evidence": "板块共振", "close": 1.0}
            for i in range(15)])
        rows, _ = self._rows(m3=m3, leader={}, sentiment={"score": 80, "label": "亢奋"},
                             cycle={"label": "趋势上行"})
        self.assertEqual(len(rows), 12)              # limit 截断
        self.assertLessEqual(rows[0]["confidence"], 100)
        # 9*4+10+5=51 +5 +5 = 61;榜单内最高不超过封顶
        self.assertEqual(rows[0]["confidence"], 61)
        self.assertEqual(rows[0]["tag"], "中高")

    def test_empty_inputs_return_no_rows(self):
        rows, env_note = pick.confidence_rows(
            None, None, [], {"score": 50, "label": "中性"}, None, set())
        self.assertEqual(rows, [])
        self.assertTrue(env_note)


class LadderViewTest(unittest.TestCase):
    def test_watch_only_marks_high_board_and_non_mainboard(self):
        view = pick.ladder_view(_leader())
        by_code = {r["code"]: r for r in view}
        self.assertTrue(by_code["600003"]["watch_only"])   # 6 板高标
        self.assertIn("只看不追", by_code["600003"]["note"])
        self.assertTrue(by_code["300002"]["watch_only"])  # 创业板
        self.assertIn("权限外", by_code["300002"]["note"])
        self.assertFalse(by_code["600001"]["watch_only"])  # 主板 2 板
        self.assertIn("弱转强", by_code["600001"]["note"])
        self.assertIn("分歧转一致", by_code["600002"]["note"])
        # 高标不进可买表,但梯队提示区完整展示
        rows, _ = pick.confidence_rows(_m3_df(), _leader(), [],
                                       {"score": 50, "label": "中性"},
                                       None, {"600980"})
        self.assertNotIn("600003", [r["code"] for r in rows])
        self.assertIn("600003", by_code)

    def test_none_leader_returns_empty(self):
        self.assertEqual(pick.ladder_view(None), [])


class PaperWinNoteTest(unittest.TestCase):
    def test_no_records(self):
        self.assertIn("无记录", pick.paper_win_note(None))
        self.assertIn("无记录", pick.paper_win_note(pd.DataFrame()))

    def test_small_sample_not_weighted(self):
        df = pd.DataFrame({"win_t1": [1]})
        self.assertIn("样本不足", pick.paper_win_note(df))

    def test_enough_sample_reports_rate(self):
        df = pd.DataFrame({"win_t1": [1] * 15 + [0] * 10})
        note = pick.paper_win_note(df)
        self.assertIn("60%", note)
        self.assertIn("25 条", note)


class StrategiesShellTest(unittest.TestCase):
    def test_shell_has_panel_script_and_fetch(self):
        shell = strategies_shell()
        self.assertIn("战法选股", shell)
        self.assertIn("strategies-panel", shell)
        self.assertIn("/strategies/data", shell)
        self.assertIn("按可信度排序", shell)
        self.assertIn("多战法共振", shell)               # 叠加信号标识
        self.assertIn("连板梯队", shell)                  # 提示区
        self.assertIn("encodeURIComponent", shell)        # 股票可点击
        self.assertIn("/analyze?query=", shell)
        self.assertIn("不构成投资建议", shell)
        self.assertIn("id=\"analysis-query\"", shell)    # 统一搜索框仍在

    def test_shell_has_method_tabs_and_feasibility_stats(self):
        """战法细分 tab(全部/M3/龙头/板块)+ 前向可行性统计卡。"""
        shell = strategies_shell()
        self.assertIn("data-stab", shell)                # 切换按钮
        self.assertIn("stab-", shell)                    # tab 容器(JS 运行时生成)
        self.assertIn("TABS=[['','全部（混合榜）'],['M3共振'", shell)  # 四路 tab 定义
        self.assertIn("'龙头战法','龙头战法'", shell)
        self.assertIn("'板块优质','板块优质'", shell)
        self.assertIn("各战法可行性", shell)               # 统计卡标题
        self.assertIn("T+1胜率", shell)
        self.assertIn("样本<10，仅供参考", shell)          # 样本不足如实标注
        self.assertIn("两种样本口径不同不混算", shell)       # 口径不混算声明
        self.assertIn("stRow('m3_push'", shell)           # 推送口径参考行
        self.assertIn("method_stats", shell)


class StrategiesDataHandlerTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        return quant_web.tornado.web.Application([
            (r"/strategies/data", quant_web.StrategiesData)])

    def test_loading_when_inner_caches_cold(self):
        # 内层 scan/leader 未就绪时返回 loading,前端提示稍候,不误报"无标的"
        with patch.object(quant_web, "_strategy_payload",
                          return_value={"loading": True, "rows": []}):
            response = self.fetch("/strategies/data")
        payload = json.loads(response.body)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["loading"])
        self.assertNotIn("rows", payload)   # loading 时不下发榜单,前端提示稍候

    def test_payload_passthrough_with_sorted_rows(self):
        fixture = {
            "loading": False,
            "sentiment": {"score": 44, "label": "谨慎"},
            "cycle": None,
            "env_note": "情绪谨慎(44/100)-3；大盘周期不可用±0",
            "rows": [{"code": "002164", "name": "宁波东力",
                      "confidence": 51, "tag": "中高", "methods": "M3共振、龙头战法",
                      "multi": True, "score10": 7.5, "price": "—",
                      "price_from": "涨停(T+1 关注)", "pct": "+9.9%",
                      "setup": "—", "plan_low": None, "plan_high": None,
                      "stop": None, "tp1": None, "tp2": None,
                      "cycle_label": "—", "evidence": "…", "tiebreak": 40.0}],
            "ladder": [{"code": "600003", "name": "高标股", "boards": 6,
                        "state": "封板", "seal_yi": 5.0, "limit_time": "09:25",
                        "watch_only": True, "note": "高标6板·只看不追等首阴反包"}],
            "theme": "低空安全", "height": 4, "count": 1, "hints": [],
            "paper_note": "样本不足",
            "scan_ready": True, "leader_ready": True,
            "generated_at": "2026-10-20 17:00:00",
        }
        with patch.object(quant_web, "_strategy_payload", return_value=fixture):
            response = self.fetch("/strategies/data")
        payload = json.loads(response.body)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["loading"])
        self.assertEqual(payload["rows"][0]["confidence"], 51)
        self.assertEqual(payload["theme"], "低空安全")
        self.assertTrue(payload["ladder"][0]["watch_only"])
        self.assertEqual(payload["env_note"], fixture["env_note"])

    def test_error_returns_ok_false(self):
        with patch.object(quant_web, "_strategy_payload",
                          side_effect=RuntimeError("boom")):
            response = self.fetch("/strategies/data")
        payload = json.loads(response.body)
        self.assertFalse(payload["ok"])
        self.assertIn("组装异常", payload["error"])


class StrategiesPageHandlerTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        return quant_web.tornado.web.Application([
            (r"/strategies", quant_web.StrategiesPage)])

    def test_page_shell_renders(self):
        response = self.fetch("/strategies")
        self.assertEqual(response.code, 200)
        body = response.body.decode("utf-8")
        self.assertIn("strategies-panel", body)
        self.assertIn("战法选股", body)
        self.assertIn("/strategies/data", body)


if __name__ == "__main__":
    unittest.main()
