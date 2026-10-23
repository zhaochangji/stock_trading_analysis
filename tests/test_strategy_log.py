# -*- coding: utf-8 -*-
"""战法可行性追踪(quant_strategy_trades)测试。"""

import datetime as dt
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (ROOT, os.path.join(ROOT, "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

import quant.strategy_log as slog  # noqa: E402


class _Cursor:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, args):
        self.calls.append((sql, args))


class _Connection:
    def __init__(self):
        self.cursor_obj = _Cursor()
        self.committed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def close(self):
        pass


class LogTodayTest(unittest.TestCase):
    ROWS = [
        {"code": "002184", "name": "海得控制", "methods": "M3共振、板块优质",
         "confidence": 60, "price": 10.30},
        {"code": "002164", "name": "宁波东力", "methods": "M3共振、龙头战法",
         "confidence": 51, "price": 5.00},
        {"code": "600199", "name": "金种子酒", "methods": "板块优质",
         "confidence": 40, "price": 8.00},
        {"code": "003037", "name": "三和管桩", "methods": "M3共振、龙头战法",
         "confidence": 55, "price": 6.00},
        {"code": "12345", "name": "坏代码", "methods": "M3共振",
         "confidence": 90, "price": 1.0},                   # 代码非法(5位)→跳过
        {"code": "301001", "name": "无价行", "methods": "龙头战法",
         "confidence": 90, "price": None},                  # 无价且无喂表价→跳过
        {"code": "603330", "name": "天洋新材", "methods": "龙头战法",
         "confidence": 32, "price": None},                  # 连板无现价→喂表价兜底
    ]

    def test_splits_rows_by_method_and_tops(self):
        conn = _Connection()
        with patch.object(slog, "ensure_table"), \
                patch.object(slog.qdb, "get_connection", return_value=conn):
            counts = slog.log_today(self.ROWS, log_date=dt.date(2026, 10, 22),
                                    top=2, spot_prices={"603330": 18.5})
        self.assertEqual(counts, {"m3": 2, "leader": 2, "sector": 2})
        self.assertTrue(conn.committed)
        logged = {}
        for sql, args in conn.cursor_obj.calls:
            self.assertIn("REPLACE INTO quant_strategy_trades", sql)
            logged.setdefault(args[1], []).append((args[2], args[4]))
        # m3 路:按可信度取前 2(海得 60/三和 55;宁波东力 51 被截掉)
        self.assertEqual(sorted(logged["m3"]), [("002184", 60.0), ("003037", 55.0)])
        # leader 路:三和 55 + 宁波东力 51
        self.assertEqual(sorted(logged["leader"]), [("002164", 51.0), ("003037", 55.0)])
        # sector 路:海得 60 + 金种子 40
        self.assertEqual(sorted(logged["sector"]), [("002184", 60.0), ("600199", 40.0)])
        # 坏代码/无价行任何路都不落库
        self.assertNotIn("12345", str(logged))
        self.assertNotIn("301001", str(logged))

    def test_top_limit_keeps_confidence_order(self):
        rows = [{"code": f"60000{i}", "name": f"x{i}", "methods": "龙头战法",
                 "confidence": i, "price": 1.0} for i in range(1, 6)]
        conn = _Connection()
        with patch.object(slog, "ensure_table"), \
                patch.object(slog.qdb, "get_connection", return_value=conn):
            counts = slog.log_today(rows, log_date=dt.date(2026, 10, 22), top=2)
        self.assertEqual(counts, {"m3": 0, "leader": 2, "sector": 0})
        leader_codes = sorted(args[2] for sql, args in conn.cursor_obj.calls
                              if args[1] == "leader")
        self.assertEqual(leader_codes, ["600004", "600005"])  # 可信度 5/4 入选


class UpdateTrackingTest(unittest.TestCase):
    def test_backfills_t1_t5_and_win_flags(self):
        book = pd.DataFrame([
            {"id": 1, "code": "002184", "log_date": "2026-10-22", "price": 10.30},
            {"id": 2, "code": "600199", "log_date": "2026-10-22", "price": None},
        ])
        history = pd.DataFrame([
            {"code": "002184", "date": "2026-10-22", "close": 10.30},
            {"code": "002184", "date": "2026-10-23", "close": 10.50},
        ])
        read_sql = MagicMock(side_effect=[book, history])
        conn = _Connection()
        with patch.object(slog, "ensure_table"), \
                patch("pandas.read_sql", read_sql), \
                patch.object(slog, "spot_latest_closes",
                             return_value=({}, None)), \
                patch.object(slog.qdb, "get_connection", return_value=conn):
            result = slog.update_tracking()
        self.assertEqual(result, (1, 0))          # T+1 胜 1(10.50>10.30);T+5 未到期
        self.assertTrue(conn.committed)
        self.assertEqual(len(conn.cursor_obj.calls), 1)   # price 空的行跳过
        sql, args = conn.cursor_obj.calls[0]
        self.assertIn("close_t1", sql)
        self.assertEqual(args[0], 10.50)
        self.assertEqual(args[3], 1)              # win_t1
        self.assertIsNone(args[4])                # win_t5 未到期

    def test_empty_book_returns_zero(self):
        with patch.object(slog, "ensure_table"), \
                patch("pandas.read_sql", return_value=pd.DataFrame()), \
                patch.object(slog, "spot_latest_closes",
                             return_value=({}, None)), \
                patch.object(slog.qdb, "get_connection",
                             return_value=_Connection()):
            self.assertEqual(slog.update_tracking(), (0, 0))


class MethodStatTest(unittest.TestCase):
    def test_stats_summary_aggregates_per_method(self):
        frame = pd.DataFrame([
            {"method": "m3", "confidence": 60, "price": 10.0,
             "close_t1": 10.5, "win_t1": 1, "win_t5": 1},
            {"method": "m3", "confidence": 50, "price": 10.0,
             "close_t1": 9.5, "win_t1": 0, "win_t5": 0},
            {"method": "leader", "confidence": 55, "price": 5.0,
             "close_t1": None, "win_t1": None, "win_t5": None},
        ])
        paper = pd.DataFrame([
            {"price": 14.4, "close_t1": 14.5, "win_t1": 1, "win_t5": None},
            {"price": 10.25, "close_t1": 10.5, "win_t1": 1, "win_t5": None},
            {"price": 8.21, "close_t1": 7.78, "win_t1": 0, "win_t5": None},
        ])
        since = pd.DataFrame([{"d": dt.date(2026, 10, 22)}])
        read_sql = MagicMock(side_effect=[frame, since, paper])
        conn = _Connection()
        with patch.object(slog, "ensure_table"), \
                patch("pandas.read_sql", read_sql), \
                patch.object(slog.qdb, "get_connection", return_value=conn):
            out = slog.stats_summary()
        self.assertEqual(out["since"], "2026-10-22")
        self.assertEqual(out["m3"]["n"], 2)
        self.assertEqual(out["m3"]["n_t1"], 2)
        self.assertEqual(out["m3"]["win1"], 50.0)         # 1 胜 1 负
        self.assertEqual(out["m3"]["avg_t1"], 0.0)        # (+5% + -5%)/2
        self.assertEqual(out["m3"]["conf"], 55.0)
        self.assertEqual(out["leader"]["n"], 1)
        self.assertIsNone(out["leader"]["win1"])          # 未回填
        self.assertEqual(out["sector"]["n"], 0)
        self.assertEqual(out["m3_push"]["n"], 3)          # 推送口径参考
        self.assertEqual(out["m3_push"]["win1"], 66.7)
        self.assertIsNone(out["m3_push"]["conf"])         # 推送表无可信度列

    def test_grade_sources_reflect_sample_shortfall(self):
        empty = pd.DataFrame()
        read_sql = MagicMock(side_effect=[empty, empty])
        with patch.object(slog, "ensure_table"), \
                patch("pandas.read_sql", read_sql), \
                patch.object(slog.qdb, "get_connection", return_value=_Connection()):
            out = slog.stats_summary()
        for key in ("m3", "leader", "sector"):
            self.assertEqual(out[key]["n"], 0)
            self.assertIsNone(out[key]["win1"])


if __name__ == "__main__":
    unittest.main()
