# -*- coding: utf-8 -*-
"""竞价选股推送(09:20 时段,四时段之一)。

逻辑 v1:
  1. 用昨日收盘数据跑 M3 多共振扫描(cn_stock_spot/fund_flow 表存的是上一交易日终值,
     K线含上一交易日) → S/A 级候选
  2. 候选拉实时竞价快照,规则:主板(60/00)、高开 0~7%(温和,非一字)、竞价量比≥1.2、
     竞价未匹配占比不过高
  3. 按(共振分+竞价量比)排序,推 1 只主板 + 2 只备选;无合格者如实说明
  4. 附:三持仓竞价表现 + 大盘竞价风向标

用法: python scripts/auction_push.py   (9:20-9:30 之间运行)
"""

import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "instock"))  # 交易日历(未直接用,留作扩展)

from quant.datasource import ths  # noqa: E402
from quant.push import send_notice  # noqa: E402
from quant.selection.engine import scan  # noqa: E402

import yaml  # noqa: E402


def _holdings():
    with open(os.path.join(ROOT, "config", "config.yaml"), encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("holdings") or []


def main():
    now = dt.datetime.now()
    stage = "live" if (now.hour, now.minute) < (9, 26) else "final"
    lines = [f"**竞价报告** {now:%m-%d %H:%M}", ""]

    # ---- 持仓竞价 ----
    holds = _holdings()
    hcodes = [ths.to_thscode(h["code"]) for h in holds]
    try:
        a = ths.auction_snapshot(hcodes, stage=stage) or {}
        items = {i.get("thscode"): i for i in (a.get("item") or a.get("data") or [])}
        lines.append("**① 持仓竞价**")
        for h in holds:
            i = items.get(ths.to_thscode(h["code"]))
            if not i or "auction_price" not in i:
                lines.append(f"- {h['name']}: 竞价数据缺失")
                continue
            lines.append(
                f"- {h['name']} 竞价 {i['auction_price']:.2f}({i['auction_pct']:+.1f}%)"
                f" 竞价量比 {i.get('auction_volume_ratio', 0):.1f}"
                f" 竞价额 {i.get('auction_amount', 0) / 1e8:.2f}亿")
        lines.append("")
    except Exception as e:  # noqa: BLE001
        lines.append(f"持仓竞价异常: {e}")

    # ---- M3 候选 + 竞价确认 → 选 1 只主板 ----
    pick, backup, why_none = None, [], ""
    try:
        top = scan(top_n=40)
        if top.empty:
            why_none = "昨日全市场无共振标的(不硬凑)"
        else:
            mb = top[top["code"].str.match(r"^(60|00)")].head(20)  # 主板池
            if mb.empty:
                why_none = "共振标的均非主板"
            else:
                codes = [ths.to_thscode(c) for c in mb["code"].tolist()]
                a2 = ths.auction_snapshot(codes, stage=stage) or {}
                aitems = {i.get("thscode"): i for i in (a2.get("item") or a2.get("data") or [])}
                cands = []
                for _, r in mb.iterrows():
                    i = aitems.get(ths.to_thscode(r["code"]))
                    if not i or "auction_price" not in i:
                        continue
                    pct = i.get("auction_pct")
                    avr = i.get("auction_volume_ratio") or 0
                    from quant.selection.confirm import confirm_auction
                    ok_c, why_c = confirm_auction(pct, avr)
                    if not ok_c:
                        continue
                    cands.append((float(r["score"]) + avr, r, i, why_c))
                if not cands:
                    why_none = "S/A 级候选竞价未确认(高开过猛/竞价缩量/低开)"
                else:
                    cands.sort(key=lambda x: -x[0])
                    _, r, i, why_c = cands[0]
                    pick = r
                    pick_auction = i
                    pick_confirm = why_c
                    backup = [c[1] for c in cands[1:3]]
                    # 模拟盘记录(竞价时点价)
                    try:
                        from quant import paper
                        paper.record("auction", r["code"], r["name"], r["horizon"],
                                     r.get("grade", ""), r.get("score10"),
                                     r.get("evidence", ""), float(i["auction_price"]),
                                     f"竞价{float(i['auction_pct']):+.1f}%/量比{i.get('auction_volume_ratio', 0):.1f}")
                        for b in backup[:2]:
                            paper.record("auction_back", b["code"], b["name"], b["horizon"],
                                         b.get("grade", ""), b.get("score10"),
                                         b.get("evidence", ""), None, "备选不买入")
                    except Exception as ep:  # noqa: BLE001
                        print(f"[paper] 记录失败: {ep}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        why_none = f"扫描异常: {e}"

    lines.append("**② 今日竞价选股(主板)**")
    if pick is not None:
        lines.append(
            f"- ★ **{pick['name']}({pick['code']})** | {pick['horizon']} | "
            f"推荐分 **{pick['score10']}/10**(M3共振 {int(pick['score'])}/8)"
            f"| 竞价 {pick_auction['auction_price']:.2f}"
            f"({pick_auction['auction_pct']:+.1f}%) 量比{pick_auction['auction_volume_ratio']:.1f}"
            f" | 行业 {pick['industry']}")
        lines.append(f"  选定依据: {pick['evidence']}")
        lines.append(f"  竞价确认: {pick_confirm}")
        if backup:
            b = "、".join(f"{x['name']}({x['code']})" for _, x in [(0, c) for c in backup])
            lines.append(f"- 备选: {b}")
        lines.append("- 竞价规则:温和高开+竞价放量确认,避开一字/低开;级别详见晚间复盘")
        # 入选股消息面(关键词粗标记,结论以原文为准)
        try:
            from quant.datasource import news as news_mod
            items = news_mod.stock_news(pick["code"], limit=3)
            if items:
                lines.append("- 入选股消息面:")
                for it in items:
                    emoji = {"利好": "🔴利好", "利空": "🟢利空", "中性": "⚪"}.get(it["tag"], "⚪")
                    lines.append(f"  - [{it['time'][5:16]}|{it['source'][:12]}] {emoji} {it['title']}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"- 入选股消息面: 获取失败({type(e).__name__})")
    else:
        lines.append(f"- 今日无合格标的。原因:{why_none}")
    lines.append("")

    # ---- 大盘竞价风向 ----
    try:
        b = ths.short_term_benchmark() or {}
        items = (b.get("item") or [])[:5]
        if items:
            tags = {}
            for it in items:
                for t in (it.get("tags") or []):
                    tags[t] = tags.get(t, 0) + 1
            top_tags = sorted(tags.items(), key=lambda x: -x[1])[:4]
            lines.append("**③ 大盘竞价风向**")
            lines.append("- 竞价风向标热门题材: " + "、".join(t for t, _ in top_tags))
    except Exception:  # noqa: BLE001
        pass

    lines.append("")
    lines.append("> 规则化信号,不构成投资建议;完整扫描见晚间复盘。")
    text = "\n".join(lines)
    print(text)
    ok = send_notice(f"竞价报告 {now:%m-%d}", text)
    print("push:", "OK" if ok else "FAIL", file=sys.stderr)


if __name__ == "__main__":
    main()
