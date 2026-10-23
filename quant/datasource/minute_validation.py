"""东财单股分钟 K 末行校验：只用于验证 THS 报价的时点，不替换 THS 价格。"""

from __future__ import annotations

import datetime as dt
import math

import requests


def last_minute_bar(code: str):
    """返回 {time, close} 或 None。失败/无成交量/时间异常时不猜测。"""
    if not len(code) == 6 or not code.isdigit():
        return None
    market = "1" if code.startswith("6") else "0"
    try:
        response = requests.get(
            "https://push2.eastmoney.com/api/qt/stock/trends2/get",
            params={"secid": f"{market}.{code}",
                    "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58", "ndays": 1},
            timeout=5)
        response.raise_for_status()
        data = response.json().get("data") or {}
        rows = data.get("trends") or []
        if data.get("code") != code or not rows:
            return None
        for line in reversed(rows):
            parts = line.split(",")
            if len(parts) < 7 or float(parts[5]) <= 0:
                continue
            at = dt.datetime.strptime(parts[0], "%Y-%m-%d %H:%M")
            close = float(parts[2])
            if math.isfinite(close) and close > 0:
                return {"time": at.isoformat(timespec="minutes"), "close": close}
    except (requests.RequestException, ValueError, TypeError, KeyError, IndexError):
        return None
    return None
