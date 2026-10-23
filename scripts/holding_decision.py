# -*- coding: utf-8 -*-
"""盘中持仓决策(M4 尾盘件):实时行情拼当日bar → 技术位 + 当日主力 + M2信号 → 操作建议。

用法: python scripts/holding_decision.py   (盘中/尾盘运行,输出到 stdout)
建议分档: 躺平持有 / 持有(冲高可部分止盈) / 减仓 / 清仓警戒 / 关注回踩加仓
所有输出为规则化信号,不构成投资建议。
"""

import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd  # noqa: E402

import quant.db as qdb  # noqa: E402
from quant.capital.signals import build_signal_frame  # noqa: E402
from quant.datasource import ths  # noqa: E402
from quant.datasource.fundflow import fetch_fundflow_em  # noqa: E402
from quant.indicators import add_indicators  # noqa: E402

FF_COLS = ["date", "main_net", "small_net", "mid_net", "big_net", "huge_net",
           "main_pct", "small_pct", "mid_pct", "big_pct", "huge_pct",
           "close_ff", "pct_change_ff"]


def _fundflow_df(code, today):
    """资金流 DataFrame:本地库 120 日(零网络,快) + 今日实时主力(东财大单口径)。

    盘中速度优先:cn_stock_fund_flow 表即东财同源每日数据,收盘作业会更新,
    直接读库;当日主力值由 quant_flow_realtime_today 的 main_net(实时) 补上。
    fetch_fundflow_em 仅在本地库完全无数据时兜底(东财盘中限流期尤其慢)。
    """
    frames = []
    try:
        ff = pd.read_sql(
            "SELECT `date`,`fund_amount`,`fund_amount_small`,`fund_amount_medium`,"
            "`fund_amount_large`,`fund_amount_super`,`fund_rate`,`fund_rate_small`,"
            "`fund_rate_medium`,`fund_rate_large`,`fund_rate_super`,`new_price`,"
            "`change_rate` FROM cn_stock_fund_flow WHERE `code`='%s' AND `date` > "
            "DATE_SUB(CURDATE(), INTERVAL 130 DAY) ORDER BY `date`" % code,
            con=qdb.get_engine())
        if not ff.empty:
            ff.columns = FF_COLS
            ff["date"] = pd.to_datetime(ff["date"].astype(str))
            frames.append(ff)
    except Exception:  # noqa: BLE001
        pass
    if not frames:
        try:  # 库内无数据的极端情况,再走网络(带容错)
            rows = fetch_fundflow_em(code)
            if rows:
                nf = pd.DataFrame(rows, columns=FF_COLS)
                nf["date"] = pd.to_datetime(nf["date"].astype(str))
                frames.append(nf)
        except Exception:  # noqa: BLE001
            pass
    have_today = any(not f.empty and f["date"].max() == pd.Timestamp(today) for f in frames)
    if have_today is False:
        live = pd.read_sql(
            "SELECT main_net, name FROM quant_flow_realtime_today "
            "WHERE `code`='%s'" % code, con=qdb.get_engine())
        if not live.empty and live.iloc[0]["main_net"] is not None:
            # 今日实时主力(分钟级累计,大单口径,与东财历史同源同单位:元)
            today_row = pd.read_sql(
                "SELECT new_price, change_rate FROM cn_stock_spot WHERE `code`='%s' "
                "AND `date`='%s'" % (code, today), con=qdb.get_engine())
            base = {"date": pd.Timestamp(today),
                    "main_net": float(live.iloc[0]["main_net"]) or 0.0}
            for c in FF_COLS:
                base.setdefault(c, None)
            base["close_ff"] = (float(today_row.iloc[0]["new_price"])
                                if not today_row.empty and today_row.iloc[0]["new_price"] is not None else None)
            base["pct_change_ff"] = (float(today_row.iloc[0]["change_rate"])
                                     if not today_row.empty and today_row.iloc[0]["change_rate"] is not None else None)
            frames.append(pd.DataFrame([base]))
    if not frames:
        return pd.DataFrame({"date": pd.to_datetime([]), "main_net": []})
    return pd.concat(frames, ignore_index=True).sort_values("date").reset_index(drop=True)


