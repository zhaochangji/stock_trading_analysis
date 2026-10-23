# -*- coding: utf-8 -*-
"""战法选股:聚合 M3 多共振 / 龙头战法 / 板块优质三路信号,按可信度排序。

可信度(0-100,透明可复核):
  单战法基础分 + 多战法叠加(每多一路 +12) + 情绪/周期环境修正。
    - M3 基础分 = score10*4 + 级别(S+10/A+5) + 板块共振 +5,封顶 60;
    - 龙头基础分 = 低位(1-2板)38 / 中位(3-4板)28;≥5板为高标只看不追,
      不进可买列表;梯队≥3家 +4、封单≥2亿 +3;
    - 板块优质基础分 = 优质分*7 + 周期主升/底部启动 +3,封顶 55。
  环境修正:情绪活跃(>=60)+5 / 谨慎(30-45)-3 / 冰点(<30)-10;大盘
  趋势上行+5 / 修复上行+3 / 高位过热-5 / 退潮下行-8;个股周期高位过热
  -15 / 下行退潮-10 / 趋势主升+4 / 底部启动+2。
分档:>=75 高可信 / >=50 中高 / <50 谨慎参考。

模拟盘历史胜率样本 < MIN_PAPER_SAMPLE 条时不参与加权(如实标注)。
IO(数据库/行情/配置)由调用方完成后传入,本模块只做规则,便于单测。
"""

from __future__ import annotations

import pandas as pd

# 主板前缀:用户无创业板(30)/科创板(68)交易权限。
MAINBOARD = ("60", "00")

# 模拟盘胜率参与加权的最小样本量。
MIN_PAPER_SAMPLE = 20


def _m3_base(r):
    """M3 多共振基础分 + 一句摘要。"""
    score = float(r.get("score10") or 0)
    base = score * 4
    grade = str(r.get("grade") or "")
    base += 10 if grade == "S" else 5 if grade == "A" else 0
    evidence = str(r.get("evidence") or "")
    if "板块共振" in evidence:
        base += 5
    return min(base, 60.0), f"M3共振 {score}分/{grade or '—'}级"


def _leader_base(item, ladder_count):
    """龙头战法基础分(高标 >=5 板返回 0,只看不追)。"""
    boards = int(item.get("boards") or 1)
    if boards >= 5:
        return 0.0, f"高标{boards}板·只看不追"
    base = 38.0 if boards <= 2 else 28.0
    if (ladder_count or 0) >= 3:
        base += 4
    if float(item.get("seal_yi") or 0) >= 2:
        base += 3
    pos = "低位" if boards <= 2 else "中位"
    return base, f"龙头战法 {boards}板·{pos}"


def _sector_base(row):
    """板块优质基础分(quality 满分 6)。"""
    quality = float(row.get("quality") or 0)
    base = quality * 7
    if row.get("cycle_label") in ("趋势主升", "底部启动"):
        base += 3
    return min(base, 55.0), f"板块优质 {quality}/6"


def _env_adjust(sentiment, cycle):
    """情绪与大盘周期对可信度的环境修正;返回 (分值, 说明)。"""
    adj, notes = 0.0, []
    score = sentiment.get("score")
    if score is not None:
        if score >= 60:
            adj += 5
            notes.append(f"情绪活跃({score}/100)+5")
        elif score < 30:
            adj -= 10
            notes.append(f"情绪冰点({score}/100)-10")
        elif score < 45:
            adj -= 3
            notes.append(f"情绪谨慎({score}/100)-3")
    label = (cycle or {}).get("label")
    cyc = {"趋势上行": 5, "修复上行": 3, "高位过热": -5, "退潮下行": -8}
    if label in cyc:
        adj += cyc[label]
        notes.append(f"大盘{label}{cyc[label]:+d}")
    elif cycle is None:
        notes.append("大盘周期不可用±0")
    return adj, "；".join(notes) if notes else "环境无修正"


def _stock_cycle_adjust(cycle_label):
    return {"趋势主升": 4, "底部启动": 2, "高位过热": -15,
            "下行退潮": -10}.get(cycle_label or "", 0)


