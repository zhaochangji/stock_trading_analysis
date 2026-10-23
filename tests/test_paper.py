"""模拟盘 T+N 追踪回填的单元测试；不访问真实数据库。"""

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

import quant.paper as paper


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


class UpdateTrackingTest(unittest.TestCase):
    def test_backfills_t0_t1_and_win_flags_from_history(self):
        trades = pd.DataFrame([
            {"id": 1, "code": "600977", "push_date": "2026-10-16", "rec_price": 14.40},
            {"id": 2, "code": "000001", "push_date": "2026-10-16", "rec_price": None},
        ])
        history = pd.DataFrame([
            {"code": "600977", "date": "2026-10-16", "close": 14.18},
            {"code": "600977", "date": "2026-10-19", "close": 14.50},
            {"code": "600977", "date": "2026-10-20", "close": 14.90},
        ])
        read_sql = MagicMock(side_effect=[trades, history])
        conn = _Connection()
        with patch.object(paper, "ensure_table"), \
                patch("pandas.read_sql", read_sql), \
                patch.object(paper, "spot_latest_closes",
                             return_value=({}, None)), \
                patch.object(paper.qdb, "get_connection", return_value=conn):
            result = paper.update_tracking()

        self.assertEqual(result, (1, 0))
        self.assertTrue(conn.committed)
        # 备选记录(rec_price 为空)不写追踪结果
        self.assertEqual(len(conn.cursor_obj.calls), 1)
        sql, args = conn.cursor_obj.calls[0]
        self.assertIn("close_t1", sql)
        self.assertEqual(args[0], 14.18)   # close_t0 推荐日收盘
        self.assertEqual(args[1], 14.50)   # close_t1 次日收盘
        self.assertIsNone(args[2])         # T+3 未到期
        self.assertEqual(args[5], 1)       # win_t1: 14.50 > 14.40
        self.assertIsNone(args[6])         # win_t5 未到期
        self.assertEqual(args[7], 1)       # WHERE id
        # 历史K线按相关代码+起始日期过滤,不再整表加载
        hist_sql = read_sql.call_args_list[1][0][0]
        self.assertIn("WHERE code IN", hist_sql)
        self.assertIn("'600977'", hist_sql)
        self.assertIn("2026-10-16", hist_sql)

    def test_empty_book_returns_zero_without_touching_db(self):
        empty = pd.DataFrame(columns=["id", "code", "push_date", "rec_price"])
        with patch.object(paper, "ensure_table"), \
                patch("pandas.read_sql", return_value=empty), \
                patch.object(paper.qdb, "get_connection",
                             side_effect=AssertionError("不应连接数据库")):
            self.assertEqual(paper.update_tracking(), (0, 0))

    def test_spot_fallback_fills_missing_t1_row(self):
        """东财断连当日 K 线缺行时,喂表收盘价兜底补 T+1(2026-10-22
        当日仅 114 只入库,10-21 信号 T+1 全空,用喂表终值即时回填)。"""
        trades = pd.DataFrame([
            {"id": 1, "code": "001283", "push_date": "2026-10-21",
             "rec_price": 52.08},
        ])
        history = pd.DataFrame([          # 只有 10-21 行,10-22 K 线缺失
            {"code": "001283", "date": "2026-10-21", "close": 51.47},
        ])
        spot = ({"001283": 50.20}, "2026-10-22")
        read_sql = MagicMock(side_effect=[trades, history])
        conn = _Connection()
        with patch.object(paper, "ensure_table"), \
                patch("pandas.read_sql", read_sql), \
                patch.object(paper, "spot_latest_closes", return_value=spot), \
                patch.object(paper.qdb, "get_connection", return_value=conn):
            result = paper.update_tracking()

        self.assertEqual(result, (0, 0))            # 50.20 < 52.08 未跑赢
        sql, args = conn.cursor_obj.calls[0]
        self.assertEqual(args[0], 51.47)            # close_t0 原有 K 线
        self.assertEqual(args[1], 50.20)            # close_t1 喂表兜底价
        self.assertEqual(args[5], 0)                # win_t1 = 0

    def test_spot_fallback_skips_codes_with_existing_row(self):
        """hist 已有当日行(东财成功的 114 只)不重复追加,喂表仅补缺。"""
        trades = pd.DataFrame([
            {"id": 1, "code": "600977", "push_date": "2026-10-21",
             "rec_price": 14.40},
        ])
        history = pd.DataFrame([
            {"code": "600977", "date": "2026-10-21", "close": 14.30},
            {"code": "600977", "date": "2026-10-22", "close": 14.52},
        ])
        spot = ({"600977": 99.99}, "2026-10-22")     # 应被忽略,不覆盖
        read_sql = MagicMock(side_effect=[trades, history])
        conn = _Connection()
        with patch.object(paper, "ensure_table"), \
                patch("pandas.read_sql", read_sql), \
                patch.object(paper, "spot_latest_closes", return_value=spot), \
                patch.object(paper.qdb, "get_connection", return_value=conn):
            paper.update_tracking()

        _, args = conn.cursor_obj.calls[0]
        self.assertEqual(args[1], 14.52)            # K 线优先,喂表不覆盖


if __name__ == "__main__":
    unittest.main()
