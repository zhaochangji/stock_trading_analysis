# -*- coding: utf-8 -*-
"""尾盘选股推送(14:35 时段,四时段之一)——补齐"主板≥3只/天"的尾盘 1 只。

逻辑 v1(讲证据):
  1. 以昨日收盘全量数据跑 M3 扫描 → S/A 级候选
  2. 主板池(60/00)取 TopN 候选后,拉 THS 实时快照尾盘确认:
     * 当日涨幅 0~7%(未一字/未秒板,也未拉高出货行情)
     * 现价 ≥ 昨收(MA10 口径顺势)
     * 实时成交额 ≥ 1 亿(活跃度)
     资金口径披露:M3 的主力资金因子为截至上一交易日(盘中无全市场资金流接口)
  3. 按推荐分排序推 1 只 + 2 备选;无合格者如实说明
  4. 全部入选写入模拟盘记录(quant_paper_trades, slot=tail, rec_price=尾盘现价)

用法: python scripts/tail_pick.py   (14:35-14:55 运行)
"""

import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "instock"))

from quant import paper  # noqa: E402
from quant.datasource import ths  # noqa: E402
from quant.push import send_notice  # noqa: E402
from quant.selection.engine import scan  # noqa: E402


def main():
    now = dt.datetime.now()
    lines = [f"**尾盘选股** {now:%m-%d %H:%M}", ""]

    pick, backup, why_none = None, [], ""
    try:
        top = scan(top_n=40)
        if top.empty:
            why_none = "全市场无 S/A 级共振标的(不硬凑)"
        else:
            mb = top[top["code"].str.match(r"^(60|00)")]
            if mb.empty:
                why_none = "共振标的均非主板"
            else:
                # 实时快照确认(全市场一次拉齐)
                snaps = {}
                codes = [ths.to_thscode(c) for c in mb["code"].tolist()]
                items = []
                for off in (0, 5000):
                    r = ths.prices_snapshot(limit=5000, offset=off) or {}
                    items.extend(r.get("item") or [])
                    if len(r.get("item") or []) < 5000:
                        break
                for it in items:
                    code = (it.get("thscode") or "").split(".")[0]
                    if code:
                        snaps[code] = it
                cands = []
                for _, r in mb.iterrows():
                    s = snaps.get(r["code"])
                    if not s:
                        continue
                    pct = float(s.get("price_change_ratio_pct") or 0)
                    price = float(s.get("last_price") or 0)
                    amt = float(s.get("turnover") or 0)
                    net = None
                    try:
                        from quant.datasource.fundflow_realtime import instant_of
                        inst = instant_of(r["code"])
                        if inst and inst["net"] is not None:
                            net = inst["net"]
                    except Exception:  # noqa: BLE001
                        net = None  # 即时流缺失按软条件(不剔,证据里说明)
                    from quant.selection.confirm import confirm_tail
                    ok_c, why_c = confirm_tail(pct, price, float(r["close"]), amt, net)
                    if not ok_c:
                        continue
                    cands.append((float(r["score10"]) or 0, r, s, why_c))
                if not cands:
                    why_none = "S/A 候选未通过尾盘确认(涨幅出界/破位/不活跃)"
                else:
                    cands.sort(key=lambda x: -x[0])
                    pick, pick_snap, pick_confirm = cands[0][1], cands[0][2], cands[0][3]
                    backup = [c[1] for c in cands[1:3]]
                    try:
                        paper.record("tail", pick["code"], pick["name"],
                                     pick["horizon"], pick["grade"],
                                     pick["score10"], pick["evidence"],
                                     float(pick_snap["last_price"]),
                                     f"尾盘{float(pick_snap['price_change_ratio_pct']):+.1f}%"
                                     f"/额{float(pick_snap['turnover']) / 1e8:.1f}亿")
                    except Exception as ep:  # noqa: BLE001
                        print(f"[paper] 记录失败: {ep}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        why_none = f"扫描异常: {e}"

    lines.append("**① 尾盘选股(主板)**")
    if pick is not None:
        lines.append(
            f"- ★ **{pick['name']}({pick['code']})** | {pick['horizon']} | "
            f"推荐分 **{pick['score10']}/10**(M3共振 {int(pick['score'])}/8)"
            f" | 尾盘现价 {float(pick_snap['last_price']):.2f}"
            f"({float(pick_snap['price_change_ratio_pct']):+.1f}%)"
            f" 实时成交额 {float(pick_snap['turnover']) / 1e8:.1f}亿 | 行业 {pick['industry']}")
        lines.append(f"  选定依据: {pick['evidence']}")
        try:
            from quant.datasource.fundflow_realtime import main_flow_today
            mf = main_flow_today(pick["code"])
            if mf and mf.get("is_today"):
                lines.append(f"  主力实时(东财·大单口径): 今日累计 {mf['main_net'] / 1e8:+.2f} 亿"
                             f"[超大 {mf['huge_net'] / 1e8:+.2f},大单 {mf['big_net'] / 1e8:+.2f}]")
        except Exception:  # noqa: BLE001
            pass
        lines.append(f"  尾盘确认: {pick_confirm}")
        if backup:
            lines.append("- 备选: " + "、".join(f"{x['name']}({x['code']})" for x in backup))
        lines.append("- 资金口径:M3 主力资金因子为东财(经上一交易日);THS即时为全口径净值")
    else:
        lines.append(f"- 今日无尾盘合格标的。原因:{why_none}")

    lines.append("")
    lines.append("> 模拟运行 v0.1,规则化信号,不构成投资建议。")
    text = "\n".join(lines)
    print(text)
    ok = send_notice(f"尾盘选股 {now:%m-%d}", text)
    print("push:", "OK" if ok else "FAIL", file=sys.stderr)


if __name__ == "__main__":
    main()