def analyze(h):
    code, name = h["code"], h["name"]
    tc = ths.to_thscode(code)
    today = dt.date.today().isoformat()
    q = next((i for i in (ths.prices_snapshot([tc]).get("item") or [])
              if i["thscode"] == tc), None)
    if not q:
        return {"name": name, "code": code, "error": "实时行情缺失"}

    df = pd.read_sql(
        f"SELECT `date`,open,close,high,low,volume,amount FROM quant_hist_daily "
        f"WHERE code='{code}' ORDER BY `date` DESC LIMIT 260", con=qdb.get_engine())
    df = df.iloc[::-1].reset_index(drop=True)
    df["date"] = df["date"].astype(str).str[:10]

    # 拼接今日实时bar(若表内尚无当日)
    if df.empty or df.iloc[-1]["date"] != today:
        df = pd.concat([df, pd.DataFrame([{
            "date": today,
            "open": float(q["open_price"]), "close": float(q["last_price"]),
            "high": float(q["high_price"]), "low": float(q["low_price"]),
            "volume": float(q["volume"]), "amount": float(q["turnover"])}])],
            ignore_index=True)

    sig = build_signal_frame(df, _fundflow_df(code, today))
    s = sig.iloc[-1]
    prev_close = float(q["prev_price"])
    pct = (s["close"] / prev_close - 1) * 100
    fade = (float(q["high_price"]) - s["close"]) / float(q["high_price"]) * 100  # 冲高回落幅度

    return {
        "name": name, "code": code, "now": float(s["close"]), "pct": pct,
        "high": float(s["high"]), "low": float(s["low"]), "fade": fade,
        "ma10": float(s["ma10"]), "ma20": float(s["ma20"]), "ma60": float(s["ma60"]),
        "ma_bull": bool(s["ma_bull"]), "above_ma10": bool(s["above_ma10"]),
        "macd_up": bool(s["dif"] > s["dea"]), "rsi14": float(s["rsi14"]),
        "high20_prev": float(s["high20_prev"]), "close_pct_5d": float(s["close_pct_5d"]),
        "main_today": float(s["main_net"]) if s["main_net"] == s["main_net"] else 0.0,
        "sig": [k for k in ("sig_A", "sig_B", "sig_C") if bool(s[k])],
        "vr": float(s["vr"]) if s["vr"] == s["vr"] else 1.0,
    }


def decide(r):
    """规则决策:返回(操作, 理由, 买卖位)。"""
    c, ma10, ma20 = r["now"], r["ma10"], r["ma20"]
    gap10 = (c / ma10 - 1) * 100
    main = r["main_today"] / 1e8
    acts, why, levels = [], [], []

    # ---- 趋势位判断 ----
    if c < ma20:
        gap20 = (c / ma20 - 1) * 100
        if gap20 > -2:  # 贴着20日线,多空分界
            acts.append("多空分界观察")
            why.append(f"距20日线({ma20:.2f})仅 {abs(gap20):.1f}%,收复则转强,收不上再减")
            levels.append(f"转强条件:站上 {ma20:.2f};仍收不回则次日减仓")
        else:
            acts.append("减仓至轻仓")
            why.append(f"收盘在20日线({ma20:.2f})下方 {abs(gap20):.1f}%,趋势破位")
        low_anchor = min(ma20, r["low"])
        levels.append(f"止损位:有效跌破 {low_anchor:.2f} 无条件离场")
        if gap10 < -5:
            acts.append("破位深、反弹即减仓")
            why.append(f"距10日线 {gap10:.1f}%,反弹空间到 {ma10:.2f} 前都是减仓区")
    elif c < ma10:
        if abs(gap10) <= 2:
            acts.append("躺平观察")
            why.append(f"紧贴10日线({ma10:.2f}),多空分界,收不上去再动")
            levels.append(f"转强条件:放量站上 {ma10:.2f};跌破 {ma20:.2f} 则减仓")
        else:
            acts.append("逢反弹减仓")
            why.append(f"10日线下方 {abs(gap10):.1f}%,短线弱势")
    else:
        acts.append("躺平持有" if r["ma_bull"] else "持有观察")
        why.append("趋势位上方" + (",均线多头" if r["ma_bull"] else ",但未多头排列"))

    # ---- 资金配合 ----
    if main < -0.5:
        why.append(f"今日主力净流出 {abs(main):.2f} 亿")
        if "躺平" in acts[-1] and main < -1:
            acts[-1] = "持有但警惕"
    elif main > 0.3:
        why.append(f"今日主力净流入 {main:.2f} 亿")
    if "sig_B" in r["sig"]:
        why.append("触发 M2·B 放量启动信号")
    if "sig_C" in r["sig"]:
        why.append("触发 M2·C 洗盘信号(回调但主力未走)")

    # ---- 冲高回落 / 超买 ----
    if r["fade"] >= 5 and r["pct"] > 0:
        acts.append("冲高回落,可部分止盈/做T")
        why.append(f"盘中冲 {r['high']:.2f} 回落 {r['fade']:.1f}%")
    if r["close_pct_5d"] > 20:
        acts.append("5日涨幅过大,防兑现盘")
        why.append(f"5日累计 +{r['close_pct_5d']:.0f}%")

    # ---- 买卖位 ----
    sup = ma20 if c > ma20 else r["low"]
    levels.append(f"支撑 {sup:.2f}(MA10 {ma10:.2f} / MA20 {ma20:.2f})")
    res = r["high20_prev"] if r["high20_prev"] == r["high20_prev"] else r["high"]
    levels.append(f"压力 {res:.2f}(20日高;今日高点 {r['high']:.2f})")
    if "sig_A" in r["sig"]:
        levels.append("M2·A 吸筹信号:回踩支撑不破可分批加仓")
    if r["rsi14"] >= 75:
        levels.append(f"RSI {r['rsi14']:.0f} 超买,不追高")
    return " / ".join(dict.fromkeys(acts)), ";".join(why), ";".join(levels)


