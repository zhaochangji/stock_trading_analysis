# -*- coding: utf-8 -*-
"""除权口径核验(quant.adjust_check)测试:全 mock,不连网不连库。

核验逻辑:通达信除权表定位最近除权日 → 库内 K 线在该日边界是否
连续(前复权正确=无跳空;被未复权 dump 覆盖=跳空超涨跌停缓冲)。
"""

import datetime as dt
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import quant.adjust_check as ac  # noqa: E402


def _event(date, fenhong=0.0, songzhuangu=0.0, peigu=0.0, peigujia=0.0,
           category=1, name="除权除息"):
    return {"date": date, "category": category, "name": name,
            "fenhong": fenhong, "songzhuangu": songzhuangu,
            "peigujia": peigujia, "peigu": peigu}


def _fetch_rows(rows):
    return lambda code: rows


class LatestEventTest(unittest.TestCase):
    def test_filters_by_date_and_params(self):
        rows = [
            _event("2026-01-10", fenhong=0.32),                    # 90 日外(视运行日)
            _event("2026-06-01", category=5, name="股本变化"),      # 无除权参数 → 跳过
            _event("2026-09-30", songzhuangu=5.0),
            _event("2026-10-15", fenhong=0.5),
        ]
        since = "2026-08-01"
        ev = ac._latest_event(rows, since)
        self.assertEqual(ev["date"], "2026-10-15")              # 取最近真除权
        self.assertIsNone(ac._latest_event(
            [_event("2026-01-10", category=5)], since))          # 全被过滤
        self.assertIsNone(ac._latest_event(None, since))

    def test_describe_params(self):
        self.assertIn("10派0.50元", ac._describe(_event("2026-10-15", fenhong=0.5)))
        self.assertIn("10送转5.0股",
                      ac._describe(_event("2026-10-15", songzhuangu=5.0)))
        self.assertIn("10配3.0股@8.5元",
                      ac._describe(_event("2026-10-15", peigu=3.0, peigujia=8.5)))
        self.assertEqual(ac._describe(_event("2026-10-15")), "除权除息")


class CheckTest(unittest.TestCase):
    """check() 的四态:ok / suspicious / no_event / no_data。

    事件日期相对今天动态构造(近 3 日/200 日前),防真实时间漂移后失效。
    """

    RECENT = (dt.date.today() - dt.timedelta(days=3)).strftime("%Y-%m-%d")
    OLD = (dt.date.today() - dt.timedelta(days=200)).strftime("%Y-%m-%d")

    def _around(self, prev_close, ex_close):
        return ((("2026-10-14", prev_close), ("2026-10-15", ex_close)))

    def test_ok_continuous_boundary(self):
        with patch.object(ac, "_rows_around", return_value=self._around(10.0, 10.05)), \
                patch.object(ac.tdx, "fetch_xdxr", _fetch_rows(
                    [_event(self.RECENT, fenhong=0.32)])):
            r = ac.check("600977", "中国电影", conn=MagicMock())
        self.assertEqual(r["status"], "ok")
        self.assertAlmostEqual(r["jump"], 0.005, places=6)
        self.assertIn("10派0.32元", r["ex_param"])

    def test_suspicious_songzhuangu_cover(self):
        """dump 未复权覆盖:除权日 10 送 10 → 库内涨跌 -49%,远超缓冲。"""
        with patch.object(ac, "_rows_around", return_value=self._around(10.0, 5.10)), \
                patch.object(ac.tdx, "fetch_xdxr", _fetch_rows(
                    [_event(self.RECENT, songzhuangu=10.0)])):
            r = ac.check("002184", "海亮股份", conn=MagicMock())
        self.assertEqual(r["status"], "suspicious")
        self.assertAlmostEqual(r["jump"], -0.49, places=6)
        self.assertIn("未复权", r["note"])

    def test_st_uses_tighter_limit(self):
        """ST 涨跌停 ±5%:跳空 -7% 在主股缓冲内但 ST 判可疑。"""
        with patch.object(ac, "_rows_around", return_value=self._around(10.0, 9.30)), \
                patch.object(ac.tdx, "fetch_xdxr", _fetch_rows(
                    [_event(self.RECENT, fenhong=0.7)])):
            r = ac.check("600xxx", "ST 某某", conn=MagicMock())
        self.assertEqual(r["status"], "suspicious")

    def test_no_event_recent(self):
        with patch.object(ac.tdx, "fetch_xdxr", _fetch_rows(
                [_event(self.OLD, fenhong=0.32)])):           # 200 天前,超回看
            r = ac.check("600977", "中国电影", conn=MagicMock())
        self.assertEqual(r["status"], "no_event")
        self.assertIn("无除权", r["note"])

    def test_no_data_paths(self):
        # ① 通达信取不到
        with patch.object(ac.tdx, "fetch_xdxr", return_value=None):
            r = ac.check("600977", conn=MagicMock())
        self.assertEqual(r["status"], "no_data")
        # ② 拉取抛异常
        with patch.object(ac.tdx, "fetch_xdxr", side_effect=RuntimeError("boom")):
            r = ac.check("600977", conn=MagicMock())
        self.assertEqual(r["status"], "no_data")
        # ③ 库内缺除权日行
        with patch.object(ac, "_rows_around", return_value=None), \
                patch.object(ac.tdx, "fetch_xdxr", _fetch_rows(
                    [_event(self.RECENT, fenhong=0.32)])):
            r = ac.check("600977", conn=MagicMock())
        self.assertEqual(r["status"], "no_data")
        self.assertEqual(r["ex_date"], self.RECENT)
        self.assertIn("缺行", r["note"])


