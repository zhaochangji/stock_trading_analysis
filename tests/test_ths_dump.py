# -*- coding: utf-8 -*-
"""THS dump 批量回补层(三层链第二层)测试。"""

import datetime as dt
import io
import os
import sys
import unittest
from unittest.mock import patch

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import quant.datasource.ths_dump as td  # noqa: E402


def _raw_frame(rows):
    """模拟 dump 原始 parquet 结构(thscode/date_ms/..._price/turnover)。"""
    return pd.DataFrame(rows, columns=[
        "thscode", "currency", "interval", "adjusted", "date_ms",
        "open_price", "high_price", "low_price", "close_price",
        "volume", "turnover"])


class DownloadDailyKTest(unittest.TestCase):
    def _run(self, rows):
        parquet = _raw_frame(rows).to_parquet(index=False)

        class _Resp:
            content = parquet
            status_code = 200

            def raise_for_status(self):
                pass

        with patch("quant.datasource.ths.dump_download_url",
                   return_value="https://o.thsi.cn/x.parquet"), \
                patch("requests.get", return_value=_Resp()):
            return td.download_daily_k()

    def test_download_cleans_columns_and_tz(self):
        # date_ms=1791763200000 → 2026-10-12 00:00 UTC → 东八区 10-12
        frame = self._run([
            ["002184.SZ", "CNY", "1d", "none", 1791763200000,
             10.1, 10.5, 10.0, 10.3, 1.04e8, 1.07e9]])
        self.assertIsNotNone(frame)
        self.assertEqual(frame.iloc[0]["code"], "002184")
        self.assertEqual(frame.iloc[0]["date"], "2026-10-12")
        self.assertAlmostEqual(frame.iloc[0]["close"], 10.3)
        self.assertIn("amount", frame.columns)
        self.assertNotIn("thscode", frame.columns)

    def test_download_failure_paths_return_none(self):
        with patch("quant.datasource.ths.dump_download_url",
                   return_value=""):
            self.assertIsNone(td.download_daily_k())
        with patch("quant.datasource.ths.dump_download_url",
                   return_value="https://o.thsi.cn/x.parquet"), \
                patch("requests.get", side_effect=RuntimeError("net")):
            self.assertIsNone(td.download_daily_k())

        empty = _raw_frame([])
        parquet = empty.to_parquet(index=False)

        class _Resp:
            content = parquet

            def raise_for_status(self):
                pass

        with patch("quant.datasource.ths.dump_download_url",
                   return_value="u"), \
                patch("requests.get", return_value=_Resp()):
            self.assertIsNone(td.download_daily_k())   # 空数据


class ImportMissingTest(unittest.TestCase):
    FRAME = pd.DataFrame([
        {"code": "002184", "date": "2026-10-20", "open": 10.1, "close": 10.3,
         "high": 10.5, "low": 10.0, "volume": 1.04e8, "amount": 1.07e9},
        {"code": "002164", "date": "2026-10-19", "open": 14.5, "close": 14.69,
         "high": 14.7, "low": 14.4, "volume": 9.9e7, "amount": 1.4e9},
    ])

    def test_inserts_only_missing_and_reports_covered(self):
        existing = pd.DataFrame([["002184", "2026-10-20"]],
                                columns=["code", "date"])
        inserted = []

        def fake_insert(rows):
            inserted.extend(rows)
        with patch("pandas.read_sql", return_value=existing), \
                patch("quant.datasource.hist_store.insert_rows",
                      side_effect=fake_insert):
            n, latest, covered = td.import_missing(self.FRAME.copy())
        self.assertEqual(n, 1)                       # 已有 002184 不碰
        self.assertEqual(inserted[0][0], "002164")
        self.assertEqual(latest, "2026-10-20")
        self.assertEqual(covered, {"002184"})        # 002164 未到最新日

    def test_existing_rows_with_real_db_date_type_are_not_replaced(self):
        """生产 read_sql 返回 datetime.date,帧 date 是 "YYYY-MM-DD" 字符串:
        两者必须能比对,否则「只插缺失行」失效,每次 dump 全量 REPLACE
        覆盖已有行(2026-10-21/22 两晚 5.5 万行全量覆盖即此因)。"""
        existing = pd.DataFrame(
            [["002184", dt.date(2026, 10, 20)], ["002164", dt.date(2026, 10, 19)]],
            columns=["code", "date"])
        with patch("pandas.read_sql", return_value=existing), \
                patch("quant.datasource.hist_store.insert_rows") as ins:
            n, latest, covered = td.import_missing(self.FRAME.copy())
        self.assertEqual(n, 0)                        # 已有行一个都不覆盖
        self.assertFalse(ins.called)

    def test_intraday_guard_skips_today(self):
        existing = pd.DataFrame([], columns=["code", "date"])
        frame = self.FRAME.copy()
        intraday = dt.datetime(2026, 10, 20, 10, 0)   # 收盘前
        with patch("pandas.read_sql", return_value=existing), \
                patch("quant.datasource.hist_store.insert_rows") as ins:
            n, latest, covered = td.import_missing(frame, now=intraday)
        # 今日行(002184@10-20)被盘中保护剔除,历史行(002164@10-19)允许写
        self.assertEqual(n, 1)
        self.assertEqual(ins.call_args[0][0][0][0], "002164")
        self.assertEqual(latest, "2026-10-19")        # 最新有效日为昨日
        # 收盘后(15:05 后)允许写今日
        after = dt.datetime(2026, 10, 20, 15, 30)
        with patch("pandas.read_sql", return_value=existing), \
                patch("quant.datasource.hist_store.insert_rows") as ins:
            n, latest, covered = td.import_missing(frame, now=after)
        self.assertEqual(n, 2)
        self.assertTrue(ins.called)


