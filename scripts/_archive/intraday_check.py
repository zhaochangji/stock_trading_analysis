# -*- coding: utf-8 -*-
"""
盘中检查(M4 雏形):持仓实时行情 + 异动归因 + 市场情绪
用法: python scripts/intraday_check.py [--no-save]
输出: stdout + reports/intraday_YYYYMMDD_HHMM.md
数据源: 同花顺扶摇 API(实时快照/涨停跌停炸板池/热股/异动归因)
"""
import os
import sys
import datetime as dt
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import yaml  # noqa: E402

from quant.datasource import ths  # noqa: E402


def load_holdings():
    with open(os.path.join(ROOT, "config", "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("holdings") or []


def sec_holdings(lines, holdings):
    """持仓实时行情表。"""
    thscodes = [ths.to_thscode(h["code"]) for h in holdings]
    r = ths.prices_snapshot(thscodes)
    items = {i["thscode"]: i for i in (r.get("item") or [])}
    lines.append("")
    lines.append("## 一、持仓实时")
    lines.append("| 股票 | 现价 | 涨跌幅 | 今开 | 最高 | 最低 | 成交额(亿) | 成交量(万手) |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for h in holdings:
        i = items.get(ths.to_thscode(h["code"]))
        if not i:
            lines.append(f"| {h['name']}({h['code']}) | 数据缺失 | | | | | | |")
            continue
        lines.append(
            f"| {h['name']}({h['code']}) | {i['last_price']:.2f} | {i['price_change_ratio_pct']:+.2f}% "
            f"| {i['open_price']:.2f} | {i['high_price']:.2f} | {i['low_price']:.2f} "
            f"| {i['turnover'] / 1e8:.2f} | {i['volume'] / 1e5:.1f} |"
        )
    return items


def sec_anomaly(lines, holdings, quote_items):
    """持仓异动归因(同花顺 AI 归因,仅异动股票返回)。"""
    thscodes = [ths.to_thscode(h["code"]) for h in holdings]
    try:
        r = ths.anomaly_analysis_stock(thscodes)
    except Exception as e:  # noqa: BLE001
        lines.append("")
        lines.append(f"## 二、持仓异动归因:接口异常({e})")
        return
    anomalies = {a.get("thscode"): a for a in (r.get("item") or []) if a.get("thscode")}
    lines.append("")
    lines.append("## 二、持仓异动归因(同花顺)")
    if not anomalies:
        lines.append("- 今日持仓均无显著异动归因。")
        return
    for h in holdings:
        a = anomalies.get(ths.to_thscode(h["code"]))
        if not a:
            continue
        kws = "、".join(a.get("keyword_list") or [])
        lines.append(f"- **{h['name']}**〔{a.get('tag_name', '异动')}〕关键词: {kws}")
        content = (a.get("analysis_content") or "").strip().splitlines()
        for ln in content[:6]:
            if ln.strip():
                lines.append(f"  - {ln.strip()}")


def sec_market(lines):
    """市场情绪:涨停/跌停/炸板/热股。"""
    lines.append("")
    lines.append("## 三、市场情绪")

    up = ths.limit_up_pool()
    ups = up.get("item") or []
    up_total = (up.get("pagination") or {}).get("total", len(ups))
    if ups:
        top = max(ups, key=lambda x: x.get("continue_day_cnt") or 0)
        lines.append(
            f"- 涨停 {up_total} 家;最高连板 **{top['name']}({top['thscode']}) {top.get('continue_day_text','')}**"
            f",封单 {((top.get('seal_money') or 0) / 1e8):.2f} 亿"
        )
        # 连板≥2 的高度梯队
        ladder = sorted(
            [u for u in ups if (u.get("continue_day_cnt") or 1) >= 2],
            key=lambda x: -(x.get("continue_day_cnt") or 0),
        )[:5]
        if ladder:
            names = "、".join(
                f"{u['name']}({u.get('continue_day_text', '')})" for u in ladder
            )
            lines.append(f"- 连板梯队(前50只内): {names}")

    down = ths.limit_down_pool()
    downs = down.get("item") or []
    down_total = (down.get("pagination") or {}).get("total", len(downs))
    lines.append(f"- 跌停 {down_total} 家")

    brk = ths.limit_break_pool()
    brks = brk.get("item") or []
    brk_total = (brk.get("pagination") or {}).get("total", len(brks))
    lines.append(f"- 炸板 {brk_total} 家")

    try:
        hot = ths.hot_stock_list("1h")
        hots = (hot.get("item") or [])[:5]
        if hots:
            lines.append("- 1小时热股 Top5: " + "、".join(f"{h['name']}(第{h['rank']})" for h in hots))
    except Exception:  # noqa: BLE001
        pass

    # 情绪结论(简单规则,后续 M3 用更细的模型)
    if up_total and down_total:
        mood = "偏暖" if up_total - down_total >= 30 else ("偏冷" if down_total >= up_total else "中性")
        lines.append(f"- 情绪初判: 涨停{up_total} vs 跌停{down_total} → **{mood}**")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-save", action="store_true", help="只打印不落盘")
    args = ap.parse_args()

    now = dt.datetime.now()
    holdings = load_holdings()
    lines = [f"# 盘中检查 {now:%Y-%m-%d %H:%M}", "> 数据: 同花顺实时接口;金额单位亿元,量单位万手"]

    quote_items = sec_holdings(lines, holdings)
    sec_anomaly(lines, holdings, quote_items)
    try:
        sec_market(lines)
    except Exception as e:  # noqa: BLE001
        lines.append(f"- 市场情绪接口异常: {e}")

    text = "\n".join(lines) + "\n"
    print(text)
    if not args.no_save:
        out = os.path.join(ROOT, "reports", f"intraday_{now:%Y%m%d_%H%M}.md")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[saved] {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