def confidence_rows(m3_df, leader, sector_rows, sentiment, cycle, held_codes,
                    limit=12, announces=None):
    """聚合三路战法信号 → 可信度排序的选股列表。

    m3_df      —— M3 scan 缓存 DataFrame(code/name/grade/score10/evidence/close);
    leader     —— leader_scan() 输出(ladder/theme/count);
    sector_rows—— 板块优质 sector_pick() 输出 rows(含 quality/cycle_label/价格计划);
    sentiment/cycle —— market_sentiment()/market_cycle() 输出;
    held_codes —— 已持仓集合(不推荐);
    announces  —— 巨潮公告核验结果 {code: {status, summary, hits, n}};
                 blocked(停牌/退市/立案等)剔除不进榜,warning(减持/问询等)
                 每条 -8、封顶 -16,status=pending 仅标注等下次刷新。
    返回 (rows, env_note);rows 按可信度降序取前 limit。
    """
    held = {str(c).zfill(6) for c in (held_codes or set())}
    agg = {}

    def touch(code, name):
        return agg.setdefault(code, {
            "code": code, "name": str(name or code), "methods": [], "bases": [],
            "evids": [], "cycle_label": None, "tiebreak": 0.0})

    if m3_df is not None and len(m3_df):
        for _, r in m3_df.head(40).iterrows():
            code = str(r.get("code") or "").zfill(6)
            if not code.startswith(MAINBOARD) or code in held:
                continue
            base, detail = _m3_base(r)
            item = touch(code, r.get("name"))
            item["methods"].append("M3共振")
            item["bases"].append(base)
            item["evids"].append(detail + "·" + str(r.get("evidence") or "")[:40])
            item.setdefault("price", r.get("close"))
            item.setdefault("price_from", "最近收盘")
            item.setdefault("score10", float(r.get("score10") or 0))
            item["tiebreak"] = max(item["tiebreak"], base)

    ladder = (leader or {}).get("ladder") or []
    ladder_count = len(ladder)  # 梯队规模(>=2 板家数),反映连板群体氛围
    for it in ladder:
        code = str(it.get("code") or "").zfill(6)
        if not code.startswith(MAINBOARD) or code in held:
            continue
        if int(it.get("boards") or 1) >= 5:
            continue  # 高标只看不追,ladder 提示区另行展示
        base, detail = _leader_base(it, ladder_count)
        item = touch(code, it.get("name"))
        item["methods"].append("龙头战法")
        item["bases"].append(base)
        item["evids"].append(f"{detail}·{it.get('state')}·封单{it.get('seal_yi')}亿"
                            f"·首封{it.get('limit_time')}")
        item["price"] = "—"
        item["price_from"] = "涨停(T+1 关注)"
        item["pct"] = "+9.90"          # 涨停幅,不带%与前端口径一致
        item.setdefault("score10", 0.0)
        item["tiebreak"] = max(item["tiebreak"],
                               int(it.get("boards") or 1) * 10
                               + float(it.get("seal_yi") or 0))

    for row in sector_rows or []:
        code = str(row.get("code") or "").zfill(6)
        if code in held:
            continue
        base, detail = _sector_base(row)
        item = touch(code, row.get("name"))
        item["methods"].append("板块优质")
        item["bases"].append(base)
        item["evids"].append(detail + "·" + str(row.get("evidence") or "")[:40])
        item["cycle_label"] = row.get("cycle_label")
        for key in ("price", "pct", "setup", "plan_low", "plan_high",
                    "stop", "tp1", "tp2"):
            if row.get(key) is not None:
                item[key] = row.get(key)
        item.setdefault("price_from", "当日实时/喂表")
        item["tiebreak"] = max(item["tiebreak"], base)

    env, env_note = _env_adjust(sentiment or {}, cycle)
    # 巨潮公告原文核验:blocked 剔除、warning 降分、pending 标注(不降分)。
    ann = {str(c).zfill(6): a for c, a in (announces or {}).items()}
    ann_blocked = sum(1 for a in ann.values() if a.get("status") == "blocked")
    if ann_blocked:
        env_note += f"；公告核验剔除 {ann_blocked} 只(停牌/退市/立案等高风险)"
    rows = []
    for code, item in agg.items():
        audit = ann.get(code) or {}
        if audit.get("status") == "blocked":
            continue
        n = len(item["methods"])
        base = max(item["bases"]) + (n - 1) * 12
        cyc_adj = _stock_cycle_adjust(item.get("cycle_label"))
        warn_hits = [h for h in (audit.get("hits") or [])
                     if h.get("level") == "warning"]
        ann_adj = -min(len(warn_hits) * 8, 16) if warn_hits else 0
        confidence = max(0, min(100, round(base + env + cyc_adj + ann_adj)))
        rows.append({
            "code": code,
            "name": item["name"],
            "confidence": confidence,
            "tag": "高可信" if confidence >= 75 else "中高" if confidence >= 50 else "谨慎参考",
            "methods": "、".join(item["methods"]),
            "multi": n >= 2,
            "score10": item.get("score10") or 0,
            "price": item.get("price") or "—",
            "price_from": item.get("price_from") or "—",
            "pct": item.get("pct") or "—",
            "setup": item.get("setup") or "—",
            "plan_low": item.get("plan_low"),
            "plan_high": item.get("plan_high"),
            "stop": item.get("stop"),
            "tp1": item.get("tp1"),
            "tp2": item.get("tp2"),
            "cycle_label": item.get("cycle_label") or "—",
            "evidence": "；".join(item["evids"]),
            "tiebreak": item["tiebreak"],
            "announce": {
                "status": audit.get("status") or "unknown",
                "summary": audit.get("summary") or "未核验",
                "n": audit.get("n") or 0,
                "url": (audit.get("hits") or [{}])[0].get("url") or "",
            },
        })
    rows.sort(key=lambda r: (-r["confidence"], -float(r["tiebreak"]),
                             -float(r["score10"])))
    return rows[:limit], env_note


