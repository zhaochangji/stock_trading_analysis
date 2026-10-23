# -*- coding: utf-8 -*-
"""每日收盘报告 v0.1(模拟运行阶段)。

生成: reports/daily_YYYY-MM-DD.md + stdout
内容: ①大盘环境(上证趋势+市场情绪) ②持仓复盘(行情/技术/资金/M2信号/规则提示)
      ③次日方向 ④备注
前置: quant_hist_daily 已含当日K线(15:05 后由 daily_close.sh 的增量任务写入)。
说明: 本报告为规则化信号提示,不构成投资建议。
"""

import os
import sys
import datetime as dt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd  # noqa: E402
import yaml  # noqa: E402

import quant.db as qdb  # noqa: E402
from quant.capital.signals import build_signal_frame  # noqa: E402
from quant.datasource.fundflow import fetch_fundflow_em  # noqa: E402
from quant.datasource.index import fetch_index_daily  # noqa: E402
from quant.datasource import ths  # noqa: E402

SIG_NAMES = {"sig_A": "A·温和吸筹", "sig_B": "B·放量启动", "sig_C": "C·洗盘识别"}


def load_holdings():
    with open(os.path.join(ROOT, "config", "config.yaml"), encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("holdings") or []


# ---------------------------------------------------------------- 大盘环境

def sec_market(lines):
    lines.append("")
    lines.append("## 一、大盘环境")
    sh = fetch_index_daily("sh000001", start="20260601")
    if len(sh) < 21:
        lines.append("- 上证指数数据不足,跳过趋势判断。")
        index_close = None
    else:
        last = sh[-1]
        closes = [r["close"] for r in sh]
        amts = [r["amount"] for r in sh]
        ma5 = sum(closes[-5:]) / 5
        ma20 = sum(closes[-20:]) / 20
        amt5_prev = sum(amts[-6:-1]) / 5
        vol_state = "放量" if last["amount"] > amt5_prev * 1.1 else ("缩量" if last["amount"] < amt5_prev * 0.9 else "平量")
        pos = "5日线上方" if last["close"] >= ma5 else "5日线下方"
        pos20 = "20日线上方" if last["close"] >= ma20 else "20日线下方"
        trend3 = (last["close"] / closes[-4] - 1) * 100 if len(closes) >= 4 else 0.0
        lines.append(
            f"- 上证指数 {last['date']} 收 **{last['close']:.2f}**({last['pct']:+.2f}%),"
            f"{pos}(MA5={ma5:.0f})、{pos20}(MA20={ma20:.0f}),"
            f"近3日 {trend3:+.2f}%,成交 {last['amount'] / 1e8:.0f} 亿 vs 5日均 {amt5_prev / 1e8:.0f} 亿({vol_state})"
        )
        index_close = last["close"]

    # 市场情绪(THS 涨停/跌停/炸板)
    try:
        up = ths.limit_up_pool()
        ups = up.get("item") or []
        up_total = (up.get("pagination") or {}).get("total", len(ups))
        down = ths.limit_down_pool()
        down_total = (down.get("pagination") or {}).get("total", len((down.get("item") or [])))
        brk = ths.limit_break_pool()
        brk_total = (brk.get("pagination") or {}).get("total", len((brk.get("item") or [])))
        height = "无连板"
        if ups:
            top = max(ups, key=lambda x: x.get("continue_day_cnt") or 0)
            height = f"{top['name']} {top.get('continue_day_text', '')}"
        diff = up_total - down_total
        mood = "偏暖" if diff >= 30 else ("偏冷" if down_total >= up_total else "中性")
        brk_rate = (brk_total / up_total * 100) if up_total else 0.0
        lines.append(
            f"- 情绪: 涨停 {up_total} / 跌停 {down_total} / 炸板 {brk_total};"
            f"涨停-跌停={diff:+d}(判据:差值≥30 为偏暖)<br/>"
            f"炸板率 {brk_rate:.0f}%(≈炸板/涨停);连板高度 {height} → **{mood}**"
        )
    except Exception as e:  # noqa: BLE001
        lines.append(f"- 情绪接口异常: {e}")
        mood = "未知"
    if index_close is not None:
        return {"close": index_close, "above_ma20": index_close >= ma20,
                "trend3": trend3, "mood": mood, "ma20": ma20,
                "up_total": up_total, "down_total": down_total}
    return {"close": None, "above_ma20": None, "trend3": None, "mood": mood}


# ---------------------------------------------------------------- 持仓复盘

def holding_block(lines, h, engine, today):
    code, name = h["code"], h["name"]
    lines.append("")
    lines.append(f"### {name}({code})")
    df = pd.read_sql(
        f"SELECT date,open,close,high,low,volume,amount,turnover FROM quant_hist_daily "
        f"WHERE code='{code}' ORDER BY date", con=engine)
    if df.empty or len(df) < 60:
        lines.append("- K线数据不足(需≥60日),跳过。")
        return
    last_date = str(df.iloc[-1]["date"])[:10]
    if last_date != today:
        lines.append(f"- ⚠ 当日K线尚未入库,以下为最近交易日 {last_date} 的数据。")

    # 资金流(东财个股资金流,近~120日)
    try:
        ff_rows = fetch_fundflow_em(code)
        ff_df = pd.DataFrame(ff_rows, columns=[
            "date", "main_net", "small_net", "mid_net", "big_net", "huge_net",
            "main_pct", "small_pct", "mid_pct", "big_pct", "huge_pct",
            "close_ff", "pct_change_ff"]) if ff_rows else None
        if ff_df is not None:
            ff_df["date"] = pd.to_datetime(ff_df["date"].astype(str))
            ff_df = ff_df.sort_values("date").reset_index(drop=True)
    except Exception:  # noqa: BLE001
        ff_df = None
    if ff_df is None:
        ff_df = pd.DataFrame({"date": pd.to_datetime([]), "main_net": []})

    sig = build_signal_frame(df, ff_df)
    last = sig.iloc[-1]

    pct = (last["close"] / df.iloc[-2]["close"] - 1) * 100
    to = df["turnover"].iloc[-1]
    to_txt = f",换手 {to:.2f}%" if to and to == to else ""
    lines.append(
        f"- 行情: 收 **{last['close']:.2f}**({pct:+.2f}%),成交额 {last['amount'] / 1e8:.2f} 亿,"
        f"成交量 {df['volume'].iloc[-1] / 1e5:.1f} 万手{to_txt}"
    )
    tech = []
    if bool(last["ma_bull"]):
        tech.append("均线多头")
    elif bool(last["above_ma10"]):
        tech.append("10日线上方(未多头)")
    else:
        tech.append("**跌破10日线**")
    if bool(last["macd_golden"]):
        tech.append("MACD金叉")
    if bool(last["break_high20"]):
        tech.append("突破20日新高")
    if not pd.isna(last["rsi14"]):
        tech.append(f"RSI14={last['rsi14']:.0f}")
    lines.append(f"- 技术: {'、'.join(tech) if tech else '—'}")
    lines.append(
        f"- 关键位: 支撑 MA10={last['ma10']:.2f} / MA20={last['ma20']:.2f};"
        f"压力 20日高 {last['high20_prev']:.2f}"
    )
    # 消息面(东财个股新闻近3条,保留时间/来源;利好利空为关键词粗标记,结论以原文为准)
    try:
        from quant.datasource import news as news_mod
        items = news_mod.stock_news(code, limit=3)
        if items:
            lines.append("- 消息面:")
            for it in items:
                emoji = {"利好": "🔴利好", "利空": "🟢利空", "中性": "⚪"}.get(it["tag"], "⚪")
                lines.append(f"  - [{it['time'][5:16]}|{it['source'][:12]}] {emoji} {it['title']}")
        else:
            lines.append("- 消息面: 近期无相关新闻")
    except Exception as e:  # noqa: BLE001
        lines.append(f"- 消息面: 获取失败({type(e).__name__}),不影响技术面结论")

    # 资金
    mn = last["main_net"]
    if ff_df.empty or mn == 0:
        lines.append("- 资金: 当日主力数据缺失")
    else:
        direction = "净流入" if mn > 0 else "净流出"
        lines.append(f"- 资金: 当日主力{direction} **{abs(mn) / 1e8:.2f} 亿**")
    # M2 信号
    fired = [SIG_NAMES[k] for k in ("sig_A", "sig_B", "sig_C") if bool(last.get(k, False))]
    lines.append(f"- M2信号: {'、'.join(fired) if fired else '当日无信号'}")

    # 规则提示(v0.1 规则化,每条附判定依据)
    tips = []
    if not bool(last["above_ma10"]):
        gap = (last["close"] / last["ma10"] - 1) * 100
        tips.append(f"短线转弱(依据:收盘 {last['close']:.2f} 低于 MA10 {last['ma10']:.2f},"
                    f"低 {abs(gap):.1f}%),若次日收盘收不回 {last['ma10']:.2f} 上方,减仓警惕")
    elif bool(last["ma_bull"]) and (ff_df.empty or mn == 0 or mn > 0):
        tips.append("趋势健康(依据:收盘站上 MA10 且均线多头)"
                    + (f",当日主力 {mn / 1e8:+.2f} 亿未明显流出" if not ff_df.empty else "")
                    + ",持股观察")
    if pct >= 9.0:
        tips.append(f"今日大涨(依据:{pct:+.1f}%,偏离5日均值过大),注意次日高开承接,冲高乏力可部分止盈")
    if not pd.isna(last["rsi14"]) and last["rsi14"] >= 75:
        tips.append(f"超买警惕(依据:RSI14={last['rsi14']:.0f},阈值 75),追高需谨慎")
    lines.append(f"- 规则提示: {' '.join(tips) if tips else '无(未触发任何规则)'}(规则化提示,非投资建议)")


def sec_holdings(lines, holdings, engine, today):
    lines.append("")
    lines.append("## 二、持仓复盘")
    for h in holdings:
        try:
            holding_block(lines, h, engine, today)
        except Exception as e:  # noqa: BLE001
            lines.append("")
            lines.append(f"### {h['name']}({h['code']})")
            lines.append(f"- 复盘异常: {e}")


# ---------------------------------------------------------------- 次日方向

def sec_tomorrow(lines, holdings, engine, today, market):
    lines.append("")
    lines.append("## 三、次日方向")
    # 大盘倾向(规则化):趋势位 × 情绪
    m = market or {}
    if m.get("close") is None:
        lines.append("- 大盘: 指数数据不足,暂不给出倾向。")
    else:
        diff = m.get("up_total", 0) - m.get("down_total", 0)
        if m["above_ma20"] and m["mood"] == "偏暖":
            lean = "**偏多**:回调可视为低吸机会"
        elif m["above_ma20"]:
            lean = "**中性偏多**:趋势位在20日线上方,但情绪一般,等待量能确认"
        elif m["mood"] == "偏暖":
            lean = "**中性**:情绪偏暖但指数仍压在20日线下,反弹需放量站上均线"
        else:
            lean = "**防守**:20日线下方 + 情绪未回暖,控制仓位,等右侧信号"
        lines.append(
            f"- 大盘: {lean}  (依据:收盘 {m['close']:.2f} vs MA20 {m['ma20']:.2f}"
            f"[{(m['close'] / m['ma20'] - 1) * 100:+.1f}%],涨停-跌停 {diff:+d}→{m['mood']}(阈值±30),"
            f"近3日 {m['trend3']:+.2f}%)")
    for h in holdings:
        try:
            df = pd.read_sql(
                f"SELECT date,close,high,low FROM quant_hist_daily WHERE code='{h['code']}' "
                f"ORDER BY date DESC LIMIT 30", con=engine)
            df = df.iloc[::-1]
            if len(df) < 2:
                continue
            c = df["close"].iloc[-1]
            ma10 = df["close"].rolling(10).mean().iloc[-1]
            ma20 = df["close"].rolling(20).mean().iloc[-1]
            prev_high = df["high"].iloc[:-1].max()  # 前高不含当日
            if not pd.isna(ma10):
                stance = "看多倾向(收10日线上方)" if c >= ma10 else "转弱警惕(收10日线下方)"
                lines.append(
                    f"- {h['name']}: {stance};现价 {c:.2f} 距 MA10 {(c / ma10 - 1) * 100:+.1f}%"
                    f" / 距 MA20 {(c / ma20 - 1) * 100:+.1f}%;"
                    f"上方压力看前高 {prev_high:.2f},下方支撑看 MA10 {ma10:.2f} / MA20 {ma20:.2f}"
                )
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 当日全市场扫描(M3)

def sec_m3(lines, engine, today):
    from quant.selection import engine as m3
    lines.append("")
    lines.append("## 四、今日全市场扫描(M3)")
    try:
        dfm, st = m3.scan(top_n=15, return_stats=True)
        lines.append(m3.render(dfm, stats=st))
    except Exception as e:  # noqa: BLE001
        lines.append(f"- M3 扫描异常: {e}(不硬凑,明日重试)")


def sec_telegraph(lines, limit=6):
    """财联社电报快讯(全市场,最新6条)——给持仓与候选的异动提供消息面参照。"""
    from quant.datasource import news as news_mod
    lines.append("")
    lines.append("### 市场快讯(财联社电报)")
    try:
        items = news_mod.telegraph(limit=limit)
        for it in items:
            emoji = {"利好": "🔴利好", "利空": "🟢利空", "中性": "⚪"}.get(it["tag"], "⚪")
            lines.append(f"- [{it['time'][5:]}] {emoji} {it['title'][:60]}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"- 获取失败({type(e).__name__}),不影响技术面结论")


def sec_leader(lines):
    """龙头战法(连板梯队/主线题材/龙头唯一性)——复盘附加段。"""
    from quant.strategies.leader import leader_scan
    lines.append("")
    lines.append("## 龙头战法(今日连板梯队)")
    try:
        r = leader_scan()
        if r and r.get("leader"):
            lead = r["leader"]
            ladder = "、".join(f"{x['name']} {x['boards']}板({x['state'][:2]})"
                               for x in r["ladder"][:8]) or "无连板梯队"
            lines.append(f"- 涨停 {r['stats']['up_cnt']} 家 / 炸板 {r['stats']['break_cnt']} 家;"
                         f"主线题材 **{r['theme']}**(最高 {r['height']} 板,{r['count']} 家)")
            lines.append(f"- 龙头:**{lead['name']}({lead['code']})** {lead['boards']} 连板 · "
                         f"{lead['state']} · 封单 {lead['seal_yi']} 亿 · 首板 {lead['limit_time']}")
            lines.append(f"- 梯队:{ladder}")
            for hint in r["hints"]:
                lines.append(f"- {hint}")
        elif r:
            lines.append(f"- 今日涨停 {r['stats']['up_cnt']} 家,暂无连板龙头(不硬凑)")
    except Exception as e:  # noqa: BLE001
        lines.append(f"- 龙头战法数据异常({type(e).__name__}),明日重试")


def _tail(lines):
    lines.append("")
    lines.append("## 五、备注")
    lines.append("- 本报告为模拟运行 v0.1,所有提示均为规则化信号,不构成投资建议;"
                 "所有结论行均附判定依据与阈值。")


def main():
    now = dt.datetime.now()
    today = now.date().isoformat()
    holdings = load_holdings()
    engine = qdb.get_engine()

    lines = [f"# 每日收盘报告 {today}",
             "> 生成时间 " + now.strftime("%Y-%m-%d %H:%M") + " | 模拟运行 v0.1"]
    market = sec_market(lines)
    sec_holdings(lines, holdings, engine, today)
    sec_tomorrow(lines, holdings, engine, today, market)
    sec_m3(lines, engine, today)
    sec_leader(lines)
    sec_telegraph(lines)
    _tail(lines)

    text = "\n".join(lines) + "\n"
    print(text)
    out = os.path.join(ROOT, "reports", f"daily_{today}.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[saved] {out}", file=sys.stderr)

    # ---- 推送:发送与本地留档相同的完整版 markdown;长文由通道分段 ----
    try:
        from quant.push import send_notice
        send_notice(f"收盘复盘 {today}", text, require_full=True)
    except Exception as e:  # noqa: BLE001
        print(f"[push] 日报推送失败: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
