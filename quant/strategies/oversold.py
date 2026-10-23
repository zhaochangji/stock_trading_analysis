# -*- coding: utf-8 -*-
"""超跌反弹候选筛选(v1,未回测验证——如实标注)。

口径(全部可复核):
  * 60 日跌幅 ≤ -25%(深跌)
  * 今日资金:THS 即时全口径净流入 > 0(跌途中资金在买)
  * 今日涨跌幅 ∈ (-3%, +3%)(企稳中:不追高、不接刀)
  * 排除 ST / 北交所
输出候选(code/name/60日跌幅/今日涨跌/净流入/行业),按净流入排序。
注意:这不是已验证策略(M5 已证纯技术面负 alpha),仅作为"资金在深跌股中的
选择"证据池;任何买入仍受交易系统纪律约束(单票≤25%、试仓≤10%、-7% 止损)。
"""

import pandas as pd

from quant import db as qdb

MIN_DROP60 = -25.0
TODAY_RANGE = (-3.0, 3.0)


def oversold_candidates(top_n=8):
    eng = qdb.get_engine()
    px = pd.read_sql(
        "SELECT code,`date`,close FROM quant_hist_daily "
        "WHERE `date` >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)", con=eng)
    px["date"] = px["date"].astype(str)

    def drop60(g):
        g = g.sort_values("date")
        if len(g) < 40:
            return None
        return (g["close"].iloc[-1] / g["close"].iloc[0] - 1) * 100

    d60 = px.groupby("code").apply(drop60).dropna()
    flow = pd.read_sql("SELECT code,net,change_rate FROM quant_flow_realtime_today", con=eng)
    spot = pd.read_sql(
        "SELECT code,name,industry FROM cn_stock_spot WHERE `date`=CURDATE()", con=eng)
    df = flow.merge(spot, on="code", how="left")
    df["drop60"] = df["code"].map(d60)
    df = df[(df["drop60"] <= MIN_DROP60) & (df["net"] > 0)
            & (df["change_rate"].between(*TODAY_RANGE))]
    df = df[~df["name"].fillna("").str.contains("ST")
            & df["code"].str.match(r"^(60|00|30|68)")]
    df = df.sort_values("net", ascending=False).head(top_n)
    return df[["code", "name", "drop60", "change_rate", "net", "industry"]]


if __name__ == "__main__":
    df = oversold_candidates()
    if df.empty:
        print("今日无符合口径的超跌资金流入候选(不硬凑)")
    else:
        for _, r in df.iterrows():
            print(f"{r['code']} {r['name']} 60日{r['drop60']:+.1f}% "
                  f"今日{r['change_rate']:+.1f}% 净流入{r['net']/1e8:+.2f}亿 {r['industry']}")
