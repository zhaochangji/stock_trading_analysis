# -*- coding: utf-8 -*-
"""技术指标库(talib,口径对齐同花顺/通达信)。

约定:输入 DataFrame 按 date 升序;所有指标不引入未来函数
(量比、20日新高等使用 shift(1) 的历史窗口)。
"""

import numpy as np
import talib


def add_indicators(df):
    """就地添加指标列并返回 df。

    必需列:close, high, low, volume
    生成列:
      ma5/10/20/60            均线
      ma_bull                 均线多头排列(ma5>ma10>ma20 且 close>ma60)
      dif/dea/macd_hist       MACD
      macd_golden             MACD 金叉(当日 dif 上穿 dea)
      rsi6/rsi14              RSI
      vr                      量比(当日量 / 前5日均量)
      high20_prev             前20日最高价(不含当日)
      break_high20            当日收盘突破前20日高
      close_pct_5d             5日累计涨跌幅%
      above_ma10              收盘不破10日线
    """
    close = df["close"].values.astype("double")
    high = df["high"].values.astype("double")

    for n in (5, 10, 20, 60):
        df[f"ma{n}"] = talib.MA(close, timeperiod=n)
    df["ma_bull"] = (df["ma5"] > df["ma10"]) & (df["ma10"] > df["ma20"]) & (df["close"] > df["ma60"])

    dif, dea, hist = talib.MACD(close, 12, 26, 9)
    df["dif"], df["dea"], df["macd_hist"] = dif, dea, hist
    df["macd_golden"] = (df["dif"] > df["dea"]) & (df["dif"].shift(1) <= df["dea"].shift(1))

    df["rsi6"] = talib.RSI(close, 6)
    df["rsi14"] = talib.RSI(close, 14)

    vol = df["volume"].astype("double")
    vol_ma5_prev = vol.shift(1).rolling(5).mean()   # 前5日均量(不含当日)
    df["vr"] = vol / vol_ma5_prev

    df["high20_prev"] = df["high"].astype("double").rolling(20).max().shift(1)
    df["break_high20"] = df["close"] > df["high20_prev"]

    df["close_pct_5d"] = df["close"].pct_change(5) * 100

    df["above_ma10"] = df["close"] >= df["ma10"]
    return df
