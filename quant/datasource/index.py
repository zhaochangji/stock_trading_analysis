# -*- coding: utf-8 -*-
"""大盘指数日线(东财 push2his,与个股K线同端点,限流宽容、已验证)。

不落库,按需拉取(指数只用于报告的大盘环境判断)。
"""

import datetime as _dt
import logging
import time

from quant.datasource import ths
from quant.lib.httputil import get_json

logger = logging.getLogger(__name__)

# 东财 secid(市场.代码)
_INDEX_SECID = {
    "sh000001": "1.000001",  # 上证指数
    "sz399001": "0.399001",  # 深证成指
    "sz399006": "0.399006",  # 创业板指
    "sh000688": "1.000688",  # 科创50
}

_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_PARAMS = {
    "fields1": "f1,f2,f3,f4,f5,f6",
    "fields2": "f51,f52,f53,f54,f55,f56,f57",
    "klt": "101",   # 日K
    "fqt": "0",     # 指数不复权
    "ut": "fa5fd1943c7b386f172d6893dbfba10b",
}


def fetch_index_daily(symbol: str = "sh000001", start: str = "20200101"):
    """指数日线(优先 THS 指数端点,回落东财;两者均有盘中丢弃当日未完成K线的守卫)。

    symbol: sh000001 / sz399001 / sz399006 / sh000688
    start:  YYYYMMDD
    返回 list[dict]: date/open/close/high/low/volume/amount/pct(相对前一日%)
    """
    if symbol not in _INDEX_SECID:
        raise ValueError(f"未知指数 {symbol},支持: {list(_INDEX_SECID)}")
    # ① THS 扶摇指数日K(主通道;/api/a-share-index/prices/historical,近5年)
    try:
        d0 = _dt.datetime.strptime(start, "%Y%m%d")
        start_ms = int(d0.timestamp() * 1000)
        end_ms = int(_dt.datetime.now().timestamp() * 1000)
        ths_code = _INDEX_SECID[symbol].split(".")[1] + "." + \
            ("SH" if symbol.startswith("sh") else "SZ")
        j = ths.index_hist(ths_code, start_ms, end_ms)
        kl = (j or {}).get("data", {}).get("item") or []
        if kl:
            out = []
            for it in kl:
                out.append({
                    "date": _dt.datetime.fromtimestamp(it["date_ms"] / 1000 + 8 * 3600).strftime("%Y-%m-%d"),
                    "open": float(it["open_price"]), "close": float(it["close_price"]),
                    "high": float(it["high_price"]), "low": float(it["low_price"]),
                    "volume": float(it.get("volume") or 0),
                    "amount": float(it.get("turnover") or 0),
                })
            for i in range(1, len(out)):
                prev = out[i - 1]["close"]
                out[i]["pct"] = (out[i]["close"] / prev - 1) * 100 if prev else 0.0
            if out:
                out[0]["pct"] = 0.0
            return _drop_incomplete_today(out)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"fetch_index_daily {symbol} THS 主通道异常,回落东财: {e}")
    # ② 东财回落(重试 3 次)
    last_err = None
    for _ in range(3):
        try:
            return _fetch_index_em(symbol, start)
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2)
    logger.warning(f"fetch_index_daily {symbol} 东财回落亦失败: {last_err}")
    return []


def _drop_incomplete_today(out):
    """历史完整性:盘中丢弃当日未完成K线(收盘后 >=15:05 保留)。"""
    now = _dt.datetime.now()
    today = now.date().isoformat()
    keep_today = now.hour > 15 or (now.hour == 15 and now.minute >= 5)
    return [r for r in out
            if r["date"] < today or (keep_today and r["date"] == today)]


def _fetch_index_em(symbol: str, start: str):
    params = dict(_PARAMS)
    params["secid"] = _INDEX_SECID[symbol]
    params["beg"] = start
    params["end"] = "20500101"
    j = get_json(_URL, params=params, timeout=15)
    if not j or not j.get("data") or not j["data"].get("klines"):
        return []
    out = []
    for line in j["data"]["klines"]:
        p = line.split(",")
        # p: 日期,开,收,高,低,量(手),额
        try:
            out.append({
                "date": p[0],
                "open": float(p[1]), "close": float(p[2]),
                "high": float(p[3]), "low": float(p[4]),
                "volume": float(p[5]), "amount": float(p[6]) if len(p) > 6 and p[6] else 0.0,
            })
        except (ValueError, IndexError):
            continue
    # 涨跌幅(与前收比)
    for i in range(1, len(out)):
        prev = out[i - 1]["close"]
        out[i]["pct"] = (out[i]["close"] / prev - 1) * 100 if prev else 0.0
    if out:
        out[0]["pct"] = 0.0
    return _drop_incomplete_today(out)
