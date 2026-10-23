# -*- coding: utf-8 -*-
"""单只 A 股的证据化买点分析；与持仓配置、交易执行完全分离。

采样时间不是交易所逐笔报价时间。任何缺失/过期/相互矛盾的关键证据
只能使买入结论降级，不能把旧价或新闻关键词当成肯定性信号。
"""

from __future__ import annotations

import datetime as dt
import math
import re

import pandas as pd

from quant import db as qdb
from quant.datasource import sector, ths
from quant.indicators import add_indicators
from quant.rules import ADD_LOSS_BAN, stop_take_prices


_CODE = re.compile(r"^(?:0[0-3]|30|60|68)\d{4}$")
_NAME = re.compile(r"^[一-鿿A-Za-z0-9·（）()*]{2,20}$")


def resolve_stock(query):
    """返回 (股票字典|None, 候选列表, 提示)。只查本地最新日期的股票目录。"""
    query = str(query or "").strip()
    if not query:
        return None, [], "请输入六位 A 股代码或股票名称。"
    if not _NAME.fullmatch(query) or (query.isdigit() and not _CODE.fullmatch(query)):
        return None, [], "请输入有效的六位 A 股代码，或 2～20 字的股票名称。"
    conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            base = ("SELECT code,name,industry,`date` FROM cn_stock_spot "
                    "WHERE `date`=(SELECT MAX(`date`) FROM cn_stock_spot) AND ")
            if _CODE.fullmatch(query):
                cur.execute(base + "code=%s LIMIT 1", (query,))
                rows = cur.fetchall()
            else:
                cur.execute(base + "name=%s LIMIT 10", (query,))
                rows = cur.fetchall()
                if not rows:
                    cur.execute(base + "name LIKE %s ORDER BY name,code LIMIT 11",
                                (f"%{query}%",))
                    rows = cur.fetchall()
    finally:
        conn.close()
    results = [dict(code=str(r[0]), name=r[1], industry=r[2] or "", date=str(r[3]))
               for r in rows if _CODE.fullmatch(str(r[0]))]
    if len(results) == 1:
        return results[0], [], ""
    if results:
        return None, results[:10], "匹配到多只股票，请选择完整名称或代码。"
    return None, [], "最新股票目录未匹配到该股票；请核对代码或名称。"


def search_directory(query, limit=10):
    """输入代码或名称的前缀/包含匹配，返回 [{code,name,industry}]，代码前缀优先。"""
    query = str(query or "").strip()
    try:
        limit = max(1, min(int(limit or 10), 20))
    except (TypeError, ValueError):
        limit = 10
    if not query or len(query) > 24:
        return []
    prefix, contain = f"{query}%", f"%{query}%"
    conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT code,name,industry FROM cn_stock_spot "
                "WHERE `date`=(SELECT MAX(`date`) FROM cn_stock_spot) "
                "AND (code LIKE %s OR name LIKE %s OR code LIKE %s OR name LIKE %s) "
                "ORDER BY (code LIKE %s) DESC, (name LIKE %s) DESC, code LIMIT %s",
                (prefix, prefix, contain, contain, prefix, prefix, limit))
            rows = cur.fetchall()
    finally:
        conn.close()
    return [dict(code=str(r[0]), name=str(r[1] or ""), industry=str(r[2] or ""))
            for r in rows if _CODE.fullmatch(str(r[0]))]