class RescueTest(unittest.TestCase):
    def test_small_fallback_keeps_original_for_precision(self):
        # 少量 fallback = 真除权/新股,直接走东财保前复权,dump 不触发
        codes = ["600001", "600002"]
        with patch.object(td, "download_daily_k") as dl:
            result = td.rescue_with_dump(codes, threshold=300)
        self.assertEqual(result, codes)
        self.assertFalse(dl.called)

    def test_large_fallback_uses_dump_and_filters_covered(self):
        codes = [f"6000{i:03d}" for i in range(400)]
        frame = pd.DataFrame([
            {"code": c, "date": "2026-10-20", "open": 1.0, "close": 1.0,
             "high": 1.0, "low": 1.0, "volume": 1.0, "amount": 1.0}
            for c in codes[:350]])                    # 350 只已到最新日
        with patch.object(td, "download_daily_k", return_value=frame), \
                patch.object(td, "import_missing",
                             return_value=(350, "2026-10-20", set(codes[:350]))):
            remaining = td.rescue_with_dump(codes, threshold=300)
        self.assertEqual(len(remaining), 50)           # 剩 50 只走东财
        self.assertNotIn(codes[0], remaining)
        self.assertIn(codes[399], remaining)

    def test_dump_unavailable_falls_back_to_eastmoney(self):
        codes = [f"6000{i:03d}" for i in range(400)]
        with patch.object(td, "download_daily_k", return_value=None):
            self.assertEqual(td.rescue_with_dump(codes, threshold=300), codes)

    def test_dump_behind_target_date_keeps_all_for_eastmoney(self):
        """当日快照大面积失败(THS 限流)时,dump 文件只到昨日(当日隔天才
        进文件)。「已到 dump 最新日」≠已到目标日,不能据此清空东财清单,
        否则当日 K 线整批断档(2026-10-21 故障:全市场当日仅 714 只)。"""
        codes = [f"6000{i:03d}" for i in range(400)]
        with patch.object(td, "download_daily_k", return_value=pd.DataFrame([1])), \
                patch.object(td, "import_missing",
                             return_value=(400, "2026-10-20", set(codes))):
            remaining = td.rescue_with_dump(codes, threshold=300,
                                             target_date=dt.date(2026, 10, 21))
        self.assertEqual(remaining, codes)            # 全量保留给东财补当日

    def test_dump_catches_target_date_filters_covered_normally(self):
        """dump 已覆盖到目标日(历史盲区场景):covered 剔除照常生效。"""
        codes = [f"6000{i:03d}" for i in range(400)]
        with patch.object(td, "download_daily_k", return_value=pd.DataFrame([1])), \
                patch.object(td, "import_missing",
                             return_value=(350, "2026-10-21", set(codes[:350]))):
            remaining = td.rescue_with_dump(codes, threshold=300,
                                             target_date=dt.date(2026, 10, 21))
        self.assertEqual(len(remaining), 50)

    def test_rescue_failed_returns_uncovered(self):
        frame_ok = {"600001", "600002"}
        with patch.object(td, "download_daily_k", return_value=pd.DataFrame([1])), \
                patch.object(td, "import_missing",
                             return_value=(2, "2026-10-20", frame_ok)):
            remaining = td.rescue_failed(["600001", "600002", "600003"])
        self.assertEqual(remaining, ["600003"])
        with patch.object(td, "download_daily_k", return_value=None):
            self.assertEqual(td.rescue_failed(["600009"]), ["600009"])
        self.assertEqual(td.rescue_failed([]), [])


class BackfillWiringTest(unittest.TestCase):
    """backfill_hist 脚本已接入三层链(dump 救场/尾救)。"""

    def test_script_imports_dump_layer(self):
        source = open(os.path.join(ROOT, "scripts", "backfill_hist.py"),
                      encoding="utf-8").read()
        self.assertIn("rescue_with_dump", source)
        self.assertIn("rescue_failed", source)       # 东财失败尾救
        self.assertIn("THS 快照→dump→东财" in source or
                      "THS 快照 → THS dump → 东财" in source or
                      "dump 批量层", source, "三层链说明缺失")


if __name__ == "__main__":
    unittest.main()
