# -*- coding: utf-8 -*-
"""当前时段实时决策(可买候选 / 持仓卖出信号)的纯逻辑层。

供看板 /recommend/live 按钮调用:
  current_slot           —— 按时钟划分竞价/盘中/尾盘/已收盘;
  confirm_buy_candidates —— M3 候选 + 当前时段行情确认(竞价规则或实时规则);
  assess_buy_candidates  —— 在时段确认之上逐只做本地技术评估,只留
                            「核心条件全过」或「通过大部分」的候选,
                            并给出建议买入区/止损/止盈;
  collect_sell_signals   —— 逐持仓跑实时决策,归类出含卖出动作的持仓。

IO(行情快照、数据库、配置)全部由调用方完成后传入,本模块只做规则,便于单测。
口径与 scripts/auction_push.py、scripts/tail_pick.py、scripts/holding_decision.py
共用 quant.selection.confirm 与 holding_decision.decide;入场/止损公式与
quant.analysis.assess_entry 完全一致(同步修改须两处同改),不在单股请求里
发起全市场资金抓取(AGENTS.md 约束)。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

# 主板前缀:用户无创业板(30)/科创板(68)交易权限,候选只保留 60/00 开头。
_MAINBOARD = ("60", "00")

# holding_decision.decide 输出的动作中,属于"需要卖出"的关键词。
SELL_KEYWORDS = ("减仓", "清仓", "止盈", "警惕")

# 核心条件 4 项制:M3 共振 + 时段确认(第一层保证)+ 技术趋势 + 买入位置。
CORE_TOTAL = 4
# 只保留「通过大部分」(>=3/4) 的候选,更低的如实不展示。
CORE_MIN = 3


def current_slot(now=None):
    """按时钟划分当前时段;返回 slot/label/mode/note。"""
    now = now or dt.datetime.now()
    t = now.time()
    if dt.time(9, 15) <= t < dt.time(9, 30):
        return {"slot": "auction", "label": "竞价", "mode": "auction",
                "note": "竞价时段：以竞价价与竞价量比做温和高开确认。"}
    if dt.time(9, 30) <= t < dt.time(11, 30):
        return {"slot": "intraday", "label": "盘中", "mode": "realtime",
                "note": "盘中时段：以实时快照做涨幅/顺势/活跃三项确认。"}
    if dt.time(11, 30) <= t < dt.time(13, 0):
        return {"slot": "intraday", "label": "午间休市", "mode": "realtime",
                "note": "午间休市：行情为上午收盘值，确认结果仅供午后参考。"}
    if dt.time(13, 0) <= t < dt.time(14, 35):
        return {"slot": "intraday", "label": "盘中", "mode": "realtime",
                "note": "盘中时段：以实时快照做涨幅/顺势/活跃三项确认。"}
    if dt.time(14, 35) <= t < dt.time(15, 0):
        return {"slot": "tail", "label": "尾盘", "mode": "realtime",
                "note": "尾盘时段：实时确认与 14:35 尾盘推送同规则。"}
    return closed_slot("已收盘/未开盘：按最近快照复核，仅供下一交易时段参考。")


def closed_slot(note):
    """非交易时段(收盘后/未开盘/非交易日)统一口径。"""
    return {"slot": "closed", "label": "已收盘", "mode": "realtime", "note": note}


def _confirmed_raw(scan_df, snaps, held_codes, mode):
    """主板/未持仓过滤 + 时段确认,返回原始候选(数值未格式化、未截断)。"""
    from quant.selection.confirm import confirm_auction, confirm_tail

    out = []
    if scan_df is None or len(scan_df) == 0:
        return out
    held = {str(c).zfill(6) for c in (held_codes or set())}
    for _, r in scan_df.iterrows():
        code = str(r.get("code") or "").zfill(6)
        if not code.startswith(_MAINBOARD) or code in held:
            continue
        snap = snaps.get(code)
        if not snap:
            continue
        if mode == "auction":
            pct = snap.get("auction_pct")
            ratio = float(snap.get("auction_volume_ratio") or 0)
            ok, why = confirm_auction(None if pct is None else float(pct), ratio)
            price = snap.get("auction_price")
            amount = snap.get("auction_amount")
            pct_value = float(pct) if pct is not None else None
        else:
            price = snap.get("last_price")
            pct = snap.get("price_change_ratio_pct")
            prev = snap.get("prev_price")
            amount = snap.get("turnover")
            if price is None or pct is None or prev is None or amount is None:
                continue
            ok, why = confirm_tail(float(pct), float(price),
                                   float(prev), float(amount), None)
            pct_value = float(pct)
        if not ok:
            continue
        try:
            price_value = float(price)
            amount_value = float(amount) if amount is not None else 0.0
        except (TypeError, ValueError):
            continue
        out.append({
            "code": code,
            "name": str(r.get("name") or code),
            "grade": str(r.get("grade") or ""),
            "horizon": str(r.get("horizon") or ""),
            "score10": float(r.get("score10") or 0),
            "evidence": str(r.get("evidence") or ""),
            "snap": snap,
            "confirm": why,
            "price": price_value,
            "pct_value": pct_value,
            "amount_value": amount_value,
        })
    return out


def confirm_buy_candidates(scan_df, snaps, held_codes, mode, limit=5):
    """对 M3 候选做当前时段确认,返回可买列表(M3 依据 + 时段确认依据)。

    规则:
      - 只保留主板(60/00),已持仓不重复推荐;
      - auction 模式用竞价快照走 confirm_auction;
      - 其余模式用实时快照走 confirm_tail(THS 即时净额缺失按软条件不剔,
        与尾盘推送一致,依据里如实标注);
      - 按推荐分降序取前 limit 只。
    """
    rows = [{
        "code": item["code"],
        "name": item["name"],
        "grade": item["grade"],
        "horizon": item["horizon"],
        "score10": item["score10"],
        "price": f"{item['price']:.2f}",
        "pct": f"{item['pct_value']:+.2f}" if item["pct_value"] is not None else "—",
        "amount_yi": f"{item['amount_value'] / 1e8:.1f}" if item["amount_value"] else "—",
        "confirm": item["confirm"],
        "evidence": item["evidence"],
    } for item in _confirmed_raw(scan_df, snaps, held_codes, mode)]
    rows.sort(key=lambda entry: -entry["score10"])
    return rows[:limit]


def entry_plan(tech, price):
    """建议买入区/止损/止盈;公式与 quant.analysis.assess_entry 完全一致。

    回踩均线:support=max(MA10,MA20) 的 1~1.02 倍;突破前 20 日高:
    high20_prev 的 1~1.02 倍。止损取「参考价 -7%」与「MA20 下方 0.5%」
    中较近者(先与参考价 -1% 取近)。tech 或 price 缺失时返回 None。
    """
    ma10, ma20 = tech.get("ma10"), tech.get("ma20")
    high20 = tech.get("high20_prev")
    if not ma10 or not ma20 or price is None or float(price) <= 0:
        return None
    support = max(ma10, ma20)
    setup, lower, upper = "回踩均线", round(support, 2), round(support * 1.02, 2)
    if high20 and float(price) >= high20:
        setup, lower, upper = "突破前20日高", round(high20, 2), round(high20 * 1.02, 2)
    entry = round((lower + upper) / 2, 2)
    stop = round(max(entry * 0.93, min(ma20 * 0.995, entry * 0.99)), 2)
    return {"setup": setup, "low": lower, "high": upper, "reference": entry,
            "stop": stop, "tp1": round(entry * 1.10, 2),
            "tp2": round(entry * 1.20, 2)}


def _trend_ok(tech, price):
    """技术趋势判定,与 quant.analysis.assess_entry 同式(单一式子,两处共用)。"""
    ma10, ma20, rsi = tech.get("ma10"), tech.get("ma20"), tech.get("rsi14")
    dif, dea = tech.get("dif"), tech.get("dea")
    return bool(price and ma10 and ma20 and price >= ma10 >= ma20
                and dif is not None and dea is not None and dif >= dea
                and rsi is not None and 40 <= rsi <= 70)


def _position_ok(tech, price, plan, pct, amount, high):
    """买入位置判定:候选区内、涨幅 (0,7%]、额≥1亿、离日高回撤≤5%(突破另需量比)。"""
    if not plan or pct is None or price is None:
        return False
    in_band = plan["low"] <= price <= plan["high"]
    if plan["setup"] == "突破前20日高":
        vr = tech.get("vr")
        in_band = bool(in_band and vr is not None and 1.5 <= vr <= 5)
    fall = None
    if high and float(high) > 0:
        try:
            fall = (1 - price / float(high)) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            fall = None
    return bool(in_band and 0 < pct <= 7 and amount and amount >= 1e8
                and fall is not None and fall <= 5)


def assess_buy_candidates(scan_df, snaps, held_codes, mode, hist_loader,
                         limit=5):
    """在时段确认之上逐只做本地技术评估,只留「确定可买/大部分通过」。

    核心条件 4 项 = M3 共振 + 时段确认(第一层已保证) + 技术趋势 + 买入位置:
      - 技术趋势: 价>=MA10>=MA20、DIF>=DEA、RSI 40~70(与 assess_entry 同式);
      - 买入位置: 现价落在建议买入区内、涨幅 (0,7%]、成交额>=1 亿、
        离日高回撤<=5%(突破另需量比 1.5~5)。
    只保留核心通过 >= CORE_MIN(3/4) 的候选;技术指标缺失/历史 K 不足的
    如实跳过并计数(返回 (rows, skipped)),不硬凑。排序:核心通过数
    降序,再按推荐分降序。

    hist_loader(code) 由调用方提供:读本地 quant_hist_daily 最近 90 日 K,
    返回 [{date,close,high,low,volume},...];只读,不在盘中写入。
    """
    from quant.analysis import technical_snapshot

    rows, skipped = [], 0
    for item in _confirmed_raw(scan_df, snaps, held_codes, mode):
        code, snap = item["code"], item["snap"]
        tech = None
        try:
            hist = hist_loader(code) or []
            if hist:
                tech = technical_snapshot(hist, snap, dt.date.today())
        except Exception:  # noqa: BLE001
            tech = None
        if not tech:
            skipped += 1
            continue
        price = item["price"]
        trend_ok = _trend_ok(tech, price)
        plan = entry_plan(tech, price)
        position_ok = _position_ok(tech, price, plan, item["pct_value"],
                                   item["amount_value"], snap.get("high_price"))
        core_pass = 2 + int(trend_ok) + int(position_ok)
        failed = [label for label, ok in (("技术趋势", trend_ok),
                                          ("买入位置", position_ok)) if not ok]
        pct, amount = item["pct_value"], item["amount_value"]
        rows.append({
            "code": code,
            "name": item["name"],
            "grade": item["grade"],
            "horizon": item["horizon"],
            "score10": item["score10"],
            "price": f"{price:.2f}",
            "pct": f"{pct:+.2f}" if pct is not None else "—",
            "amount_yi": f"{amount / 1e8:.1f}" if amount else "—",
            "confirm": item["confirm"],
            "evidence": item["evidence"],
            "core_pass": core_pass,
            "core_total": CORE_TOTAL,
            "verdict_tag": "核心条件全过" if core_pass >= CORE_TOTAL else "通过大部分",
            "failed": "、".join(failed) if failed else "—",
            "setup": (plan or {}).get("setup") or "—",
            "plan_low": (plan or {}).get("low"),
            "plan_high": (plan or {}).get("high"),
            "stop": (plan or {}).get("stop"),
            "tp1": (plan or {}).get("tp1"),
            "tp2": (plan or {}).get("tp2"),
            "kline_date": str(tech.get("hist_date") or ""),
        })
    rows.sort(key=lambda entry: (-entry["core_pass"], -entry["score10"]))
    return [row for row in rows if row["core_pass"] >= CORE_MIN][:limit], skipped


def collect_sell_signals(holdings, analyze_fn, decide_fn):
    """逐持仓跑实时决策,归类出含卖出动作的持仓(附完整依据)。

    返回 (sells, quiet_names, failed):
      sells  —— 动作含 SELL_KEYWORDS 的持仓;
      quiet  —— 暂无卖出信号的持仓名;
      failed —— 行情缺失/分析异常的持仓(如实披露,不硬凑结论)。
    """
    sells, quiet, failed = [], [], []
    for h in holdings or []:
        code = str(h.get("code") or "").zfill(6)
        name = str(h.get("name") or code)
        try:
            result = analyze_fn(h)
        except Exception as error:  # noqa: BLE001
            failed.append({"code": code, "name": name, "error": type(error).__name__})
            continue
        if not result or "error" in result:
            failed.append({"code": code, "name": name,
                           "error": (result or {}).get("error") or "无实时行情"})
            continue
        action, why, levels = decide_fn(result)
        entry = {
            "code": code,
            "name": str(result.get("name") or name),
            "now": f"{float(result['now']):.2f}",
            "pct": f"{float(result['pct']):+.2f}",
            "action": action,
            "why": why,
            "levels": levels,
        }
        cost = h.get("cost")
        if cost:
            try:
                entry["pl_pct"] = f"{(float(result['now']) / float(cost) - 1) * 100:+.2f}"
            except (TypeError, ValueError, ZeroDivisionError):
                entry["pl_pct"] = "—"
        if any(k in action for k in SELL_KEYWORDS):
            sells.append(entry)
        else:
            quiet.append(entry["name"])
    return sells, quiet, failed


# ---------------- 情绪与周期(板块优质股筛选) ----------------
# 数据基础:当日全市场行情(涨跌/涨停/大涨占比)、指数日 K(大盘周期)、
# 本地 90 日 K(个股周期)。全部只读本地库或轻量指数接口,不做全市场抓取。

# 情绪门控:分数越低门越严(涨幅上限收紧/成交额门槛提高)。
_SENTIMENT_CAP = {"冰点": (5.0, 2e8), "谨慎": (5.0, 1e8),
                  "中性": (7.0, 1e8), "活跃": (7.0, 1e8), "亢奋": (7.0, 1e8)}


def market_sentiment(spot_df):
    """当日市场情绪:上涨占比/涨停近似(>=9.8%)/大涨占比(>=5%) → 0-100 分。

    score = 100*(0.5*上涨占比 + 0.3*min(涨停/100,1) + 0.2*min(大涨占比*10,1)),
    分档 亢奋>=80 / 活跃>=60 / 中性>=45 / 谨慎>=30 / 冰点<30,规则透明可复核。
    """
    empty = {"score": None, "label": "不可用", "up_ratio": None,
             "limit_up": None, "hot_ratio": None, "total": 0}
    if spot_df is None or len(spot_df) == 0 or "change_rate" not in spot_df:
        return empty
    pct = pd.to_numeric(spot_df["change_rate"], errors="coerce").dropna()
    if pct.empty:
        return empty
    up = float((pct > 0).mean())
    limit_up = int((pct >= 9.8).sum())
    hot = float((pct >= 5).mean())
    score = round(100 * (0.5 * up + 0.3 * min(limit_up / 100.0, 1)
                         + 0.2 * min(hot * 10, 1)))
    if score >= 80:
        label = "亢奋"
    elif score >= 60:
        label = "活跃"
    elif score >= 45:
        label = "中性"
    elif score >= 30:
        label = "谨慎"
    else:
        label = "冰点"
    return {"score": score, "label": label, "up_ratio": round(up * 100, 1),
            "limit_up": limit_up, "hot_ratio": round(hot * 100, 1),
            "total": int(len(pct))}


def market_cycle(index_rows):
    """大盘周期:上证日 K → 5 日累计/连涨连跌/MA5/MA10 → 周期标签。

    标签:退潮下行(5日<=-3% 或 破MA10 且下行) / 高位过热(5日>=6% 且回落
    MA5 下) / 修复上行(连涨>=2 日且 5 日非负) / 趋势上行(价>=MA5>=MA10) /
    震荡整固(其余)。返回 None 表示数据不可用,如实降级为仅当日情绪。
    """
    if not index_rows or len(index_rows) < 15:
        return None
    rows = sorted(index_rows, key=lambda r: str(r.get("date")))
    try:
        closes = [float(r["close"]) for r in rows if r.get("close")]
    except (TypeError, ValueError):
        return None
    if len(closes) < 15:
        return None
    ma5, ma10, close = sum(closes[-5:]) / 5, sum(closes[-10:]) / 10, closes[-1]
    chg5 = (close / closes[-6] - 1) * 100 if len(closes) >= 6 else 0.0
    streak = 0
    for i in range(len(closes) - 1, 0, -1):
        diff = closes[i] - closes[i - 1]
        if diff > 0 and streak >= 0:
            streak += 1
        elif diff < 0 and streak <= 0:
            streak -= 1
        else:
            break
    if chg5 <= -3 or (close < ma10 and chg5 < 0):
        label = "退潮下行"
    elif chg5 >= 6 and close < ma5:
        label = "高位过热"
    elif streak >= 2 and chg5 >= 0:
        label = "修复上行"
    elif close >= ma5 >= ma10:
        label = "趋势上行"
    else:
        label = "震荡整固"
    return {"label": label, "chg5": round(chg5, 2), "streak": streak,
            "close": round(close, 2), "ma5": round(ma5, 2),
            "ma10": round(ma10, 2), "as_of": str(rows[-1].get("date"))}


def _recently_above_ma20(rows, ma20, price):
    """近 3 根收盘曾在 MA20 下方、现价已站上 → 刚突破(底部启动特征)。"""
    if not ma20 or price is None or price < ma20:
        return False
    for r in rows[-4:-1]:
        close = r.get("close")
        try:
            if close is not None and float(close) < ma20:
                return True
        except (TypeError, ValueError):
            continue
    return False


def stock_cycle(hist_rows, price, tech):
    """个股周期位置:20 日区间位置/近 5 日涨幅/均线关系 → 周期标签。

    下行退潮(破 MA20) / 高位过热(20日位置>=85% 且 近5日>=12% 或 RSI>=75) /
    底部启动(位置<=45% 且刚站上 MA20) / 趋势主升(多头排列且 5 日<=15%) /
    震荡观察(其余)。口径基于本地 K(断档时滞后,由调用方披露)。
    """
    base = {"label": "不可用", "why": "历史K或指标缺失", "pos20": None, "chg5": None}
    if not tech or price is None:
        return base
    rows = sorted(hist_rows or [], key=lambda r: str(r.get("date")))
    ma20, ma10 = tech.get("ma20"), tech.get("ma10")
    above = bool(ma20 and price >= ma20)
    pos20, chg5 = None, None
    try:
        tail = rows[-19:]
        highs = [float(r["high"]) for r in tail if r.get("high")]
        lows = [float(r["low"]) for r in tail if r.get("low")]
        if highs and lows:
            hi, lo = max(max(highs), float(price)), min(min(lows), float(price))
            if hi > lo:
                pos20 = round((price - lo) / (hi - lo) * 100, 1)
        if len(rows) >= 5 and rows[-5].get("close"):
            chg5 = round((price / float(rows[-5]["close"]) - 1) * 100, 1)
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    rsi = tech.get("rsi14") or 0
    if not above:
        label = "下行退潮"
    elif pos20 is not None and pos20 >= 85 and (max(chg5 or 0, 0) >= 12 or rsi >= 75):
        label = "高位过热"
    elif pos20 is not None and pos20 <= 45 and _recently_above_ma20(rows, ma20, price):
        label = "底部启动"
    elif above and ma10 and price >= ma10 >= (ma20 or 0) and (chg5 is None or chg5 <= 15):
        label = "趋势主升"
    else:
        label = "震荡观察"
    ma_text = f"MA20 {ma20:.2f}{'上方' if above else '下方'}" if ma20 else "MA20 缺失"
    why = (f"20日区间位置 {pos20}%" if pos20 is not None else "20日区间不可算")
    why += (f"，近5日 {chg5:+.1f}%" if chg5 is not None else "")
    why += f"，现价在{ma_text}"
    return {"label": label, "why": why, "pos20": pos20, "chg5": chg5}


# 通俗板块大类名 → THS 细分行业关键词(正则 alternation,包含匹配)。
# THS 板块库(320 个)全是细分行业名,「大金融」这类通俗说法靠别名兜底。
_SECTOR_ALIASES = {
    "大金融": "金融|银行|证券|保险|商行",
    "金融": "金融|银行|证券|保险|商行",
    "大科技": "半导体|软件|计算机|通信|电子",
    "科技": "半导体|软件|计算机|通信|电子",
    "大消费": "食品|饮料|酒|家电|零售|服装|纺|化妆品|个护|旅游",
    "消费": "食品|饮料|酒|家电|零售|服装|纺|化妆品|个护|旅游",
    "大医药": "医药|医疗|药|疫苗|生物",
    "医药": "医药|医疗|药|疫苗|生物",
    "大军工": "军工|航空装备|航天装备|地面兵装",
    "军工": "军工|航空装备|航天装备|地面兵装",
    "大地产": "地产",
    "地产": "地产",
    "大能源": "电力|煤炭|石油|油气|火电|水电|风电|光伏|燃气|热力",
    "能源": "电力|煤炭|石油|油气|火电|水电|风电|光伏|燃气|热力",
}


def _alias_pattern(query):
    """返回查询(含「大X」变体)对应的别名关键词正则;无别名返回 None。"""
    variants = {query}
    if query.startswith("大") and len(query) > 2:
        variants.add(query[1:])
    return next((_SECTOR_ALIASES[v] for v in variants if v in _SECTOR_ALIASES), None)


def _suggest_names(stats, names, query):
    """无命中时的建议:先给与查询沾边的板块(滑窗子串),再补当日热门。"""
    import re as _re

    frags = {query}
    if query.startswith("大") and len(query) > 2:
        frags.add(query[1:])
    frags |= {query[i:i + 2] for i in range(len(query) - 1)}
    mask = pd.Series(False, index=stats.index)
    for frag in frags:
        if frag:
            mask |= names.str.contains(_re.escape(frag), na=False, regex=True)
    related = [n for n in names[mask].tolist() if n != query]
    hot = (stats.sort_values("avg_pct", ascending=False)["index_name"]
           .astype(str).head(8).tolist())
    out = related[:6] + [h for h in hot if h not in related][:2]
    return out[:8] or hot


def match_sectors(stats_df, query):
    """按板块名匹配:精确 → 通俗大类别名 → 包含;返回 (code 列表, 名称列表, 建议)。

    别名优先于包含匹配:「金融」若先走包含只会命中「多元金融」,漏掉
    银行/证券/保险,所以查询命中别名表时直接用别名关键词组。
    """
    import re as _re

    q = str(query or "").strip()
    if not q or stats_df is None or stats_df.empty:
        return [], [], []
    names = stats_df["index_name"].astype(str)
    exact = stats_df[names == q]
    if len(exact):
        hits = exact
    else:
        pattern = _alias_pattern(q)
        if pattern:
            hits = stats_df[names.str.contains(pattern, na=False, regex=True)]
        else:
            hits = stats_df[names.str.contains(q, na=False)]
    suggest = _suggest_names(stats_df, names, q)
    return (hits["index_code"].tolist(), hits["index_name"].astype(str).tolist(),
            suggest)


def sector_prefilter(members_df, spot_df, sentiment, held_codes, limit=24):
    """板块成分粗筛(纯本地):主板、剔除 ST/已持仓,涨幅与成交额按情绪门控。

    情绪门控:冰点/谨慎(分数<45) 涨幅上限 5%,冰点另要求成交额≥2亿;
    其余 7% / 1亿。按成交额降序取前 limit 只进入逐股技术评估。
    返回 (候选 DataFrame, 门控说明)。
    """
    gate = _SENTIMENT_CAP.get(sentiment.get("label") or "", (7.0, 1e8))
    pct_cap, amount_min = gate
    note = (f"情绪门控：市场{sentiment.get('label') or '未知'}"
            f"({sentiment.get('score') if sentiment.get('score') is not None else '—'}/100)，"
            f"候选涨幅上限 {pct_cap:.0f}%、成交额≥{amount_min / 1e8:.0f}亿，"
            "剔除高位过热周期个股。")
    if members_df is None or members_df.empty or spot_df is None or spot_df.empty:
        return members_df.iloc[0:0] if members_df is not None else None, note
    df = members_df.merge(spot_df, on="code", how="inner", suffixes=("", "_spot"))
    df = df[df["code"].astype(str).str.match(r"^(60|00)")]
    if "name_spot" in df:
        df = df[~df["name_spot"].astype(str).str.contains("ST", na=False)]
    df = df[~df["name"].astype(str).str.contains("ST", na=False)]
    held = {str(c).zfill(6) for c in (held_codes or set())}
    df = df[~df["code"].isin(held)]
    for col in ("change_rate", "deal_amount"):
        df = df.assign(**{col: pd.to_numeric(df[col], errors="coerce")})
    df = df[(df["change_rate"] > 0) & (df["change_rate"] <= pct_cap)
            & (df["deal_amount"] >= amount_min)]
    df = df.sort_values("deal_amount", ascending=False).head(limit)
    return df, note


# 板块优质股门槛:优质分 >= 2(技术趋势/买入位置至少一项全过);周期
# 剔除(高位过热/下行退潮)之外的弱分股如实过滤并计数,不硬凑。
QUALITY_MIN = 2.0


def sector_pick(cands, snaps, sentiment, cycle, sector_stat, hist_loader,
                limit=8):
    """板块内优质股终评:技术趋势/买入位置 + 周期标签 + 情绪适配 → 优质分。

    优质分(满分6):技术趋势2 + 买入位置2 + 周期(主升/底部启动+1,震荡0) +
    成交额≥5亿 0.5 + 板块当日上涨占比>50%且均涨>0 0.5。
    周期硬门:「高位过热」与「下行退潮」一律剔除并如实计数(做好周期判断,
    过热不追、退潮不接)。snaps 为可选实时快照(优先于喂表价,缺失回退并
    标注)。返回 (rows, meta)。
    """
    from quant.analysis import technical_snapshot

    rows, skipped = [], 0
    dropped = {"高位过热": 0, "下行退潮": 0}
    sector_hot = bool(sector_stat and (sector_stat.get("avg_pct") or 0) > 0
                      and (sector_stat.get("up_ratio") or 0) > 0.5)
    for _, r in (cands.iterrows() if cands is not None and len(cands) else []):
        code = str(r.get("code") or "").zfill(6)
        snap = (snaps or {}).get(code) or {}
        spot_price, pct, amount = (r.get("new_price"), r.get("change_rate"),
                                   r.get("deal_amount"))
        realtime = False
        price = snap.get("last_price")
        if price:
            price, realtime = float(price), True
        else:
            price = float(spot_price) if spot_price else None
        if price is None or pct is None or amount is None:
            continue
        high = snap.get("high_price") or r.get("high_price")
        tech, hist = None, None
        try:
            hist = hist_loader(code) or []
            if hist:
                tech = technical_snapshot(hist, snap or None, dt.date.today())
        except Exception:  # noqa: BLE001
            tech = None
        if not tech:
            skipped += 1
            continue
        cyc = stock_cycle(hist, price, tech)
        if cyc["label"] in dropped:
            dropped[cyc["label"]] += 1
            continue
        trend_ok = _trend_ok(tech, price)
        plan = entry_plan(tech, price)
        position_ok = _position_ok(tech, price, plan, float(pct),
                                   float(amount), high)
        cycle_bonus = 1 if cyc["label"] in ("趋势主升", "底部启动") else 0
        quality = (2 * int(trend_ok) + 2 * int(position_ok) + cycle_bonus
                   + (0.5 if float(amount) >= 5e8 else 0)
                   + (0.5 if sector_hot else 0))
        parts = []
        if trend_ok:
            parts.append(f"趋势过(价{price:.2f}≥MA10{tech['ma10']:.2f}≥MA20{tech['ma20']:.2f})")
        else:
            parts.append("趋势未过")
        parts.append("位置过" if position_ok else "位置未过")
        parts.append(f"周期{cyc['label']}({cyc['why']})")
        parts.append(f"情绪:个股{float(pct):+.1f}%·市场{sentiment.get('label')}"
                     f"({sentiment.get('score') if sentiment.get('score') is not None else '—'}/100)"
                     + (f"·大盘{cycle['label']}" if cycle else "·大盘周期不可用"))
        rows.append({
            "code": code,
            "name": str(r.get("name_spot") or r.get("name") or code),
            "price": f"{price:.2f}",
            "realtime": realtime,
            "pct": f"{float(pct):+.2f}",
            "amount_yi": f"{float(amount) / 1e8:.1f}",
            "quality": f"{quality:.1f}",
            "trend_ok": trend_ok,
            "position_ok": position_ok,
            "cycle_label": cyc["label"],
            "cycle_why": cyc["why"],
            "setup": (plan or {}).get("setup") or "—",
            "plan_low": (plan or {}).get("low"),
            "plan_high": (plan or {}).get("high"),
            "stop": (plan or {}).get("stop"),
            "tp1": (plan or {}).get("tp1"),
            "tp2": (plan or {}).get("tp2"),
            "kline_date": str(tech.get("hist_date") or ""),
            "evidence": "；".join(parts),
        })
    rows.sort(key=lambda entry: (-float(entry["quality"]),
                                 -float(entry["amount_yi"])))
    kept = [r for r in rows if float(r["quality"]) >= QUALITY_MIN]
    meta = {"skipped": skipped, "dropped_hot": dropped["高位过热"],
            "dropped_retreat": dropped["下行退潮"],
            "below_min": len(rows) - len(kept)}
    return kept[:limit], meta


__all__ = ["current_slot", "closed_slot", "confirm_buy_candidates",
           "entry_plan", "assess_buy_candidates", "collect_sell_signals",
           "market_sentiment", "market_cycle", "stock_cycle",
           "match_sectors", "sector_prefilter", "sector_pick",
           "SELL_KEYWORDS"]