def technical_snapshot(history, quote, today):
    """历史日 K + 今日单只 THS 快照拼接，指标暂值不冒充收盘确认值。"""
    rows = [dict(r) for r in history if str(r["date"])[:10] <= today.isoformat()]
    rows.sort(key=lambda r: str(r["date"]))
    if not rows:
        return None
    previous = [r for r in rows if str(r["date"])[:10] < today.isoformat()]
    hist_date = str(rows[-1]["date"])[:10]
    merged = False
    previous_close = float(previous[-1]["close"]) if previous else None
    if quote and previous_close and _positive(quote.get("last_price")):
        prev = _positive(quote.get("prev_price"))
        price = float(quote["last_price"])
        volume = _positive(quote.get("volume"))
        # 昨收与最近历史 K 不一致时可能是旧快照、除权或数据错配，不能拼接。
        if prev and abs(prev / previous_close - 1) <= 0.01 and volume:
            high = max(price, _positive(quote.get("high_price")) or price)
            low = min(price, _positive(quote.get("low_price")) or price)
            rows = previous + [dict(date=today.isoformat(), close=price,
                                    high=high, low=low, volume=volume)]
            merged = True
    if len(rows) < 65:
        return None
    frame = pd.DataFrame(rows)
    for key in ("close", "high", "low", "volume"):
        frame[key] = pd.to_numeric(frame[key], errors="coerce")
    if frame[["close", "high", "low", "volume"]].isna().any().any():
        return None
    frame = add_indicators(frame)
    last = frame.iloc[-1]

    def number(key):
        value = last[key]
        return float(value) if pd.notna(value) and math.isfinite(float(value)) else None

    return {"ma5": number("ma5"), "ma10": number("ma10"), "ma20": number("ma20"),
            "ma60": number("ma60"), "rsi14": number("rsi14"),
            "dif": number("dif"), "dea": number("dea"),
            "vr": number("vr"), "high20_prev": number("high20_prev"),
            "previous_close": previous_close, "hist_date": hist_date,
            "indicator_date": str(last["date"])[:10], "quote_merged": merged}