def ladder_view(leader, announces=None, limit=10):
    """连板梯队提示区(全部展示:主板可关注,非主板标注权限外,高标只看不追)。

    announces 为巨潮核验结果 {code: {status, summary}}(可缺省);
    停牌/立案等 blocked 标注"公告高风险",主板正常标的展示核验摘要。
    """
    ann = {str(c).zfill(6): a for c, a in (announces or {}).items()}
    out = []
    for it in ((leader or {}).get("ladder") or [])[:limit]:
        code = str(it.get("code") or "").zfill(6)
        boards = int(it.get("boards") or 1)
        watch_only = boards >= 5 or not code.startswith(MAINBOARD)
        audit = ann.get(code) or {}
        status = audit.get("status") or "unknown"
        out.append({
            "code": code, "name": it.get("name") or "", "boards": boards,
            "state": it.get("state") or "", "seal_yi": it.get("seal_yi"),
            "limit_time": it.get("limit_time") or "",
            "watch_only": watch_only,
            "announce": {
                "status": status,
                "summary": audit.get("summary") or "未核验",
                "url": (audit.get("hits") or [{}])[0].get("url") or "",
            },
            "note": (f"高标{boards}板·只看不追等首阴反包" if boards >= 5 else
                     "非主板·交易权限外" if not code.startswith(MAINBOARD) else
                     "低位可关注弱转强/分歧回封" if boards <= 2 else
                     "中位只做分歧转一致"),
        })
    return out


def paper_win_note(paper_df):
    """模拟盘战法胜率:样本不足时不参与加权,如实返回说明。"""
    if paper_df is None or len(paper_df) == 0:
        return "模拟盘无记录,历史胜率暂不参与可信度加权。"
    done = paper_df[paper_df["win_t1"].notna()] if "win_t1" in paper_df else pd.DataFrame()
    if len(done) < MIN_PAPER_SAMPLE:
        return (f"模拟盘已完成 {len(done)} 条(<{MIN_PAPER_SAMPLE}),样本不足,"
                "历史胜率暂不参与可信度加权。")
    rate = float(done["win_t1"].mean() * 100)
    return f"模拟盘 T+1 胜率 {rate:.0f}%(样本 {len(done)} 条)。"


__all__ = ["confidence_rows", "ladder_view", "paper_win_note",
           "MAINBOARD", "MIN_PAPER_SAMPLE"]
