# -*- coding: utf-8 -*-
"""M5 回测(技术面子集,S 信号 10 年验证)。

目的:验证当前 M3 规则里的技术面部分在历史数据上的事前胜率,校准 S 级阈值。
限制(如实说明):M3 的资金流因子(当日/5日/10日主力)无 10 年历史数据回填,
本回测只验证技术面子集。真上实现方式:
  * S_tech = 5 个技术因子中 ≥4 个:站上MA10、均线多头、MACD多/近3日金叉、
    创20日新高、量比∈[1.5,5];否决:RSI14>80、5日涨>15%、当日跌>7%
  * 信号日 = 当日收盘判定成立日(与实时盘同口径)
  * 胜率 = 收盘买入后 T+1 / T+5 收盘上涨的比例;统计 全部/分年/分周期时长
对照基线:同期全样本无条件 P(次日上涨)
用法: python scripts/backtest_m5.py [--start 2016-01-04] [--workers 8]
"""

import argparse
import datetime as dt
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import talib as tl  # noqa: E402
import quant.db as qdb  # noqa: E402


def stock_signals(g):
    """单只股票逐日算技术因子,返回信号日 DataFrame。"""
    g = g.sort_values("date").reset_index(drop=True)
    if len(g) < 30:
        return None
    o, c, h, l, v = (g["open"].values, g["close"].values, g["high"].values,
                     g["low"].values, g["volume"].values.astype(float))
    ma5, ma10, ma20, ma60 = (tl.SMA(c, t) for t in (5, 10, 20, 60))
    dif, dea, _ = tl.MACD(c, 12, 26, 9)
    rsi = tl.RSI(c, 14)
    high20_prev = pd.Series(h).shift(1).rolling(20).max().values
    prev_c = pd.Series(c).shift(1).values
    vol_ma5 = pd.Series(v).shift(1).rolling(5).mean().values
    vr = v / np.where(vol_ma5 > 0, vol_ma5, np.nan)

    trend_ok = c >= ma10
    bull = (ma5 > ma10) & (ma10 > ma20) & (c > ma60)
    macd_up = (dif > dea) | (pd.Series(dif - dea).rolling(3).min().values > 0)
    breakout = c > high20_prev
    vr_ok = (vr >= 1.5) & (vr <= 5)
    tech_score = trend_ok.astype(int) + bull.astype(int) + np.nan_to_num(macd_up).astype(bool).astype(int) \
        + breakout.astype(int) + np.nan_to_num(vr_ok).astype(bool).astype(int)
    chg = np.where(prev_c > 0, (c / prev_c - 1) * 100, 0.0)
    c5 = pd.Series(c).shift(5).values
    pct5 = np.where(c5 > 0, (c / c5 - 1) * 100, 0.0)

    sig = (tech_score >= 4) & (chg > -7) & (rsi <= 80) & (pct5 <= 15) & (vr > 0)
    df = g.loc[sig, ["code", "date", "close"]].copy()
    if df.empty:
        return None
    idx = df.index.values
    df["t1"] = np.where(idx + 1 < len(c), c[idx + 1], np.nan)
    df["t5"] = np.where(idx + 5 < len(c), c[idx + 5], np.nan)
    df["t10"] = np.where(idx + 10 < len(c), c[idx + 10], np.nan)
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-04")
    a = ap.parse_args()
    eng = qdb.get_engine()
    print("读取K线…", file=sys.stderr)
    hist = pd.read_sql(
        "SELECT code,`date`,`open`,`close`,`high`,`low`,`volume` FROM quant_hist_daily "
        "WHERE `date` >= %s", con=eng, params=(a.start,))
    hist["date"] = pd.to_datetime(hist["date"]).dt.strftime("%Y-%m-%d")
    print(f"{len(hist)} 行,{hist['code'].nunique()} 只", file=sys.stderr)

    sigs = []
    for code, g in hist.groupby("code", sort=False):
        try:
            df = stock_signals(g)
            if df is not None:
                sigs.append(df)
        except Exception:
            pass
    sig = pd.concat(sigs, ignore_index=True) if sigs else pd.DataFrame()
    if sig.empty:
        print("无信号")
        return
    sig["year"] = sig["date"].str[:4]
    sig["win1"] = sig["t1"] > sig["close"]
    sig["win5"] = sig["t5"] > sig["close"]

    # 基线:全样本无条件次日上涨概率
    up_all = (hist.sort_values("date").groupby("code")["close"].pct_change() > 0).mean()

    print(f"\n=== M5 技术面 S 信号回测({a.start} 起,{len(sig)} 个信号)===")
    print(f"整体:T+1 胜率 {sig['win1'].mean() * 100:.1f}% | T+5 胜率 {sig['win5'].mean() * 100:.1f}%"
          f" | 基线(全样本次日涨) {up_all * 100:.1f}%")
    gy = sig.groupby("year").agg(n=("close", "size"), w1=("win1", "mean"),
                                 w5=("win5", "mean"))
    print(gy.assign(w1=lambda d: (d.w1 * 100).round(1), w5=lambda d: (d.w5 * 100).round(1)).to_string())
    out = os.path.join(ROOT, "reports", "backtest_m5_signals.csv")
    sig.to_csv(out, index=False)
    print(f"信号明细: {out} ({len(sig)} 行)", file=sys.stderr)


if __name__ == "__main__":
    main()