def _positive(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def _collect_date(value):
    try:
        return dt.datetime.fromisoformat(str(value)[:19])
    except (TypeError, ValueError):
        return None


def _market_state(now):
    if now.weekday() >= 5:
        return False, "非工作日，今日不可买入。"
    clock = now.time()
    if dt.time(9, 30) <= clock <= dt.time(11, 30) or dt.time(13) <= clock < dt.time(14, 57):
        return True, "连续竞价时段（仅规则条件，不是成交保证）。"
    if clock >= dt.time(15):
        return False, "今日交易时段已结束，不能再于今日买入。"
    if dt.time(14, 57) <= clock < dt.time(15):
        return False, "收盘集合竞价时段，不将采样价视为可执行买点。"
    return False, "当前不在连续竞价时段，不生成今日可执行买入结论。"


def assess_entry(facts, now=None, holding=None, total_assets=None):
    """纯规则判断；所有必需检查均通过才返回「条件具备」，其余暂缓。"""
    now = now or dt.datetime.now()
    quote, tech = facts.get("quote") or {}, facts.get("tech") or {}
    price = _positive(quote.get("last_price"))
    today = now.date().isoformat()
    opened, market_note = _market_state(now)
    quote_date = facts.get("quote_date")
    trade_at = _collect_date(facts.get("quote_trade_at"))
    trade_age = (now - trade_at).total_seconds() if trade_at else None
    trade_is_today = bool(trade_at and trade_at.date() == now.date()
                          and trade_age is not None and trade_age >= 0)
    # 只在连续竞价时段内要求 ≤180s 的实时分钟 K；收盘/非交易时段允许使用当日较旧的
    # 分钟 K（同日期即可），价格计划标注为下一时段参考情景。
    trade_age_ok = bool(trade_age is not None
                        and (trade_age <= 180 or not opened))
    quote_has_time = bool(trade_is_today and trade_age_ok)
    pct = quote.get("price_change_ratio_pct")
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        pct = None
    checks, warnings = [], []

    def check(label, ok, detail):
        checks.append({"label": label, "ok": bool(ok), "detail": detail})

    check("交易时段", opened, market_note)
    check("可核对的成交时刻", quote_has_time,
          f"东财单股分钟 K 最新成交 {facts['quote_trade_at']} 与 THS 报价相符；"
          "跨来源采样不保证实际下单成交。" if quote_has_time else
          "缺少与 THS 快照价格相符的当日分钟 K，或盘中已超过3分钟；不能把采集时间当成交时间。")

    quote_at = _collect_date(facts.get("quote_at"))
    age = (now - quote_at).total_seconds() if quote_at else None
    spot_date = facts.get("spot_date")
    hist_date = _collect_date(tech.get("hist_date"))
    valid_quote = bool(price and quote_has_time and quote_at and age is not None and 0 <= age <= 180
                       and spot_date == today and quote_date == today
                       and hist_date and 0 <= (now.date() - hist_date.date()).days <= 7
                       and tech.get("quote_merged"))
    check("行情与日K", valid_quote,
          f"THS 快照采集 {facts.get('quote_at') or '未知'}；本地日K至 {tech.get('hist_date') or '未知'}；"
          "接口不提供逐笔成交时刻，指标以当日快照临时拼接。"
          if valid_quote else "报价无法核实近3分钟成交/当日交易日期、已过期，或目录、历史 K、昨收无法匹配；不能确认今日价格。")

    ma10, ma20, rsi = (tech.get(k) for k in ("ma10", "ma20", "rsi14"))
    dif, dea = tech.get("dif"), tech.get("dea")
    trend_ok = bool(price and ma10 and ma20 and price >= ma10 >= ma20
                    and dif is not None and dea is not None and dif >= dea
                    and rsi is not None and 40 <= rsi <= 70)
    check("技术趋势", trend_ok,
          (f"MA10 {ma10:.2f} / MA20 {ma20:.2f}，RSI14 {rsi:.1f}，"
           f"MACD DIF {dif:.2f} / DEA {dea:.2f}；要求价≥MA10≥MA20、DIF≥DEA、RSI 40～70。")
          if all(x is not None for x in (ma10, ma20, rsi, dif, dea))
          else "历史 K 不足或技术指标缺失，不输出确定买点。")

    high20 = tech.get("high20_prev")
    # 仅当日快照能与本地日期交叉核验时，才给出候选价格；旧价不能生成「今日买点」。
    support = max(ma10, ma20) if valid_quote and ma10 and ma20 else None
    setup = "回踩均线"
    if support:
        lower = round(support, 2)
        upper = round(support * 1.02, 2)
        if price and high20 and price >= high20:
            setup, lower, upper = "突破前20日高", round(high20, 2), round(high20 * 1.02, 2)
        entry = round((lower + upper) / 2, 2)
        stop = round(max(entry * 0.93, min(ma20 * 0.995, entry * 0.99)), 2)
        plan = {"setup": setup, "low": lower, "high": upper, "reference": entry,
                "stop": stop, "tp1": round(entry * 1.10, 2),
                "tp2": round(entry * 1.20, 2)}
    else:
        plan = None
    vr = tech.get("vr")
    in_band = bool(plan and price and plan["low"] <= price <= plan["high"])
    if setup == "突破前20日高":
        in_band = bool(in_band and vr is not None and 1.5 <= vr <= 5)
    high = _positive(quote.get("high_price"))
    fall_from_high = (1 - price / high) * 100 if price and high else None
    liquid = (_positive(quote.get("turnover")) or 0) >= 1e8
    position_ok = bool(in_band and pct is not None and 0 < pct <= 7 and liquid and
                       fall_from_high is not None and fall_from_high <= 5)
    check("买入位置", position_ok,
          (f"{setup}候选区 {plan['low']:.2f}～{plan['high']:.2f}；"
           f"采样价 {price:.2f}，当日 {pct:+.2f}%" if plan and price and pct is not None
           else "买入区间不可计算或涨幅缺失") +
          f"；成交额≥1亿，离日高回撤≤5%，突破时量比 1.5～5。"
          + (f"本次回撤 {fall_from_high:.1f}%，量比 {vr:.2f}。" if fall_from_high is not None and vr is not None else ""))

    state = facts.get("sector") or {}
    sector_ok = bool(state.get("resonant") and state.get("source") in ("THS指数实时", "THS实时")
                     and facts.get("sector_at") and
                     _collect_date(facts["sector_at"]) and
                     0 <= (now - _collect_date(facts["sector_at"])).total_seconds() <= 300)
    sector_pct = state.get("pct", state.get("pct_mid"))
    check("板块共振", sector_ok,
          f"{state.get('name', state.get('industry', '未匹配'))} "
          f"{sector_pct if sector_pct is not None else '—'}% · "
          f"{state.get('source', '无来源')} · 采集 {facts.get('sector_at') or '未知'}；"
          "仅接受 THS 5 分钟内的当日共振，不以东财昨日替代。")

    ths_flow, em_flow = facts.get("ths_flow") or {}, facts.get("em_flow") or {}
    ths_net, em_net = ths_flow.get("net"), em_flow.get("main_net")
    ths_at = _collect_date(facts.get("ths_flow_at"))
    em_at = _collect_date(em_flow.get("last_time"))
    funds_ok = bool(ths_net is not None and em_net is not None
                    and ths_net >= 0 and em_net >= 0 and valid_quote
                    and ths_at and 0 <= (now - ths_at).total_seconds() <= 300
                    and em_at and em_at.date() == now.date()
                    and 0 <= (now - em_at).total_seconds() <= 600)
    check("资金双口径", funds_ok,
          (f"THS 全单 {ths_net / 1e8:+.2f} 亿" if isinstance(ths_net, (int, float))
           else "THS 全单缺失") + "；" +
          (f"东财大单 {em_net / 1e8:+.2f} 亿（{em_flow.get('last_time')}）"
           if isinstance(em_net, (int, float)) else "东财大单缺失") +
          "。要求两个不同口径同为非负且数据未过期；方向分歧时暂缓。")

    recent = []
    stock_name = str(facts.get("name") or "").strip()
    stock_code = str(facts.get("code") or "").strip()
    for item in facts.get("news") or []:
        published = _collect_date(item.get("time"))
        text = f"{item.get('title') or ''} {item.get('digest') or ''}"
        related = ((stock_name and stock_name in text)
                   or (stock_code and stock_code in text))
        if related and published and dt.timedelta(0) <= now - published <= dt.timedelta(days=14):
            recent.append(item)
    negatives = [item for item in recent if item.get("tag") == "利空"]
    news_ok = facts.get("news_status") == "ok" and bool(recent) and not negatives
    check("消息核对", news_ok,
          ("近14日相关媒体新闻含风险关键词，需核对公告原文，暂缓买入。" if negatives else
           "近14日有个股相关媒体条目、未检出风险关键词；仅完成媒体标题初筛，未核验公司公告。")
          if recent else "近14日没有个股相关媒体条目或获取失败；不把大盘新闻当作个股消息。")
    warnings.append("消息面仅有媒体标题初筛，未核验交易所/巨潮公告原文；不凭该项单独决定买入。")

    holding = holding or {}
    cost = _positive(holding.get("cost"))
    add_banned = bool(price and cost and price / cost - 1 <= -ADD_LOSS_BAN)
    check("持仓与权限", not add_banned and not str(facts.get("code", "")).startswith(("30", "68")),
          (f"已有仓位成本 {cost:.3f}，当前浮亏 {(price / cost - 1) * 100:.2f}%，"
           "按纪律禁止亏损加仓。" if add_banned else
           "创业板/科创板交易权限未在系统中核验，暂不输出可买结论。"
           if str(facts.get("code", "")).startswith(("30", "68")) else
           "未触发亏损加仓禁令；单票≤25%、总仓位上限仍需人工核验。"))
    has_assets = bool(_positive(total_assets))
    if not has_assets:
        warnings.append("总资产未配置，无法自动核验单票 25% 和总仓位上限；不得据此确定下单股数。")
    else:
        warnings.append("单票市值不得超过总资产 25%，还须核验其他持仓与总仓位上限。")
    if cost:
        warnings.append("已持有股票：以下新仓参考线不替代原持仓纪律止损/止盈线。")
    warnings.append("A 股 T+1、跳空和滑点可能令止损价无法成交；消息标题不等于公告，须核对原文。")
    score = sum(c["ok"] for c in checks)
    ready = all(c["ok"] for c in checks)
    if not opened:
        verdict = "今日不可买入 · 非连续竞价时段"
    elif not has_assets:
        verdict = "暂不买入 · 总资产未配置，仓位无法核验"
        ready = False
    elif ready:
        verdict = "规则条件具备 · 仅可在候选区小仓观察"
    else:
        verdict = "暂不买入 · 条件尚不齐"
    return {"verdict": verdict, "ready": ready, "checks": checks, "warnings": warnings,
            "passed": score, "total": len(checks),
            "plan": plan, "holding_levels": stop_take_prices(holding, ma20) if cost else None}


def fetch_analysis(code):
    """后台拉取单只股票；来源失败保留缺口而不是填造 0 或回填旧价。"""
    conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name,industry,`date`,pre_close_price FROM cn_stock_spot WHERE code=%s "
                        "ORDER BY `date` DESC LIMIT 1", (code,))
            spot = cur.fetchone()
            cur.execute("SELECT `date`,close,high,low,volume FROM quant_hist_daily "
                        "WHERE code=%s ORDER BY `date` DESC LIMIT 90", (code,))
            hist = [dict(date=str(r[0]), close=r[1], high=r[2], low=r[3], volume=r[4])
                    for r in cur.fetchall()]
    finally:
        conn.close()
    if not spot:
        raise LookupError(f"本地股票目录没有 {code}")
    facts = {"code": code, "name": spot[0], "industry": spot[1] or "",
             "spot_date": str(spot[2]), "source_errors": {},
             "quote": None, "quote_at": None, "quote_date": None, "quote_trade_at": None,
             "sector": None, "sector_at": None,
             "ths_flow": None, "ths_flow_at": None, "em_flow": None,
             "news": [], "news_status": "error"}
    try:
        # 单只快照设短超时和一次请求，不进入通用适配层的多轮退避。
        items = (ths.call("/api/a-share/prices/snapshot",
                          {"thscodes": ths.to_thscode(code)}, retries=1, timeout=6)
                 or {}).get("item") or []
        facts["quote"] = next((q for q in items
                               if str(q.get("thscode", "")).split(".")[0] == code), None)
        if not facts["quote"]:
            facts["source_errors"]["行情"] = "THS 未返回目标代码的快照"
        facts["quote_at"] = dt.datetime.now().isoformat(timespec="seconds")
        if facts["quote"] and str(spot[2]) == dt.date.today().isoformat() and hist:
            prev_close = next((r["close"] for r in hist if r["date"] < str(spot[2])), None)
            db_quote_ok = bool(prev_close and _positive(spot[3]) and
                               abs(float(spot[3]) / float(prev_close) - 1) <= .01)
            # THS 快照不含交易日期。休市后只在最新日目录昨收与历史 K 一致、
            # 且 THS 报价与当日日内区间一致时，才把它作为「截至目录日期」候选。
            q = facts["quote"]
            price = _positive(q.get("last_price"))
            qprev = _positive(q.get("prev_price"))
            high = _positive(q.get("high_price"))
            low = _positive(q.get("low_price"))
            if (db_quote_ok and qprev and prev_close and price and high and low
                    and abs(qprev / float(prev_close) - 1) <= .01
                    and low <= price <= high):
                facts["quote_date"] = str(spot[2])
        if facts["quote"] and not facts["quote_date"]:
            facts["source_errors"]["日期"] = "THS 快照无交易日字段，无法与本地当日数据交叉核验"
        if facts["quote_date"]:
            from quant.datasource.minute_validation import last_minute_bar
            bar = last_minute_bar(code)
            if (bar and bar["time"][:10] == facts["quote_date"]
                    and abs(bar["close"] / float(facts["quote"]["last_price"]) - 1) <= .015):
                facts["quote_trade_at"] = bar["time"]
            else:
                facts["source_errors"]["成交时刻"] = "分钟 K 缺失或其价格与 THS 快照不一致"
    except Exception as exc:
        facts["source_errors"]["行情"] = type(exc).__name__
    facts["tech"] = technical_snapshot(hist, facts["quote"], dt.date.today())
    try:
        # 只查询已由看板/其他调用缓存好的板块；无缓存时不临时触发
        # THS 全行业限流重试与全市场聚合。缺失显式展示为未通过。
        cache = getattr(sector, "_heat_cache", {})
        heat = cache.get("data") or {}
        if heat and dt.datetime.now().timestamp() - cache.get("ts", 0) <= 300:
            from sqlalchemy import text
            with qdb.get_engine().connect() as connection:
                indices = connection.execute(text(
                    "SELECT index_code FROM quant_ths_membership WHERE code=:code "
                    "AND updated=(SELECT MAX(updated) FROM quant_ths_membership)"),
                    {"code": code}).fetchall()
            states = [heat[r[0]] for r in indices if r[0] in heat]
            if states:
                facts["sector"] = next((s for s in states if s.get("resonant")),
                                       min(states, key=lambda s: s.get("rank", 999)))
                facts["sector_at"] = dt.datetime.fromtimestamp(cache["ts"]).isoformat(timespec="seconds")
        if not facts["sector"]:
            facts["source_errors"]["板块"] = "THS 行业热度缓存未就绪，未临时触发全市场拉取"
    except Exception as exc:
        facts["source_errors"]["板块"] = type(exc).__name__
    try:
        from quant.datasource import fundflow_realtime as flow
        # 优先 3 分钟内的共用缓存；否则读喂表任务的单票记录，严格核对日期/更新时间。
        cache = flow._CACHE
        if cache.get("df") is not None and dt.datetime.now().timestamp() - cache.get("ts", 0) <= 180:
            hit = cache["df"].loc[cache["df"]["code"] == code]
            if not hit.empty and pd.notna(hit.iloc[0]["net"]):
                facts["ths_flow"] = {"net": float(hit.iloc[0]["net"])}
                facts["ths_flow_at"] = dt.datetime.fromtimestamp(cache["ts"]).isoformat(timespec="seconds")
        if not facts["ths_flow"]:
            with qdb.get_connection() as flow_conn:
                with flow_conn.cursor() as cur:
                    cur.execute("SELECT `date`,net,updated_at FROM quant_flow_realtime_today "
                                "WHERE code=%s LIMIT 1", (code,))
                    row = cur.fetchone()
            if row and str(row[0]) == dt.date.today().isoformat() and row[1] is not None:
                facts["ths_flow"] = {"net": float(row[1])}
                facts["ths_flow_at"] = row[2].isoformat(timespec="seconds") if row[2] else None
        if not facts["ths_flow"]:
            facts["source_errors"]["THS 资金"] = "仅有旧日资金记录，未用于今日判断"
    except Exception as exc:
        facts["source_errors"]["THS 资金"] = type(exc).__name__
    try:
        from quant.datasource.fundflow_realtime import main_flow_today
        facts["em_flow"] = main_flow_today(code, attempts=1, request_retries=1, timeout=6)
    except Exception as exc:
        facts["source_errors"]["东财资金"] = type(exc).__name__
    try:
        from quant.datasource import news as news_source
        facts["news"] = news_source.stock_news(code, limit=5, timeout=7)
        facts["news_status"] = "ok"
    except Exception as exc:
        facts["source_errors"]["消息"] = type(exc).__name__
    facts["completed_at"] = dt.datetime.now().isoformat(timespec="seconds")
    return facts
