# -*- coding: utf-8 -*-
"""M3 多共振选股引擎(全市场扫描)。

数据全部来自本地 MySQL,不发起网络请求:
  - quant_hist_daily   : K线 → 技术共振(均线/MACD/突破/RSI)
  - cn_stock_fund_flow  : 当日/5日/10日主力净额与占比 → 资金共振
  - cn_stock_spot       : 量比/换手/ROE/PE/60日涨幅/行业 → 辅助与基本面
  - config.config.yaml  : 范围(北交所排除)、持仓

共振因子(布尔,命中记 1 分):
  trend_ok   收盘≥MA10(短期趋势未坏)
  bull       均线多头(MA5>MA10>MA20 且 close>MA60)
  macd_up    DIF>DEA(MACD 红柱区或近3日金叉)
  flow_today 当日主力净流入
  flow5      5日主力累计净流入且占比≥1.5%(吸筹特征)
  flow10     10日主力累计净流入且占比≥1.5%(持续吸筹)
  breakout   收盘突破前20日高(启动特征)
  vr_ok      量比∈[1.5,5](放量适度,非爆量出货)

一票否决(任一命中即不入选):
  RSI14>80 超买 / 5日累计涨幅>15% 追高 / ST / 北交所 / 上市<60日 / 停牌(今日无成交)

分级:S ≥4 分且含 flow5 或 breakout;A ≥3 分;B ≥2 分。
长短线:breakout 或 量比≥2 → 短线;flow5&flow10 且 ROE≥10 且 PE∈(0,40] 且 60日涨幅<30% → 长线。
"""

import datetime as dt
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import quant.db as qdb  # noqa: E402
from quant.indicators import add_indicators  # noqa: E402


