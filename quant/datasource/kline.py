# -*- coding: utf-8 -*-
"""东财 push2his K线直连模块。

不经过 clist 接口(该接口高频后会被东财临时拉黑),直接用代码规则推算 secid:
  6 开头 → 沪市(market=1);0/3 开头 → 深市(market=0)
"""

import logging

from quant.lib.httputil import get_json

logger = logging.getLogger(__name__)


def to_secid(code: str) -> str:
    """A股代码 → 东财 secid。"""
    market = "1" if code.startswith("6") else "0"
    return f"{market}.{code}"


_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
# fields2: f51日期 f52开 f53收 f54高 f55低 f56成交量(手) f57成交额 f58振幅
#          f59涨跌幅 f60涨跌额 f61换手率
_PARAMS = {
    "fields1": "f1,f2,f3,f4,f5,f6",
    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    "klt": "101",   # 日K
    "fqt": "1",     # 前复权
    "ut": "fa5fd1943c7b386f172d6893dbfba10b",
}


def fetch_kline_em(code: str, start: str, end: str = None):
    """拉单只股票日线(前复权)。

    start/end: YYYYMMDD;返回 list[tuple] 或 None。
    列顺序与 quant_hist_daily 插入字段(不含 code/date 前缀)一致:
    (date, open, close, high, low, volume股, amount, amplitude, quote_change, ups_downs, turnover)
    """
    params = dict(_PARAMS)
    params["secid"] = to_secid(code)
    params["beg"] = start
    params["end"] = end or "20500101"
    try:
        j = get_json(_URL, params=params, timeout=15)
        if not j or not j.get("data") or not j["data"].get("klines"):
            return None
        out = []
        for line in j["data"]["klines"]:
            p = line.split(",")
            # p: 日期,开,收,高,低,量(手),额,振幅,涨跌幅,涨跌额,换手率
            out.append((p[0], _fv(p[1]), _fv(p[2]), _fv(p[3]), _fv(p[4]),
                        _fv(p[5], scale=100.0),   # 手 → 股
                        _fv(p[6]), _fv(p[7]), _fv(p[8]), _fv(p[9]), _fv(p[10])))
        # 历史表只存完整交易日:
        # 盘中(<15:05)丢弃今日未走完的K线;收盘后(>=15:05)保留今日完整K线,
        # 供每日增量任务写入当天数据(盘中实时数据归 M4 实时层)
        import datetime as _dt
        _now = _dt.datetime.now()
        today = _now.date().isoformat()
        keep_today = _now.hour > 15 or (_now.hour == 15 and _now.minute >= 5)
        out = [row for row in out if row[0] < today or (keep_today and row[0] == today)]
        return out
    except Exception as e:
        logger.debug(f"fetch_kline_em {code} 异常: {e}")
        return None


def _fv(v, scale: float = 1.0):
    """安全转 float;'-' → None。"""
    try:
        if v in ("-", "", None):
            return None
        f = float(v) * scale
        return int(f) if scale != 1.0 else f
    except (TypeError, ValueError):
        return None
