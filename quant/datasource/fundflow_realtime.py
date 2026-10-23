# -*- coding: utf-8 -*-
"""盘中实时资金流(同花顺即时口径)。

来源:同花顺数据中心「个股资金流-即时」页(akshare stock_fund_flow_individual),
全市场一次抓取 ~10 秒,本地缓存 3 分钟。
⚠口径说明(如实):
  * THS「净额」= 流入资金 − 流出资金(全单口径,含散单);
  * 东财「主力净额」= 超大单+大单净额(收盘后经 InStock 表可得,盘中历史接口不含当日);
  * 两者都是真实数据,口径不同,展示时必须标注,不混用。
失败返回 None,调用方降级显示,不影响主链。
"""

import time

import pandas as pd

_CACHE = {"ts": 0.0, "df": None}


def _parse_amount(s):
    """'2.50亿'/'-1476.61万'/数字 → 元(float);失败 None。"""
    try:
        s = str(s).strip().replace(",", "")
        if s.endswith("亿"):
            return float(s[:-1]) * 1e8
        if s.endswith("万"):
            return float(s[:-1]) * 1e4
        return float(s)
    except (TypeError, ValueError):
        return None


def _parse_pct(s):
    try:
        return float(str(s).rstrip("%"))
    except (TypeError, ValueError):
        return None


def ths_instant_flow(max_age=180):
    """返回 DataFrame:code/name/price/pct/net(元)/in/out/amount(元/亿混合已归元)。

    带全表缓存(默认 180 秒);失败重试一次,仍失败返回 None。
    """
    now = time.time()
    if _CACHE["df"] is not None and now - _CACHE["ts"] < max_age:
        return _CACHE["df"]
    import akshare as ak
    for attempt in (1, 2):
        try:
            df = ak.stock_fund_flow_individual(symbol="即时")
            if df is None or df.empty:
                raise RuntimeError("空数据")
            out = pd.DataFrame({
                "code": df["股票代码"].astype(str).str.zfill(6),
                "name": df["股票简称"],
                "price": pd.to_numeric(df["最新价"], errors="coerce"),
                "pct": df["涨跌幅"].map(_parse_pct),
                "net": df["净额"].map(_parse_amount),
                "in": df["流入资金"].map(_parse_amount),
                "out": df["流出资金"].map(_parse_amount),
                "amount": df["成交额"].map(_parse_amount),
            }).dropna(subset=["code"])
            _CACHE.update({"ts": now, "df": out})
            return out
        except Exception:  # noqa: BLE001
            if attempt == 2:
                return None
            time.sleep(2)
    return None


def cached_instant(max_age=1800):
    """只读现有缓存的全市场即时资金流;无缓存/过老返回 None。

    绝不主动抓取——供单股/少量股票场景(分析页、实时决策)读取
    战法页 payload 或调度已拉好的缓存,不在单股请求里发起全市场
    资金抓取(AGENTS 约束)。max_age 默认 30 分钟:THS 原始缓存
    3 分钟新鲜度,超龄后此处降级为不展示;调度喂表(10:00/11:00/
    13:05/14:00)与战法页 payload 都会刷新它,盘中通常不会超龄。
    """
    df = _CACHE["df"]
    if df is None or time.time() - _CACHE["ts"] > max_age:
        return None
    return df


def cached_flow_of(code):
    """单只股票的当日净额(元,THS 全单口径);缓存无/不在表 → None。"""
    df = cached_instant()
    if df is None:
        return None
    hit = df[df["code"] == str(code).zfill(6)]
    if hit.empty:
        return None
    return float(hit.iloc[0]["net"])


def fmt_flow_net(value):
    """资金净额(元)→ 人话:+1.23亿 / +3,023万 / -825万 / -12,300元。"""
    if value is None:
        return "—"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    if abs(value) >= 1e8:
        return f"{value / 1e8:+.2f}亿"
    if abs(value) >= 1e4:
        return f"{value / 1e4:+,.0f}万"
    return f"{value:+,.0f}元"


def instant_of(code):
    """单只股票的即时资金流 dict;无数据返回 None。"""
    df = ths_instant_flow()
    if df is None:
        return None
    hit = df[df["code"] == str(code).zfill(6)]
    if hit.empty:
        return None
    r = hit.iloc[0]
    rank_n = int((df["net"] > r["net"]).sum()) + 1
    return {
        "code": r["code"], "name": r["name"], "price": float(r["price"]),
        "pct": float(r["pct"]) if pd.notna(r["pct"]) else None,
        "net": float(r["net"]), "in_yi": r["in"] / 1e8, "out_yi": r["out"] / 1e8,
        "amount_yi": r["amount"] / 1e8,
        "rank": rank_n, "n": len(df),
    }


def main_flow_today(code, *, attempts=2, request_retries=3, timeout=10):
    """东财主力(超大单+大单)口径的**当日实时累计**资金流(分钟端为准)。

    实测结论(讲证据):
      * fflow/{daykline,kline} 的 lmt 参数必须为 0,lmt=N 会把当日累计行截掉;
      * 盘中恒等式精确成立:主力净额 f52 = 大单 f55 + 超大单 f56(逐分钟累计口径);
      * 分钟端(push2delay 域优先)在午休/收盘后仍保留当日累计行,daykline 上当日行时有时无。
    返回:dict(date, last_time, main_net/small_net/mid_net/big_net/huge_net(元),
    close, pct, is_today);失败/无当日行返回 None。
    """
    import os
    import sys as _sys
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        instock_dir = os.path.join(root, "instock")
        if instock_dir not in _sys.path:
            _sys.path.insert(0, instock_dir)
        from instock.core.eastmoney_fetcher import eastmoney_fetcher
        fetcher = eastmoney_fetcher()
    except Exception:  # noqa: BLE001
        return None
    secid = ("1." if str(code).startswith(("6", "9", "5")) else "0.") + str(code).zfill(6)
    today = pd.Timestamp.today().strftime("%Y-%m-%d")
    for host in ("push2delay", "push2"):
        url = f"http://{host}.eastmoney.com/api/qt/stock/fflow/kline/get"
        for _ in range(attempts):
            try:
                j = fetcher.make_request(url, params={
                    "lmt": "0", "klt": "1", "fields1": "f1,f2,f3,f7",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                    "ut": "b2884a393a59ad64002292a3e90d46a5",
                    "secid": secid}, retry=request_retries, timeout=timeout).json()
                kl = (j.get("data") or {}).get("klines") or []
                rows = [l.split(",") for l in kl if l.startswith(today)]
                if rows:
                    p = rows[-1]
                    return {
                        "date": today, "last_time": p[0],
                        "main_net": float(p[1]), "small_net": float(p[2]),
                        "mid_net": float(p[3]), "big_net": float(p[4]),
                        "huge_net": float(p[5]),
                        "main_pct": None,
                        "close": None, "pct": None,
                        "is_today": True,
                    }
            except Exception:  # noqa: BLE001
                time.sleep(1)
    return None
