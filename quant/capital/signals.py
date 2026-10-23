# -*- coding: utf-8 -*-
"""主力资金三信号引擎(M2)。

输入:单只股票的 K线 DataFrame(date 升序,含指标)+ 资金流 DataFrame(date 升序)
输出:逐日信号标记(布尔列),供 M3 多共振引擎与 M5 回测消费。

信号定义(方案 V3):
  A · 温和吸筹:近5日主力净流入为正天数>=3,且累计主力净流入/累计成交额>=1.5%,
      且5日累计涨幅<3%(主力在潜伏,股价未启动)
  B · 放量启动:当日主力净流入>0,且量比>2,且收盘突破前20日最高
  C · 洗盘识别:当日回调(涨跌幅<0),但主力仍净流入,且缩量(量比<1),
      且收盘不破10日线(假摔洗盘,趋势未坏)
"""

import numpy as np
import pandas as pd

from quant.indicators import add_indicators

# 资金流字段名(与 quant.datasource.fundflow 输出对齐)
FF_COLS = ["date", "main_net", "small_net", "mid_net", "big_net", "huge_net",
           "main_pct", "small_pct", "mid_pct", "big_pct", "huge_pct",
           "close_ff", "pct_change_ff"]


def load_fundflow_df(rows):
    """fetch_fundflow_em 的行 → DataFrame(date 升序)。"""
    df = pd.DataFrame(rows, columns=FF_COLS)
    df["date"] = df["date"].astype(str)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def build_signal_frame(kline_df, fundflow_df):
    """合并K线+资金流+指标,产出逐日信号。

    kline_df: 列含 date/open/close/high/low/volume/amount…(date 为 str 或 datetime)
    fundflow_df: load_fundflow_df 的输出
    返回 DataFrame(信号列:sig_A/sig_B/sig_C);资金流缺失的日期信号为 False。
    """
    k = kline_df.copy()
    k["date"] = pd.to_datetime(k["date"].astype(str))
    k = k.sort_values("date").reset_index(drop=True)
    k = add_indicators(k)

    f = fundflow_df[["date", "main_net"]].copy()
    k = k.merge(f, on="date", how="left")
    k["main_net"] = k["main_net"].fillna(0.0)

    # 主力净流入占比口径:窗口累计净额 / 窗口累计成交额(K线 amount,元)
    amt = k["amount"].astype("double")

    # ---- 信号 A · 温和吸筹(5日窗口) ----
    pos_days = (k["main_net"] > 0).rolling(5).sum()
    cum_net = k["main_net"].rolling(5).sum()
    cum_amt = amt.rolling(5).sum()
    cum_pct = np.where(cum_amt > 0, cum_net / cum_amt * 100, 0.0)
    k["sig_A"] = (pos_days >= 3) & (cum_pct >= 1.5) & (k["close_pct_5d"] < 3.0)

    # ---- 信号 B · 放量启动(当日) ----
    k["sig_B"] = ((k["main_net"] > 0) & (k["vr"] > 2.0) & k["break_high20"])

    # ---- 信号 C · 洗盘识别(当日) ----
    pct_today = k["close"].pct_change() * 100
    k["sig_C"] = ((pct_today < 0) & (k["main_net"] > 0)
                  & (k["vr"] < 1.0) & k["above_ma10"].fillna(False))

    # 前60日为指标预热期(MA60/MACD 等不可靠)
    k.loc[:60, ["sig_A", "sig_B", "sig_C"]] = False
    return k


def summarize_signals(df):
    """信号点摘要打印(调试/验证用)。"""
    for sig in ("sig_A", "sig_B", "sig_C"):
        hits = df[df[sig]]
        print(f"  {sig}: {len(hits)} 个信号点")
        for _, r in hits.tail(3).iterrows():
            print(f"    {r['date'].date()} close={r['close']} vr={r['vr']:.2f} "
                  f"main_net={r['main_net']/1e8:+.2f}亿")
