# -*- coding: utf-8 -*-
"""通达信数据源(quant.datasource.tdx)测试:全 mock,不连真实服务器。

2026-10-23 调研实测:K 线/五档接口已死(服务器协议变更),除权/分时/
财务可用——详见模块 docstring;测试锁住可用接口的转换与多服务器
重试逻辑(曾因 DataFrame bool 判断在第一台成功后仍连炸 5 台)。
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
from collections import OrderedDict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import quant.datasource.tdx as tdx  # noqa: E402


class _FakeApi:
    def __init__(self, xdxr=None, minute=None, finance=None):
        self._xdxr = xdxr
        self._minute = minute
        self._finance = finance
        self.disconnected = False

    def get_xdxr_info(self, market, code):
        return self._xdxr

    def get_history_minute_time_data(self, market, code, day):
        return self._minute

    def get_finance_info(self, market, code):
        return self._finance

    def disconnect(self):
        self.disconnected = True


class IsEmptyTest(unittest.TestCase):
    def test_none_list_dataframe(self):
        self.assertTrue(tdx._is_empty(None))
        self.assertTrue(tdx._is_empty([]))
        self.assertTrue(tdx._is_empty(pd.DataFrame()))
        self.assertFalse(tdx._is_empty([1]))
        self.assertFalse(tdx._is_empty(pd.DataFrame({"a": [1]})))
        self.assertFalse(tdx._is_empty({"k": 1}))     # dict 无 len==0 判断歧义
        self.assertFalse(tdx._is_empty("abc"))


class MarketTest(unittest.TestCase):
    def test_market_prefix(self):
        self.assertEqual(tdx._market("600977"), 1)      # 沪市主板
        self.assertEqual(tdx._market("002184"), 0)      # 深市主板
        self.assertEqual(tdx._market("300001"), 0)      # 创业板(深)
        self.assertEqual(tdx._market("688001"), 1)      # 科创板(沪)
        self.assertEqual(tdx._market("9"), 1)           # 9 开头沪
        self.assertEqual(tdx._market("510300"), 1)      # 沪 ETF


class FetchXdxrTest(unittest.TestCase):
    ROWS = [
        OrderedDict([("year", 2026), ("month", 7), ("day", 16),
                     ("category", 1), ("name", "除权除息"),
                     ("fenhong", 0.3199999928474426),
                     ("songzhuangu", 0.0), ("peigujia", 0.0),
                     ("peigu", 0.0)]),
        OrderedDict([("year", None), ("month", 8), ("day", 9),
                     ("category", 5), ("name", "股本变化"),
                     ("fenhong", None), ("songzhuangu", None),
                     ("peigujia", None), ("peigu", None)]),
    ]

    def test_converts_xdxr_rows(self):
        api = _FakeApi(xdxr=self.ROWS)
        with patch.object(tdx, "_hosts", return_value=[("s", "1.1.1.1", 7709)]), \
                patch.object(tdx, "_connect_one", return_value=api):
            out = tdx.fetch_xdxr("600977")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["date"], "2026-07-16")
        self.assertAlmostEqual(out[0]["fenhong"], 0.32, places=5)
        self.assertEqual(out[1]["date"], None)          # year 缺失
        self.assertIsNone(out[1]["fenhong"])
        self.assertTrue(api.disconnected)               # 短连接有断开

    def test_retries_until_nonempty(self):
        """第一台连不上/第二台空、第三台成功:返回第三台,前两台不炸。"""
        empty = _FakeApi(xdxr=[])
        good = _FakeApi(xdxr=self.ROWS)
        apis = [None, empty, good]
        with patch.object(tdx, "_hosts", return_value=[
                ("a", "1.1.1.1", 7709), ("b", "2.2.2.2", 7709),
                ("c", "3.3.3.3", 7709)]), \
                patch.object(tdx, "_connect_one",
                             side_effect=lambda ip, port: apis.pop(0)):
            out = tdx.fetch_xdxr("600977")
        self.assertEqual(len(out), 2)
        self.assertTrue(empty.disconnected)
        self.assertTrue(good.disconnected)

    def test_dataframe_result_not_bool_judged(self):
        """fetch_minute 返回 DataFrame 时不再触发 bool 异常重试浪费。"""
        api = _FakeApi(minute=[OrderedDict([("price", 10.5), ("vol", 100)])])
        with patch.object(tdx, "_hosts", return_value=[
                ("a", "1.1.1.1", 7709), ("b", "2.2.2.2", 7709)]), \
                patch.object(tdx, "_connect_one", return_value=api) as conn:
            frame = tdx.fetch_minute("002184", "2026-10-22")
        self.assertEqual(len(frame), 1)
        self.assertEqual(float(frame["price"].iloc[0]), 10.5)
        self.assertEqual(conn.call_count, 1)            # 首台成功即返回


class FetchMinuteTest(unittest.TestCase):
    def test_date_defaults_to_today_history_api(self):
        """date=None 走当日(历史分时接口),数据字段正确。"""
        rows = [OrderedDict([("price", 10.56), ("vol", 7604)]),
                OrderedDict([("price", 10.78), ("vol", 6640)])]
        api = _FakeApi(minute=rows)
        with patch.object(tdx, "_hosts", return_value=[("s", "1.1.1.1", 7709)]), \
                patch.object(tdx, "_connect_one", return_value=api):
            frame = tdx.fetch_minute("002184")
        self.assertEqual(len(frame), 2)
        self.assertEqual(list(frame.columns), ["price", "vol"])

    def test_date_with_dashes_normalized(self):
        api = _FakeApi(minute=[])
        with patch.object(tdx, "_hosts", return_value=[("s", "1.1.1.1", 7709)]), \
                patch.object(tdx, "_connect_one", return_value=api):
            self.assertIsNone(tdx.fetch_minute("002184", "2026-10-22"))


class MinuteCacheTest(unittest.TestCase):
    def test_caches_success_only(self):
        """成功结果 60 秒内复用(不重连);失败不缓存,下次重试。"""
        rows = [OrderedDict([("price", 10.5), ("vol", 100)])]
        api = _FakeApi(minute=rows)
        empty = _FakeApi(minute=[])
        apis = [api, empty]
        hosts = [("a", "1.1.1.1", 7709), ("b", "2.2.2.2", 7709)]
        with patch.object(tdx, "_hosts", return_value=hosts), \
                patch.object(tdx, "_connect_one",
                             side_effect=lambda ip, port: apis.pop(0)) as conn:
            frame1 = tdx.fetch_minute_cached("002184")
            frame2 = tdx.fetch_minute_cached("002184")      # 命中缓存
        self.assertEqual(len(frame1), 1)
        self.assertEqual(conn.call_count, 1)                 # 只连了一次
        # 清缓存后失败不缓存:再调返回 None 且不会拿旧值
        tdx._MINUTE_CACHE.pop(("002184", ""))
        with patch.object(tdx, "_hosts", return_value=hosts), \
                patch.object(tdx, "_connect_one", return_value=empty):
            frame3 = tdx.fetch_minute_cached("002184")
        self.assertIsNone(frame3)
        self.assertNotIn(("002184", ""), tdx._MINUTE_CACHE)  # 失败不进缓存


class FetchFinanceTest(unittest.TestCase):
    def test_finance_fields(self):
        api = _FakeApi(finance={"liutongguben": 1867000000.0,
                                "zongguben": 1867000000.0,
                                "industry": 18,
                                "updated_date": 20260825})
        with patch.object(tdx, "_hosts", return_value=[("s", "1.1.1.1", 7709)]), \
                patch.object(tdx, "_connect_one", return_value=api):
            out = tdx.fetch_finance("600977")
        self.assertEqual(out["liutongguben"], 1867000000.0)
        self.assertEqual(out["updated_date"], "20260825")
        self.assertIsNone(tdx._num("x"))               # 非数值 → None


class ProbeShapeTest(unittest.TestCase):
    def test_probe_reports_availability_dict(self):
        api = _FakeApi(xdxr=[1], minute=[1] * 240,
                       finance={"liutongguben": 1.0})
        api.get_security_list = MagicMock(return_value=[1] * 1000)
        api.get_security_bars = MagicMock(return_value=[])    # K 线已死
        api.get_security_quotes = MagicMock(return_value=[])
        with patch.object(tdx, "_hosts", return_value=[("s", "1.1.1.1", 7709)]), \
                patch.object(tdx, "_connect_one", return_value=api):
            out = tdx.probe()
        self.assertTrue(out["connect"])
        self.assertEqual(out["bars_kline"], 0)        # 期望死(协议变更)
        self.assertEqual(out["quotes"], 0)
        self.assertEqual(out["xdxr"], 1)


if __name__ == "__main__":
    unittest.main()