class _FakeCur:
    def __init__(self, results):
        self._results = results          # sql 关键词 → fetchall 返回
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, args=None):
        self.executed.append(sql)
        self._pending = self._results(sql)

    def fetchall(self):
        return self._pending

    def fetchone(self):
        return self._pending[0] if self._pending else None


class _FakeConn:
    def __init__(self, results):
        self._results = results
        self.statements = []

    def cursor(self):
        cur = _FakeCur(self._results)
        self._curs = getattr(self, "_curs", [])
        self._curs.append(cur)
        return cur

    def commit(self):
        pass

    def close(self):
        pass

    @property
    def executed(self):
        return [s for c in getattr(self, "_curs", []) for s in c.executed]


class UniverseTest(unittest.TestCase):
    def test_merges_four_sources(self):
        def results(sql):
            if "quant_sim_positions" in sql:
                return [("600977",)]
            if "quant_strategy_trades" in sql:
                return [("002815",), ("600977",)]
            if "quant_paper_trades" in sql:
                return [("603232",)]
            if "cn_stock_spot" in sql:
                return [("600977", "中国电影"), ("002815", "崇达技术"),
                        ("603232", "格尔软件")]
            return []
        conn = _FakeConn(results)
        with patch.object(ac.yaml, "safe_load",
                          return_value={"holdings": [{"code": "002815"}]}):
            codes = ac._universe(conn, days=7)
        self.assertEqual(codes, {"002815": "实盘持仓", "600977": "模拟盘持仓",
                                 "603232": "推送信号"})
        self.assertNotIn("战法候选", codes.values())       # 002815 已被持仓先占
        # 列名防回归:quant_sim_positions 用 qty(无 shares 列,2026-10-23 首跑踩过)
        joined = " ".join(conn.executed)
        self.assertIn("qty>0", joined)
        self.assertNotIn("shares>0", joined)

    def test_holdings_file_error_does_not_raise(self):
        conn = _FakeConn(lambda sql: [])
        with patch.object(ac, "open", side_effect=OSError("no file")):
            codes = ac._universe(conn)
        self.assertEqual(codes, {})


class RunCheckTest(unittest.TestCase):
    def test_summarizes_and_persists(self):
        conn = _FakeConn(lambda sql: [])

        def fake_check(code, name="", lookback_days=90, conn=None, _fetch_xdxr=None):
            if code == "002815":
                return {"code": code, "name": name, "ex_date": "2026-10-15",
                        "ex_param": "10送转10.0股", "prev_close": 10.0,
                        "ex_close": 5.1, "jump": -0.49, "status": "suspicious",
                        "note": "疑似未复权覆盖"}
            return {"code": code, "name": name, "ex_date": "", "ex_param": "",
                    "prev_close": None, "ex_close": None, "jump": None,
                    "status": "no_event", "note": "近期无除权"}

        with patch.object(ac, "_universe", return_value={"002815": "实盘持仓",
                                                         "600977": "实盘持仓"}), \
                patch.object(ac, "_names", return_value={}), \
                patch.object(ac, "check", side_effect=fake_check):
            summary = ac.run_check(conn=conn)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["suspicious"], 1)
        self.assertEqual(summary["no_event"], 1)
        self.assertEqual(summary["suspects"][0]["code"], "002815")
        self.assertIn("REPLACE INTO quant_adjust_check", " ".join(conn.executed))

    def test_universe_read_error_becomes_no_data(self):
        """清单来源异常时 run_check 不炸,逐股记 no_data。"""
        conn = _FakeConn(lambda sql: [])
        with patch.object(ac, "_universe", return_value={"600977": "实盘持仓"}), \
                patch.object(ac, "_names", return_value={}), \
                patch.object(ac, "check", side_effect=RuntimeError("db down")):
            summary = ac.run_check(conn=conn)
        self.assertEqual(summary["no_data"], 1)
        self.assertEqual(summary["total"], 1)


if __name__ == "__main__":
    unittest.main()