def main():
    import yaml
    with open(os.path.join(ROOT, "config", "config.yaml"), encoding="utf-8") as f:
        holdings = (yaml.safe_load(f) or {}).get("holdings") or []
    now = dt.datetime.now()
    print(f"# 持仓操作决策(实时)| {now:%Y-%m-%d %H:%M}\n")
    # 同花顺当日异动归因(消息面参照,一次批量拉三只)
    anom = {}
    try:
        from quant.datasource import ths as ths_mod
        res = ths_mod.anomaly_analysis_stock(
            [ths_mod.to_thscode(h["code"]) for h in holdings]) or {}
        for it in (res.get("item") or res.get("data") or []):
            code = (it.get("thscode") or "").split(".")[0]
            reason = (it.get("anomaly_reason") or it.get("reason")
                      or it.get("label") or "")
            if code and reason:
                anom[code] = str(reason)[:60]
    except Exception:
        anom = {}
    decision_results = {}
    for h in holdings:
        try:
            r = analyze(h)
        except Exception as e:  # noqa: BLE001
            print(f"## {h['name']}({h['code']}) 分析异常: {e}\n")
            continue
        if "error" in r:
            print(f"## {r['name']}({r['code']}): {r['error']}\n")
            continue
        act, why, levels = decide(r)
        decision_results[str(r["code"]).zfill(6)] = r
        print(f"## {r['name']}({r['code']}) — 建议:**{act}**")
        print(f"- 实时: {r['now']:.2f}({r['pct']:+.2f}%) 区间 {r['low']:.2f}~{r['high']:.2f}"
              f" 量比≈{r['vr']:.1f} RSI{r['rsi14']:.0f}"
              f" | MACD{'红柱' if r['macd_up'] else '绿柱'}"
              f" | 主力今日 {r['main_today']/1e8:+.2f}亿")
        print(f"- 依据: {why}")
        if h.get("cost"):
            try:
                pl = (r["now"] / float(h["cost"]) - 1) * 100
            except (TypeError, ValueError):
                pl = None
            if pl is not None:
                sh_txt = f" × {h.get('shares', '?')} 股" if h.get("shares") else ""
                print(f"- 持仓: 成本 {h['cost']}{sh_txt} → 浮盈 {pl:+.2f}%")
        print(f"- 关键位: {levels}")
        print(f"- M2信号: {'、'.join(r['sig']) if r['sig'] else '无'}")
        # 资金:THS即时(全口径) + 东财主力实时(超大+大单,lmt=0 技巧取盘中累计行)
        try:
            from quant.datasource.fundflow_realtime import instant_of, main_flow_today
            inst = instant_of(r["code"])
            mf = main_flow_today(r["code"])
            parts = []
            if inst:
                parts.append(f"THS即时净额 {inst['net'] / 1e8:+.2f} 亿(全口径,"
                             f"流入{inst['in_yi']:.2f}/流出{inst['out_yi']:.2f},"
                             f"全市场排 {inst['rank']}/{inst['n']})")
            if mf and mf.get("is_today"):
                _t = (mf.get("last_time") or "")[11:16]
                parts.append(f"主力实时(东财·大单口径,至{_t}) "
                             f"{mf['main_net'] / 1e8:+.2f} 亿"
                             f"[超大 {mf['huge_net'] / 1e8:+.2f},大单 {mf['big_net'] / 1e8:+.2f},"
                             f"小单 {mf['small_net'] / 1e8:+.2f}]")
            else:
                parts.append(f"主力(东财,至上一交易日) {r['main_today'] / 1e8:+.2f} 亿")
            print("- 资金: " + " | ".join(parts))
        except Exception:
            pass
        if anom.get(r["code"]):
            print(f"- 当日异动归因(同花顺): {anom[r['code']]}")
        # 板块参照(行业资金流为上一交易日口径,盘中无实时板块流;查不到如实说明)
        try:
            from quant.datasource import sector as _sector
            ind = pd.read_sql(
                f"SELECT industry FROM cn_stock_spot WHERE code='{r['code']}'",
                con=qdb.get_engine())
            ind_name = ind.iloc[0]["industry"] if len(ind) else None
            st = _sector.stock_sector_now(qdb.get_engine(), r["code"], ind_name)
            if st:
                src = st.get("source", "")
                if src == "THS指数实时":
                    print(f"- 板块(THS指数): {st['name']} 今日 {st['pct']:+.2f}%"
                          f"(排 {st['rank']}/{st['n']},{'板块共振' if st['resonant'] else '板块偏弱'})"
                          f" | 个股-板块 {r['pct'] - st['pct']:+.2f}%")
                elif src == "THS实时":
                    print(f"- 板块(THS聚合): {st['industry']} 中位 {st['pct_mid']:+.2f}%"
                          f"(排 {st['rank']}/{st['n']},上涨家 {st['up_ratio'] * 100:.0f}%)"
                          f" | 个股-行业中位 {r['pct'] - st['pct_mid']:+.2f}%")
                else:
                    print(f"- 板块(东财昨日): {st.get('industry', '')} 行业主力 {st['fund_amount_yi']:+.1f} 亿"
                          f"(排 {st['rank']}/{st['n']}),行业涨幅 {st['change_rate']:+.2f}%"
                          f" | 个股 - 行业: {r['pct'] - st['change_rate']:+.2f}% | 口径:上一交易日")
            else:
                print("- 板块: 板块状态未匹配(不硬凑)")
        except Exception:
            pass
        # 消息面(东财个股新闻近2条;利好/利空为关键词粗标记,以原文为准)
        try:
            from quant.datasource import news as news_mod
            items = news_mod.stock_news(r["code"], limit=2)
            if items:
                print("- 消息面:")
                for it in items:
                    tag = {"利好": "🔴", "利空": "🟢", "中性": "⚪"}.get(it["tag"], "⚪")
                    print(f"  - [{it['time'][5:16]}|{it['source'][:12]}] {tag} {it['title'][:46]}")
        except Exception:
            pass
        print()
    # 交易纪律：统一用已展示决策中的当日价格估值，并按现金 + 持仓市值计算组合仓位。
    try:
        import quant.rules as rules
        from quant.portfolio import calculate_portfolio

        cfg_all = yaml.safe_load(open(os.path.join(ROOT, "config", "config.yaml"),
                                      encoding="utf-8")) or {}
        portfolio_cfg = cfg_all.get("portfolio") or {}
        prices = {str(code).zfill(6): result["now"]
                  for code, result in decision_results.items()
                  if result.get("now") is not None}
        valuation = calculate_portfolio(
            holdings, prices, portfolio_cfg.get("cash_balance"),
            legacy_total_assets=portfolio_cfg.get("total_assets"),
            cash_estimated=bool(portfolio_cfg.get("cash_balance_estimated")))
        total_assets = valuation["total_assets"]
        f_map = {}
        mstate = "中性"
        for holding in holdings:
            code = str(holding.get("code") or "").zfill(6)
            result = decision_results.get(code)
            if not result:
                continue
            f_map[code] = (holding.get("name", code), rules.assess_holding(
                holding, result["now"], mstate, result["pct"], result.get("ma20")))
        portfolio_findings, _ = rules.assess_portfolio(holdings, prices, total_assets, mstate)
        if portfolio_findings:
            f_map["__port"] = ("组合", portfolio_findings)
        rules.record_findings(f_map)
        if total_assets is None:
            missing = "、".join(valuation["missing_codes"])
            detail = f"缺少持仓行情/股数：{missing}" if missing else "请校准现金余额"
            print(f"# 组合仓位无法核验：资产估值数据不完整（{detail}）")
        pending = rules.pending_discipline(qdb.get_engine())
        if not pending.empty:
            print("\n# 交易纪律(未执行项,严格遵守)")
            for _, item in pending.iterrows():
                print(f"- [{'DISCIPLINE' if item['level']=='DISCIPLINE' else 'WARN '}] "
                      f"{item['name'] or item['code']} · {item['rule']}: {item['message']}")
            print("  (已执行请到 看板→持仓管理 标记,否则每轮重复提醒)")
    except Exception as e:  # noqa: BLE001
        print(f"# 纪律评估异常: {e}")
    print("> 规则化信号提示,不构成投资建议;数据=实时行情+当日主力资金。")


if __name__ == "__main__":
    main()