def _load_universe_cfg():
    import yaml
    with open(os.path.join(ROOT, "config", "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _compute_tech(engine, recent_days=200):
    """从 quant_hist_daily 批量计算技术共振(截至表内最新交易日)。"""
    sql = ("SELECT code,`date`,open,close,high,low,volume,amount "
           "FROM quant_hist_daily WHERE `date` >= DATE_SUB(CURDATE(), INTERVAL %d DAY) "
           "ORDER BY code,`date`") % recent_days
    hist = pd.read_sql(sql, con=engine)
    rows = []
    for code, g in hist.groupby("code", sort=False):
        g = g.reset_index(drop=True)
        if len(g) < 65:  # MA60 需要完整窗口
            continue
        g = add_indicators(g)
        last = g.iloc[-1]
        # 近3日是否金叉
        golden3 = bool(g["macd_golden"].iloc[-3:].any())
        rows.append({
            "code": code,
            "hist_date": str(last["date"])[:10],
            "close": float(last["close"]),
            "ma_bull": bool(last["ma_bull"]),
            "above_ma10": bool(last["above_ma10"]),
            "macd_up": bool(last["dif"] > last["dea"]) or golden3,
            "break_high20": bool(last["break_high20"]),
            "high20_prev": float(last["high20_prev"]) if not pd.isna(last["high20_prev"]) else np.nan,
            "rsi14": float(last["rsi14"]) if not pd.isna(last["rsi14"]) else 50.0,
            "close_pct_5d": float(last["close_pct_5d"]) if not pd.isna(last["close_pct_5d"]) else 0.0,
        })
    return pd.DataFrame(rows)


def scan(top_n=30, return_stats=False):
    """全市场扫描,返回分级结果 DataFrame(return_stats=True 时附 (df, stats))。"""
    engine = qdb.get_engine()
    cfg = _load_universe_cfg()
    holdings = {h["code"] for h in cfg.get("holdings") or []}

    spot = pd.read_sql(
        "SELECT code,name,new_price,change_rate,volume_ratio,turnoverrate,"
        "roe_weight,pe,speed_increase_60,industry,listing_date,total_market_cap "
        "FROM cn_stock_spot", con=engine)
    flow = pd.read_sql(
        "SELECT code,fund_amount,fund_amount_5,fund_rate_5,fund_amount_10,fund_rate_10 "
        "FROM cn_stock_fund_flow", con=engine)
    tech = _compute_tech(engine)

    df = spot.merge(flow, on="code", how="inner").merge(tech, on="code", how="inner")

    # ---- 范围过滤(与 user 确认的 universe 一致) ----
    df = df[df["code"].str.match(r"^(60|00|30|68)")].copy()          # 主板+创业+科创,排除北交所
    df = df[~df["name"].str.contains("ST", case=False, na=False)]    # 排除 ST
    if df["listing_date"].notna().any():
        lmin = pd.to_datetime(df["listing_date"], errors="coerce")
        df = df[~(lmin > pd.Timestamp(dt.date.today() - dt.timedelta(days=60)))]
    df = df[pd.to_numeric(df["new_price"], errors="coerce") > 0]     # 排除停牌

    # ---- 共振因子 ----
    df["trend_ok"] = df["above_ma10"]
    df["bull"] = df["ma_bull"]
    df["macd_up"] = df["macd_up"]
    df["flow_today"] = df["fund_amount"] > 0
    df["flow5"] = (df["fund_amount_5"] > 0) & (df["fund_rate_5"] >= 1.5)
    df["flow10"] = (df["fund_amount_10"] > 0) & (df["fund_rate_10"] >= 1.5)
    df["breakout"] = df["break_high20"]
    vr = pd.to_numeric(df["volume_ratio"], errors="coerce").fillna(1.0)
    df["vr_ok"] = (vr >= 1.5) & (vr <= 5)

    factors = ["trend_ok", "bull", "macd_up", "flow_today", "flow5", "flow10", "breakout", "vr_ok"]
    df["score"] = df[factors].sum(axis=1)

    # ---- 板块共振(第 9 维,推荐分独立加 0.5):THS 指数实时 → THS快照聚合 → 东财昨日,三级回落 ----
    from quant.datasource import sector as _sector
    try:
        bench = _sector.code_heat_map(engine)
    except Exception:  # noqa: BLE001
        bench = {}
    df["sector_ok"] = False
    df["sector_str"] = "板块层未开(数据缺失)" if not bench else "板块未匹配"

    def _fmt_sec(s):
        if not s:
            return "板块未匹配"
        tag = "共振" if s.get("resonant") else "弱"
        src = s.get("source")
        if src == "THS指数实时":
            return (f"THS指数:{s['name']} {s['pct']:+.2f}%(排{s['rank']}/{s['n']},{tag})")
        if src == "THS实时":
            return (f"THS聚合:{s['industry']} 中位{s['pct_mid']:+.2f}%"
                    f"(排{s['rank']}/{s['n']},上涨家{s['up_ratio'] * 100:.0f}%,{tag})")
        return (f"东财昨日:{s.get('industry', s.get('name', ''))}"
                f" 主力{s['fund_amount_yi']:+.1f}亿(排{s['rank']}/{s['n']},{tag})")

    _secs = []
    for _code, _ind in zip(df["code"], df["industry"]):
        s = bench.get(_code)
        if not s and isinstance(_ind, str) and _ind:
            s = bench.get(_sector._clean_name(_ind))
        _secs.append(s)
    if any(_secs):
        df["sector_ok"] = [bool(s and s.get("resonant")) for s in _secs]
        df["sector_str"] = [_fmt_sec(s) for s in _secs]
        df["sector_src"] = [s.get("source") if s else "" for s in _secs]
    else:
        df["sector_src"] = ""

    # ---- 一票否决 ----
    chg = pd.to_numeric(df["change_rate"], errors="coerce").fillna(0)
    veto = (df["rsi14"] > 80) | (df["close_pct_5d"] > 15) | (chg < -7)
    df = df[~veto]

    # ---- 分级(S 级稀缺化:高分 + 当日主力净流入 + 当日不深跌) ----
    def _grade(r):
        if (r["score"] >= 5 and r["flow_today"]
                and r["change_rate"] > -3 and (r["flow5"] or r["breakout"])):
            return "S"
        if r["score"] >= 3:
            return "A"
        if r["score"] >= 2:
            return "B"
        return ""
    df["grade"] = df.apply(_grade, axis=1)
    df = df[df["grade"] != ""]
    # P0 修订(2026-10-14,依据:M5 回测显示纯技术 S 技术面为负 alpha):
    # S 级每日封顶 10 只(按 推荐分近似序 共振+板块加成 排序),其余降级 A,
    # 维护 "S=高确信稀缺" 的语义,直到 M5 用模拟盘数据校准后放开。
    _cap_rank = (df["score"] * 1.25 - df.get("deduct", 0) + df["sector_ok"] * 0.5)\
        .rank(ascending=False, method="first")
    _demote = (df["grade"] == "S") & (_cap_rank > 10)
    df.loc[_demote, "grade"] = "A"
    # 重要:多次布尔过滤后索引不连续,后续向量运算必须先重置,
    # 否则与过滤前计算的 Series 索引对齐会产生并集,形状错位
    df = df.reset_index(drop=True)

    # ---- 长短线分类 ----
    vr = pd.to_numeric(df["volume_ratio"], errors="coerce").fillna(1.0)
    roe = pd.to_numeric(df["roe_weight"], errors="coerce").fillna(0)
    pe = pd.to_numeric(df["pe"], errors="coerce")
    spd60 = pd.to_numeric(df["speed_increase_60"], errors="coerce").fillna(0)
    rsi_num = pd.to_numeric(df["rsi14"], errors="coerce").fillna(50)
    short_c = df["breakout"] | ((vr >= 2) & (pd.to_numeric(df["change_rate"], errors="coerce").fillna(0) > 0))
    long_c = df["flow5"] & df["flow10"] & (roe >= 10) & pe.between(0, 40) & (spd60 < 30)
    df["horizon"] = np.where(short_c, "短线", np.where(long_c, "长线", "波段"))

    # ---- 推荐分(10分制,公式透明可复核):共振分(满8)×1.25 − 明确扣分项 + 板块共振0.5 ----
    # 扣分:RSI14>70 重复超买 −1.0;当日涨幅>7% 追高风险 −0.5;60日涨幅>30% 位置偏高 −0.5
    chg_num = pd.to_numeric(df["change_rate"], errors="coerce").fillna(0)
    ded = ((rsi_num > 70) * 1.0 + (chg_num > 7) * 0.5 + (spd60 > 30) * 0.5)
    df["deduct"] = ded
    df["score10"] = ((df["score"] * 1.25 - ded + df["sector_ok"] * 0.5)
                     .clip(0, 10).round(1))

    # 持仓标记
    df["is_holding"] = df["code"].isin(holdings)

    # 全貌统计(必须在 head() 截断前计算)
    stats = {
        "total": int(len(df)),
        "by_grade": df["grade"].value_counts().to_dict(),
        "by_horizon": df["horizon"].value_counts().to_dict(),
    }

    # ---- 排序输出(S 在前) ----
    _gk = {"S": 0, "A": 1, "B": 2}
    df = df.assign(_g=df["grade"].map(_gk)).sort_values(
        ["_g", "score", "fund_rate_5"], ascending=[True, False, False]).head(top_n)
    cols = ["code", "name", "industry", "grade", "horizon", "score", "score10", "deduct",
            "close", "change_rate", "fund_amount", "fund_rate_5", "fund_rate_10",
            "fund_amount_5", "fund_amount_10", "volume_ratio", "rsi14", "is_holding", "hist_date",
            "trend_ok", "bull", "macd_up", "flow_today", "flow5", "flow10", "breakout", "vr_ok",
            "sector_ok", "sector_str"]
    out = df[cols].copy()
    out["fund_amount"] = (out["fund_amount"] / 1e8).round(2)       # 亿
    out["fund_amount_5"] = (out["fund_amount_5"] / 1e8).round(2)   # 亿
    out["fund_amount_10"] = (out["fund_amount_10"] / 1e8).round(2)  # 亿
    out["change_rate"] = out["change_rate"].round(2)
    out["fund_rate_5"] = out["fund_rate_5"].round(2)
    out["fund_rate_10"] = out["fund_rate_10"].round(2)
    out["volume_ratio"] = pd.to_numeric(out["volume_ratio"], errors="coerce").round(2)

    # ---- 选定依据:逐条列出实际命中的因子与数值(证据列) ----
    def _evidence(r):
        parts = []
        if bool(r["sector_ok"]):
            parts.append(f"板块共振[{r['sector_str']}]")
        if r["flow5"]:
            parts.append(f"5日吸筹{r['fund_rate_5']:+.1f}%/{r['fund_amount_5']:.2f}亿")
        if r["flow10"]:
            parts.append(f"10日吸筹{r['fund_rate_10']:+.1f}%/{r['fund_amount_10']:.2f}亿")
        if r["flow_today"]:
            parts.append(f"当日主力{r['fund_amount']:+.2f}亿")
        if r["breakout"]:
            parts.append("创20日新高")
        if r["macd_up"]:
            parts.append("MACD金叉")
        if r["bull"]:
            parts.append("多头排列")
        if r["trend_ok"]:
            parts.append("站上MA10")
        if r["vr_ok"]:
            parts.append(f"量比{r['volume_ratio']:.1f}")
        return "+".join(parts) or "仅基础共振"
    if not out.empty:
        out["evidence"] = [_evidence(r) for _, r in out.iterrows()]
    else:
        out["evidence"] = []
    if return_stats:
        return out.reset_index(drop=True), stats
    return out.reset_index(drop=True)


def render(df, note="", stats=None):
    lines = ["### M3 全市场多共振扫描", ""]
    if stats:
        g = stats.get("by_grade", {})
        h = stats.get("by_horizon", {})
        lines.append(
            f"**全貌**:命中 {stats.get('total', 0)} 只 —— "
            f"S {g.get('S', 0)} / A {g.get('A', 0)} / B {g.get('B', 0)};"
            f"短线 {h.get('短线', 0)} / 波段 {h.get('波段', 0)} / 长线 {h.get('长线', 0)}"
            "(下表为分级排序 Top30)")
        lines.append("")
    if df.empty:
        lines.append("今日无满足共振条件的标的(不硬凑)。")
        return "\n".join(lines)
    lines.append("| 代码 | 名称 | 行业 | 级 | 线 | 共振/8 | 推荐分/10 | 现价 | 涨跌% | 量比 | 选定依据 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for _, r in df.iterrows():
        hold = "★" if r["is_holding"] else ""
        lines.append(
            f"| {r['code']} | {r['name']}{hold} | {str(r['industry'])[:8]} | **{r['grade']}** | {r['horizon']} "
            f"| {int(r['score'])}/8 | **{r['score10']}** | {r['close']:.2f} | {r['change_rate']:+.1f} "
            f"| {r['volume_ratio']:.1f} | {r.get('evidence', '')} |")
    if note:
        lines.append("")
        lines.append(f"> {note}")
    lines.append("")
    lines.append("> **推荐分公式**(10分制,可复核):共振因子(0~8:站上MA10/多头排列/MACD多/当日主力流入/"
                 "5日吸筹≥1.5%/10日吸筹≥1.5%/创20日新高/量比1.5-5)×1.25")
    lines.append("> − 扣分:RSI14>70 时 −1.0;当日涨幅>7% 时 −0.5;60日涨幅>30% 时 −0.5。"
                 "**板块共振 +0.5**:所属行业「THS实时」口径(全市场快照按行业聚合:中位涨幅>0 且涨幅排名前1/3)"
                 ";THS 聚合不可用时回落「东财昨日」主力净流入口径并在依据中标注。")
    lines.append("> 级别映射:S=共振≥5/8(推荐≥6.25)且当日主力流入、当日跌幅≤3%、含吸筹或突破;A=共振≥3/8;B=共振≥2/8。长线另需 ROE≥10、PE≤40、60日涨幅<30。")
    lines.append("> 规则化筛选结果,不构成投资建议。")
    return "\n".join(lines)


def main():
    df, stats = scan(return_stats=True)
    text = render(df, stats=stats,
                  note=f"扫描时间 {dt.datetime.now():%Y-%m-%d %H:%M};K线截至表内最新交易日")
    print(text)
    out = os.path.join(ROOT, "reports", f"scan_{dt.date.today():%Y-%m-%d}.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"[saved] {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
