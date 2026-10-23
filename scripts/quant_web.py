# -*- coding: utf-8 -*-
"""量化实时看板(Web,端口 9989)。

页面以服务端缓存展示,缓存失效时后台重建,不阻塞页面请求:
  ① 今日 S 级推荐(推荐分/依据/实时价与涨跌)
  ② 持仓实时监控卡(实时价/主力实时/THS净额/板块/建议与关键位)
  ③ 今日推送记录与模拟盘记录
  ④ THS 行业指数实时榜与全市场资金净额榜
数据口径:推荐因子=收盘口径;价格/资金/板块=盘中实时。
"""

import datetime as dt
import html
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import tornado.ioloop
import tornado.web

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "instock"))

import quant.db as qdb  # noqa: E402
from quant.datasource import sector as sec_mod  # noqa: E402
from quant.datasource import ths  # noqa: E402
from quant.datasource.fundflow_realtime import ths_instant_flow  # noqa: E402
from quant.selection.engine import scan  # noqa: E402

import holding_decision as hd  # noqa: E402

_CACHE = {}
_CACHE_LOCK = threading.RLock()
_PORTFOLIO_LOCK = threading.RLock()
_CACHE_REFRESHING = set()
_CACHE_RETRY_AFTER = {}
_CACHE_GENERATION = {}
_REFRESH_ALL = {"running": False}
_CACHE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="quant-cache")


def cached(key, ttl, fn):
    """同步缓存，仅供耗时可控且严格本地计算的调用。"""
    now = time.time()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and now - entry[0] < ttl:
            return entry[1]
        generation = _CACHE_GENERATION.get(key, 0)
    result = fn()
    with _CACHE_LOCK:
        if _CACHE_GENERATION.get(key, 0) == generation:
            _CACHE[key] = (time.time(), result)
    return result


def _refresh_cache(key, fn, generation=None):
    with _CACHE_LOCK:
        if generation is None:
            generation = _CACHE_GENERATION.get(key, 0)
    try:
        value = fn()
    except Exception as error:  # noqa: BLE001
        print(f"[cache] {key} 后台刷新失败: {error}", flush=True)
        with _CACHE_LOCK:
            if _CACHE_GENERATION.get(key, 0) == generation:
                _CACHE_RETRY_AFTER[key] = time.time() + 10
    else:
        with _CACHE_LOCK:
            if _CACHE_GENERATION.get(key, 0) == generation:
                _CACHE[key] = (time.time(), value)
                _CACHE_RETRY_AFTER.pop(key, None)
                if key == "snap":
                    for cache_key in tuple(_CACHE):
                        if cache_key in ("cards", "main", "positions") or cache_key.startswith("stock:"):
                            _CACHE.pop(cache_key, None)
                            _CACHE_GENERATION[cache_key] = _CACHE_GENERATION.get(cache_key, 0) + 1
    finally:
        with _CACHE_LOCK:
            _CACHE_REFRESHING.discard(key)


def cached_stale(key, ttl, fn, fallback):
    """过期缓存先返回并后台刷新，冷启动先返回 fallback。"""
    now = time.time()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and now - entry[0] < ttl:
            return entry[1]
        if key not in _CACHE_REFRESHING and now >= _CACHE_RETRY_AFTER.get(key, 0):
            _CACHE_REFRESHING.add(key)
            generation = _CACHE_GENERATION.get(key, 0)
            try:
                _CACHE_EXECUTOR.submit(_refresh_cache, key, fn, generation)
            except RuntimeError as error:
                _CACHE_REFRESHING.discard(key)
                _CACHE_RETRY_AFTER[key] = now + 10
                print(f"[cache] {key} 无法提交后台刷新: {error}", flush=True)
        return entry[1] if entry else fallback


def invalidate_cache(*keys):
    with _CACHE_LOCK:
        for key in keys:
            _CACHE.pop(key, None)
            _CACHE_RETRY_AFTER.pop(key, None)
            _CACHE_GENERATION[key] = _CACHE_GENERATION.get(key, 0) + 1


def warm_all():
    """预热看板缓存，不阻塞 Tornado 请求线程。"""
    cached_stale("scan", 600, lambda: scan(top_n=40, return_stats=True),
                 (pd.DataFrame(), {}))
    empty = pd.DataFrame(columns=["price", "pct"]).rename_axis("code")
    cached_stale("snap", 20, _fetch_snapshot, empty)
    cached_stale("flow", 60, _fetch_flow, pd.DataFrame())
    cached_stale("heat", 120, _fetch_heat, {})
    cached_stale("paper", 120, _fetch_paper, pd.DataFrame())
    _sector_stats()   # 板块聚合(板块优质股筛选用)
    _index_cycle()    # 大盘周期(板块优质股筛选用)
    from quant.strategies.leader import leader_scan  # noqa: E402

    cached_stale("leader", 1800, lambda: leader_scan() or {}, {})  # 涨停梯队


def get_scan():
    def fetch():
        try:
            return scan(top_n=40, return_stats=True)
        except Exception:  # noqa: BLE001
            return pd.DataFrame(), {}
    return cached("scan", 600, fetch)


def _fetch_snapshot():
    items = []
    for offset in (0, 5000):
        try:
            response = ths.prices_snapshot(limit=5000, offset=offset) or {}
            page = response.get("item") or []
            items.extend(page)
            if len(page) < 5000:
                break
        except Exception as error:  # noqa: BLE001
            print(f"[cache] THS 行情获取失败: {error}", flush=True)
            break
    frame = pd.DataFrame([{
        "code": (item.get("thscode") or "").split(".")[0],
        "price": item.get("last_price"),
        "pct": item.get("price_change_ratio_pct"),
    } for item in items])
    if frame.empty:
        raise RuntimeError("THS 行情快照无数据；保留上次缓存")
    return frame.set_index("code")


def _portfolio_context(snapshot=None, migrate_legacy_cash=True):
    """加载配置，以单个行情快照计算资产，并仅迁移一次旧总资产。"""
    from quant.portfolio import calculate_portfolio, infer_legacy_cash_balance

    with _PORTFOLIO_LOCK:
        cfg = _load_config()
    holds = [dict(item, code=str(item.get("code") or "").strip().zfill(6))
             for item in (cfg.get("holdings") or [])]
    if snapshot is None:
        empty = pd.DataFrame(columns=["price", "pct"]).rename_axis("code")
        snapshot = cached_stale("snap", 20, _fetch_snapshot, empty)
    prices = {
        item["code"]: (snapshot.loc[item["code"]]["price"]
                       if snapshot is not None and not snapshot.empty
                       and item["code"] in snapshot.index else None)
        for item in holds
    }
    portfolio = cfg.get("portfolio") or {}
    cash = portfolio.get("cash_balance")
    legacy_assets = portfolio.get("total_assets")
    cash_estimated = bool(portfolio.get("cash_balance_estimated"))
    if cash is None and migrate_legacy_cash:
        inferred = infer_legacy_cash_balance(holds, prices, legacy_assets)
        if inferred is not None:
            with _PORTFOLIO_LOCK:
                latest = _load_config()
                latest_holds = [dict(item, code=str(item.get("code") or "").strip().zfill(6))
                                for item in (latest.get("holdings") or [])]
                latest_portfolio = latest.get("portfolio") or {}
                if latest_portfolio.get("cash_balance") is None:
                    latest_prices = {
                        item["code"]: (snapshot.loc[item["code"]]["price"]
                                       if snapshot is not None and not snapshot.empty
                                       and item["code"] in snapshot.index else None)
                        for item in latest_holds
                    }
                    inferred = infer_legacy_cash_balance(
                        latest_holds, latest_prices, latest_portfolio.get("total_assets"))
                    if inferred is not None:
                        latest_portfolio["cash_balance"] = round(inferred, 2)
                        latest_portfolio["cash_balance_estimated"] = True
                        latest["portfolio"] = latest_portfolio
                        _save_config(latest)
                        cfg, holds, prices, portfolio = latest, latest_holds, latest_prices, latest_portfolio
                        cash = portfolio["cash_balance"]
                        legacy_assets = portfolio.get("total_assets")
                        cash_estimated = True
                else:
                    # 用户在迁移等待期间完成了现金校准，必须采用最新的已核对现金。
                    cfg = latest
                    holds = latest_holds
                    portfolio = latest_portfolio
                    cash = portfolio.get("cash_balance")
                    legacy_assets = portfolio.get("total_assets")
                    cash_estimated = bool(portfolio.get("cash_balance_estimated"))
    valuation = calculate_portfolio(
        holds, prices, cash, legacy_total_assets=legacy_assets,
        cash_estimated=cash_estimated)
    return cfg, holds, prices, valuation


def get_snapshot():
    return cached("snap", 20, _fetch_snapshot)


def _fetch_flow():
    try:
        return pd.read_sql("SELECT * FROM quant_flow_realtime_today", con=qdb.get_engine())
    except Exception as error:  # noqa: BLE001
        print(f"[cache] 实时资金查询失败: {error}", flush=True)
        return pd.DataFrame()


def get_flow():
    return cached("flow", 60, _fetch_flow)


def _fetch_heat():
    try:
        return sec_mod.ths_index_heat()
    except Exception:  # noqa: BLE001
        return {}


def get_heat():
    return cached("heat", 120, _fetch_heat)


def _fetch_paper():
    try:
        # 直接读取明细表。旧代码依赖 quant_paper_today 视图，但该视图并未在
        # 当前数据库创建，导致模拟盘整段数据被异常吞掉，页面看不到追踪字段。
        from quant.paper import ensure_table
        ensure_table()
        return pd.read_sql(
            "SELECT * FROM quant_paper_trades "
            "ORDER BY push_date DESC, id DESC LIMIT 20",
            con=qdb.get_engine())
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


def _paper_slot_label(value):
    """将数据库时段枚举转换为页面可读文本。"""
    return {
        "auction": "竞价",
        "auction_back": "竞价备选",
        "tail": "尾盘",
    }.get(str(value or "").strip(), str(value or "—"))


def _paper_result_label(value):
    """将 T+1 胜负标记转换为可读结果，未到期不伪造结论。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "待更新"
    try:
        return "胜" if int(float(value)) == 1 else "负"
    except (TypeError, ValueError):
        return "待更新"


def _paper_row_result(rec_price, win_t1):
    """整行结果文案：无推荐价的是备选记录，不计胜负。"""
    if rec_price is None or (isinstance(rec_price, float) and pd.isna(rec_price)):
        return "备选(未买入)"
    return _paper_result_label(win_t1)


def _paper_t1_summary(paper):
    """返回近 20 条记录的 T+1 胜率，未完成记录不计入分母。"""
    if paper.empty or "win_t1" not in paper:
        return "T+1 胜率：暂无已完成记录"
    values = pd.to_numeric(paper["win_t1"], errors="coerce").dropna()
    if values.empty:
        return "T+1 胜率：暂无已完成记录（等待 T+1 收盘数据）"
    return (f"T+1 胜率：{values.mean() * 100:.1f}%（已完成 {len(values)} 条；"
            "未到 T+1 的记录不计入）")


def get_paper():
    return cached("paper", 120, _fetch_paper)


def fmt(v, f="{:.2f}", sign=True):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    try:
        return ("{:+.2f}" if sign and abs(float(v)) < 1e6 else f).format(float(v))
    except (ValueError, TypeError):
        return str(v)


def build_cards(engine, _snap):
    import yaml
    with open(os.path.join(ROOT, "config", "config.yaml"), encoding="utf-8") as f:
        holds = (yaml.safe_load(f) or {}).get("holdings") or []
    flow = get_flow()
    ftab = flow.set_index("code") if not flow.empty else None
    cards = []
    for h in holds:
        code = h["code"]
        sp = _snap.loc[code] if code in _snap.index else None
        frow = ftab.loc[code] if (ftab is not None and code in ftab.index) else None
        st = None
        try:
            st = sec_mod.stock_sector_now(engine, code)
        except Exception:  # noqa: BLE001
            st = None
        extra = ""
        if frow is not None and pd.notna(frow.get("main_net")):
            extra += (f"<div>主力实时: {fmt(float(frow['main_net']) / 1e8)} 亿"
                      f"({html.escape(str(frow.get('main_caliber') or ''))})</div>")
        if st and st.get("source") == "THS指数实时":
            extra += (f"<div>板块: {html.escape(st['name'])} {fmt(st['pct'])}%"
                      f"(排{st['rank']}/{st['n']})</div>")
        try:
            r = hd.analyze(h)
            act, why, levels = hd.decide(r)
            cost, shares = h.get("cost"), h.get("shares")
            if sp is not None and cost:
                pl = (float(sp["price"]) / float(cost) - 1) * 100
                plcls = "up" if pl > 0 else ("down" if pl < 0 else "p")
                pos = f"<div>成本 <b>{cost}</b> × {shares or '?'} 股 → "
                pos += (f"浮盈 <b class={plcls}>{fmt(pl)}%</b>"
                        f"(约 {fmt((float(sp['price']) - float(cost)) * (shares or 0) / 1e4, sign=False)} 万"
                        f")</div>") if shares else (f"浮盈 <b class={plcls}>{fmt(pl)}%</b></div>")
                extra += pos
            extra += f"<div>建议: <b>{html.escape(str(act))}</b> — {html.escape(str(why))[:60]}</div>"
            price, pct = (sp["price"], sp["pct"]) if sp is not None else (None, None)
            cards.append(f"<div class=card><b>{html.escape(h['name'])}({code})</b> "
                         f"{fmt(price)} {fmt(pct)}%{extra}"
                         f"<div class=small>关键位: {html.escape(str(levels))[:60]}</div></div>")
        except Exception as e:  # noqa: BLE001
            cards.append(f"<div class=card><b>{code}</b> 分析异常 {html.escape(str(e))[:40]}</div>")
    return cards


class RefreshData(tornado.web.RequestHandler):
    def post(self):
        with _CACHE_LOCK:
            if _REFRESH_ALL["running"]:
                self.set_header("Content-Type", "application/json; charset=utf-8")
                self.write(json.dumps({"ok": True, "message": "刷新已在进行中"}, ensure_ascii=False))
                return
            _REFRESH_ALL["running"] = True

        def refresh_all():
            try:
                jobs = (
                    ("scan", lambda: scan(top_n=40, return_stats=True)),
                    ("snap", _fetch_snapshot),
                    ("flow", _fetch_flow),
                    ("heat", _fetch_heat),
                    ("paper", _fetch_paper),
                )
                for key, fn in jobs:
                    with _CACHE_LOCK:
                        if key in _CACHE_REFRESHING:
                            continue
                        _CACHE_REFRESHING.add(key)
                    _refresh_cache(key, fn)
            finally:
                with _CACHE_LOCK:
                    _CACHE.pop("cards", None)
                    _REFRESH_ALL["running"] = False

        try:
            _CACHE_EXECUTOR.submit(refresh_all)
        except RuntimeError:
            with _CACHE_LOCK:
                _REFRESH_ALL["running"] = False
        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.write(json.dumps({"ok": True, "message": "已开始后台刷新，请稍后查看"}, ensure_ascii=False))


class RefreshStatus(tornado.web.RequestHandler):
    def get(self):
        with _CACHE_LOCK:
            state = {
                "running": bool(_REFRESH_ALL["running"] or _CACHE_REFRESHING),
                "refreshing": sorted(_CACHE_REFRESHING),
            }
        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.write(json.dumps(state, ensure_ascii=False))


class Main(tornado.web.RequestHandler):
    def get(self):
        engine = qdb.get_engine()
        empty = (pd.DataFrame(), {})
        df, stats = cached_stale(
            "scan", 600, lambda: scan(top_n=40, return_stats=True), empty)
        empty_snap = pd.DataFrame(columns=["price", "pct"]).rename_axis("code")
        _snap = cached_stale("snap", 20, _fetch_snapshot, empty_snap)
        flow = cached_stale("flow", 60, _fetch_flow, pd.DataFrame())
        heat = cached_stale("heat", 120, _fetch_heat, {})
        paper = cached_stale("paper", 120, _fetch_paper, pd.DataFrame())
        now = dt.datetime.now()
        S = [f"<h1>量化实时看板</h1><div class=muted>生成 {now:%H:%M:%S}(数据后台刷新;"
             f"推荐因子=收盘口径,价格/资金/板块=实时) <button type=button class=refresh-btn onclick=refreshDashboard()>刷新数据</button><span id=refresh-status></span></div>",
             '<div class="analysis-home-search">'
             '<form action="/analyze" method="get">'
             '<input name="query" aria-label="股票代码或名称" placeholder="代码或名称，查看买点分析" '
             'style="width:235px;min-height:44px;padding:8px;background:#161b22;color:#d8dee9;'
             'border:1px solid #333;border-radius:4px">'
             '<button style="min-height:44px;padding:8px 10px;margin-left:6px;background:#2e3440;'
             'color:#88c0d0;border:1px solid #4c566a;border-radius:4px">分析买点</button>'
             '</form>'
             '<a href="/learn" style="margin-left:14px">K线学堂(0-1)</a></div>']
        S.append("<h2>① S 级推荐(按推荐分,含实时价)</h2>")
        if not df.empty:
            sdf = df[df["grade"] == "S"].head(15)
            rows = []
            for _, r in sdf.iterrows():
                code = r["code"]
                sp = _snap.loc[code] if code in _snap.index else None
                price, pct = (sp["price"], sp["pct"]) if sp is not None else (None, None)
                pcls = "up" if (pct or 0) > 0 else ("down" if (pct or 0) < 0 else "p")
                rows.append(
                    f"<tr><td>{code}</td><td>{html.escape(str(r['name']))}</td>"
                    f"<td><b>{r['score10']}</b></td><td>{int(r['score'])}/8</td>"
                    f"<td class=n>{fmt(price, sign=False)}</td>"
                    f"<td class={pcls}>{fmt(pct)}%</td>"
                    f"<td>{html.escape(str(r['horizon']))}</td>"
                    f"<td class=small>{html.escape(str(r['evidence'])[:68])}</td></tr>")
            S.append("<table><tr><th>代码</th><th>名称</th><th>推荐分</th><th>共振</th>"
                     "<th>实时价</th><th>涨跌%</th><th>线</th><th>选定依据</th></tr>"
                     + "".join(rows) + "</table>")
            S.append(f"<div class=muted>全貌:命中 {stats.get('total', 0)} 只 — "
                     f"{stats.get('by_grade', {})}</div>")
        else:
            S.append("<div>扫描数据正在后台加载，稍后点击「刷新数据」查看。</div>")

        S.append("<h2>② 持仓实时监控</h2>")
        S.extend(cached_stale(
            "cards", 90, lambda: build_cards(engine, _snap),
            ["<div class=muted>持仓分析正在后台更新…</div>"]))

        S.append("<h2>③ 今日荐股(竞价/尾盘推送)</h2>")
        if not paper.empty:
            today = dt.date.today().isoformat()
            tp = paper[pd.to_datetime(paper["push_date"]).dt.strftime("%Y-%m-%d") == today]
            rows = [f"<tr><td>{r['push_date']}</td><td>{_paper_slot_label(r['push_slot'])}</td>"
                    f"<td>{r['code']}</td><td>{html.escape(str(r['name']))}</td>"
                    f"<td><b>{fmt(r['score10'], sign=False)}</b></td>"
                    f"<td class=n>{fmt(r['rec_price'], sign=False)}</td>"
                    f"<td class=small>{html.escape(str(r['evidence'])[:60])}</td></tr>"
                    for _, r in tp.iterrows()]
            S.append("<table><tr><th>日期</th><th>时段</th><th>代码</th><th>名称</th>"
                     "<th>推荐分</th><th>推荐价</th><th>依据</th></tr>" + "".join(rows) + "</table>")
        S.append("<h2>④ 模拟盘记录(近 20 条)</h2>")
        if not paper.empty:
            rows = [f"<tr><td>{r['push_date']}</td><td>{_paper_slot_label(r['push_slot'])}</td>"
                    f"<td>{r['code']}</td><td>{html.escape(str(r['name']))}</td>"
                    f"<td class=n>{fmt(r['rec_price'], sign=False)}</td>"
                    f"<td class=n>{fmt(r['close_t1'], sign=False)}</td>"
                    f"<td>{_paper_row_result(r['rec_price'], r['win_t1'])}</td></tr>"
                    for _, r in paper.iterrows()]
            S.append("<table><tr><th>日期</th><th>时段</th><th>代码</th><th>名称</th>"
                      "<th>推荐价</th><th>T+1收</th><th>胜负</th></tr>" + "".join(rows) + "</table>")
            S.append(f"<div class=muted>{_paper_t1_summary(paper)}</div>")
        else:
            S.append("<div class=muted>暂无模拟盘记录；数据表尚未创建或暂时不可用。</div>")

        S.append("<h2>⑤ THS 行业指数实时榜</h2>")
        if heat:
            top = sorted(heat.values(), key=lambda x: -x["pct"])[:10]
            bot = sorted(heat.values(), key=lambda x: x["pct"])[:5]
            fmt_sector = lambda item: f"{html.escape(item['name'])} {item['pct']:+.2f}%"
            S.append(f"<div>强: {' | '.join(fmt_sector(item) for item in top)}</div>")
            S.append(f"<div class=muted>弱: {' | '.join(fmt_sector(item) for item in bot)}</div>")

        S.append("<h2>⑥ 即时资金净额榜(THS 全口径)</h2>")
        if not flow.empty:
            topin = flow.nlargest(8, "net")
            topout = flow.nsmallest(8, "net")
            cell = lambda row: f"{html.escape(str(row['name']))} {fmt(float(row['net']) / 1e8)}亿"
            S.append(f"<div>流入: {' | '.join(cell(row) for _, row in topin.iterrows())}</div>")
            S.append(f"<div class=muted>流出: {' | '.join(cell(row) for _, row in topout.iterrows())}</div>")
        S.append("<div class=muted style=margin-top:14px>规则化信号,不构成投资建议;"
                 "每条结论的判定阈值见晚间复盘报告。</div>")
        self.write(_wrap("\n".join(S)))

def build_stock_page(code):
    """构建个股页面:当前报价、资金口径、板块和简明建议。"""
    import pymysql
    from quant.candle_dict import CANDLE_DICT, hit_summary

    code = str(code).zfill(6)
    conn = pymysql.connect(host="localhost", user="root", password="root",
                           database="instockdb", connect_timeout=5, read_timeout=8,
                           write_timeout=5)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name, industry FROM cn_stock_spot WHERE code=%s", (code,))
        row = cur.fetchone()
        name, industry = (row[0], row[1]) if row else (code, "")
        cur.execute("SELECT * FROM cn_stock_pattern WHERE code=%s AND date=%s",
                    (code, dt.date.today().isoformat()))
        prow = cur.fetchone()
        pcols = [d[0] for d in cur.description] if cur.description else []
        hits = [k for k, v in zip(pcols, prow or ())
                if v not in (0, None, "") and k not in ("date", "code", "name")]
        cur.execute("SELECT `date`,open,close,high,low,volume,amount,quote_change "
                    "FROM quant_hist_daily WHERE code=%s ORDER BY `date` DESC LIMIT 8", (code,))
        hist_rows = cur.fetchall()[::-1]
    finally:
        conn.close()

    quote = ((ths.prices_snapshot([ths.to_thscode(code)]) or {}).get("item") or [{}])[0]
    price, pct = quote.get("last_price"), quote.get("price_change_ratio_pct")
    o, h, low, prev = (quote.get("open_price"), quote.get("high_price"),
                       quote.get("low_price"), quote.get("prev_price"))
    amount_yi = (quote.get("turnover") or 0) / 1e8
    cls = "up" if (pct or 0) > 0 else ("down" if (pct or 0) < 0 else "p")
    change = (float(price) - float(prev)) if price is not None and prev else None
    parts = [
        f"<div class=quote><div class=q-name>{html.escape(str(name))} "
        f"<span class=q-code>{code}</span><span class=pill>{html.escape(str(industry or ''))}</span></div>"
        f"<div class=q-price><span class='q-big {cls}'>{fmt(price, sign=False)}</span>"
        f"<span class='q-chg {cls}'>{fmt(pct)}%</span><span class='q-chg {cls}'>{fmt(change)}</span></div>"
        f"<div class=q-stats><span>今开<b>{fmt(o, sign=False)}</b></span>"
        f"<span>最高<b>{fmt(h, sign=False)}</b></span><span>最低<b>{fmt(low, sign=False)}</b></span>"
        f"<span>昨收<b>{fmt(prev, sign=False)}</b></span><span>成交额<b>{fmt(amount_yi, sign=False)} 亿</b></span></div>"
        f"<div class=muted style=margin-top:8px>行情更新时间 {dt.datetime.now():%H:%M:%S} · 价格来自 THS 实时快照</div></div>"
    ]
    parts.append("<h2>① 资金(双口径)</h2>")
    try:
        from quant.datasource.fundflow_realtime import instant_of, main_flow_today
        instant = instant_of(code)
        main = main_flow_today(code)
        if instant:
            parts.append(f"<div>THS 全口径: 今日净额 {fmt(instant['net']/1e8)} 亿 "
                         f"(流入 {instant['in_yi']:.2f} / 流出 {instant['out_yi']:.2f})</div>")
        if main:
            parts.append(f"<div>东财主力(大单口径): {fmt(main.get('main_net', 0)/1e8)} 亿 "
                         f"· 更新 {html.escape(str(main.get('last_time') or ''))}</div>")
        if not instant and not main:
            parts.append("<div class=muted>暂无可用的实时资金数据</div>")
    except Exception as e:  # noqa: BLE001
        parts.append(f"<div class=muted>资金获取失败: {html.escape(str(e))[:80]}</div>")

    parts.append("<h2>② 板块实时</h2>")
    try:
        state = sec_mod.stock_sector_now(qdb.get_engine(), code)
        if state:
            parts.append(f"<div>{html.escape(str(state.get('name') or ''))} "
                         f"{fmt(state.get('pct'))}% · {html.escape(str(state.get('source') or ''))}</div>")
        else:
            parts.append("<div class=muted>暂未匹配到所属板块</div>")
    except Exception as e:  # noqa: BLE001
        parts.append(f"<div class=muted>板块查询失败: {html.escape(str(e))[:80]}</div>")

    parts.append("<h2>③ 今日K线形态(白话解释)</h2>")
    summary = hit_summary(hits)
    if summary:
        parts.extend(f"<div>{html.escape(item)}</div>" for item in summary)
    else:
        parts.append("<div class=muted>今日无已记录形态触发</div>")

    parts.append("<h2>④ 近期日K(最近 8 日)</h2>")
    if hist_rows:
        parts.append("<table><tr><th>日期</th><th>开盘</th><th>收盘</th><th>最高</th><th>最低</th>"
                     "<th>成交量</th><th>涨跌幅</th></tr>" + "".join(
            f"<tr><td>{r[0]}</td><td>{fmt(r[1],sign=False)}</td><td>{fmt(r[2],sign=False)}</td>"
            f"<td>{fmt(r[3],sign=False)}</td><td>{fmt(r[4],sign=False)}</td>"
            f"<td>{fmt(r[5],sign=False)}</td><td>{fmt(r[7])}%</td></tr>" for r in hist_rows) + "</table>")
    else:
        parts.append("<div class=muted>暂无本地日K数据</div>")

    parts.append("<h2>⑤ 操作建议(规则化)</h2>")
    try:
        import holding_decision as decision
        import yaml
        cfg_path = os.path.join(ROOT, "config", "config.yaml")
        with open(cfg_path, encoding="utf-8") as cfg_file:
            holds = (yaml.safe_load(cfg_file) or {}).get("holdings") or []
        holding = next((item for item in holds if str(item.get("code", "")).zfill(6) == code), None)
        if holding:
            result = decision.analyze(holding)
            if "error" not in result:
                action, reason, levels = decision.decide(result)
                parts.append(f"<div><b>{html.escape(action)}</b></div>"
                             f"<div>依据: {html.escape(reason)}</div>"
                             f"<div>关键位: {html.escape(levels)}</div>")
                if holding.get("cost"):
                    cost = float(holding["cost"])
                    pl_pct = (result["now"] / cost - 1) * 100
                    parts.append(f"<div>持仓成本 {cost:.3f} · 浮盈亏 {pl_pct:+.2f}%</div>")
            else:
                parts.append(f"<div class=muted>{html.escape(str(result['error']))}</div>")
        else:
            parts.append("<div class=muted>非当前持仓：不生成持仓操作建议。</div>")
    except Exception as e:  # noqa: BLE001
        parts.append(f"<div class=muted>规则建议暂不可用: {html.escape(str(e))[:80]}</div>")

    parts.append("<h2>⑥ 个股新闻(标题关键词粗标记，仅供参考)</h2>")
    try:
        from quant.datasource.news import stock_news
        news_items = stock_news(code, limit=5)
        if news_items:
            parts.append("<table><tr><th>时间</th><th>标签</th><th>标题/摘要</th><th>来源</th></tr>" + "".join(
                f"<tr><td>{html.escape(item.get('time',''))}</td>"
                f"<td>{html.escape(item.get('tag','中性'))}</td>"
                f"<td>{html.escape(item.get('title',''))}<div class=small>{html.escape(item.get('digest',''))}</div></td>"
                f"<td>{html.escape(item.get('source',''))}</td></tr>" for item in news_items) + "</table>")
        else:
            parts.append("<div class=muted>暂未获取到个股新闻；消息获取失败不代表没有公告。</div>")
    except Exception as e:  # noqa: BLE001
        parts.append(f"<div class=muted>消息获取失败: {html.escape(str(e))[:80]}</div>")

    parts.append("<h2>⑦ 风险提示</h2><div>页面展示行情与规则化指标，不构成投资建议；新闻标签是标题关键词粗标，须核对原文。</div>")
    parts.append(f"<div class=muted><a href='/positions'>持仓管理</a> · "
                 f"<a href='/learn'>K线学堂</a> · <a href='/'>返回总览</a></div>")
    return "\n".join(parts)


class StockPage(tornado.web.RequestHandler):
    def get(self):
        code = str(self.get_argument("code", "002878")).zfill(6)
        body = cached_stale(
            f"stock:{code}", 60, lambda: build_stock_page(code),
            "<h2>个股数据正在后台加载，请稍后刷新。</h2>")
        self.write(_wrap(body))

# ---------------- 持仓管理 + 交易纪律(用户输入:总资产/成本/股数/买入时间) ----------------

def _load_config():
    import yaml
    with open(os.path.join(ROOT, "config", "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_config(cfg):
    import yaml
    with open(os.path.join(ROOT, "config", "config.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


class PositionsData(tornado.web.RequestHandler):
    def get(self):
        empty = pd.DataFrame(columns=["price", "pct"]).rename_axis("code")
        snapshot = cached_stale("snap", 20, _fetch_snapshot, empty)
        _, holds, _, valuation = _portfolio_context(snapshot=snapshot)
        total_assets = valuation["total_assets"]
        from quant.portfolio import _number

        quote_at = "—"
        with _CACHE_LOCK:
            snap_entry = _CACHE.get("snap")
            if snap_entry:
                quote_at = dt.datetime.fromtimestamp(snap_entry[0]).strftime("%H:%M:%S")

        positions = {}
        for holding in holds:
            code = holding["code"]
            item = valuation["positions"].get(code, {})
            market_value = item.get("market_value")
            profit_pct = item.get("profit_pct")
            ratio = (market_value / total_assets * 100
                     if market_value is not None and total_assets else None)
            positions[code] = {
                "price": fmt(item.get("price"), sign=False) if item.get("price") is not None else "—",
                "market_value": fmt(market_value, sign=False) if market_value is not None else "—",
                "profit_pct": f"{fmt(profit_pct)}%" if profit_pct is not None else "—",
                "position_ratio": f"{fmt(ratio, sign=False)}%" if ratio is not None else "—",
            }
        position_ratio = (sum(_number(item.get("market_value")) or 0
                              for item in valuation["positions"].values())
                          / total_assets * 100 if total_assets else None)
        if valuation["cash_source"] == "estimated_from_legacy_total_assets":
            note = ("现金由旧版总资产一次性估算；请按券商余额核对并校准。"
                    if valuation["complete"] and valuation["cash_known"] else
                    "估值数据不完整；旧版总资产仅用于推算现金，请按券商余额核对。")
        elif valuation["complete"] and valuation["cash_known"]:
            note = "账户总资产=可用现金+实时持仓市值；未计手续费、税费。成交后请按券商余额更新现金。"
        else:
            missing = "、".join(valuation["missing_codes"]) or "未录入现金余额"
            note = f"估值数据不完整：{missing}。请确认现金余额及持仓报价/股数。"
        self.set_header("Cache-Control", "no-store")
        self.write({
            "ok": True,
            "total_assets": fmt(total_assets, sign=False) if total_assets is not None else "数据不完整",
            "market_value": fmt(valuation["market_value"], sign=False)
                            if valuation["market_value"] is not None else "数据不完整",
            "cash_balance": fmt(valuation["cash_balance"], sign=False)
                            if valuation["cash_known"] else "待校准",
            "unrealized_pnl": fmt(valuation["unrealized_pnl"])
                               if valuation["unrealized_pnl"] is not None else "成本/行情不完整",
            "position_ratio": f"{fmt(position_ratio, sign=False)}%" if position_ratio is not None else "—",
            "note": note,
            "quote_at": quote_at,
            "positions": positions,
        })


_SUGGEST_SCRIPT = '''<script>
(function(){
  var input=document.getElementById("new-code");
  var list=document.getElementById("new-code-list");
  var nameInput=document.querySelector("input[name=new_name]");
  if(!input||!list)return;
  var timer=null,items=[];
  function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){
    return "&#"+c.charCodeAt(0)+";";});}
  function hide(){list.style.display="none";}
  function render(rows){
    items=rows||[];
    if(!items.length){hide();return;}
    list.innerHTML=items.map(function(r,i){
      return "<div data-i="+i+" style='padding:7px 10px;cursor:pointer;display:flex;gap:10px;align-items:center'>"+
        "<b>"+esc(r.code)+"</b><span>"+esc(r.name)+"</span>"+
        "<span style='opacity:.55'>"+esc(r.industry)+"</span></div>";}).join("");
    list.style.display="block";}
  input.addEventListener("input",function(){
    clearTimeout(timer);
    var q=input.value.trim();
    if(!q){hide();return;}
    timer=setTimeout(function(){
      fetch("/stock/search?q="+encodeURIComponent(q),{cache:"no-store"})
        .then(function(r){return r.json();})
        .then(function(d){render(d.ok?d.items:[]);})
        .catch(function(){hide();});},200);});
  list.addEventListener("mousedown",function(e){
    var row=e.target.closest("[data-i]");
    if(!row)return;
    var it=items[+row.getAttribute("data-i")];
    if(!it)return;
    input.value=it.code;
    if(nameInput)nameInput.value=it.name;
    hide();e.preventDefault();});
  input.addEventListener("blur",function(){setTimeout(hide,180);});
  input.addEventListener("keydown",function(e){if(e.key==="Escape")hide();});
})();
</script>'''


def build_positions_page():
    """持仓管理页:实时资产估值、持仓编辑、止损止盈与纪律面板。"""
    import quant.rules as rules
    empty_snap = pd.DataFrame(columns=["price", "pct"]).rename_axis("code")
    snapshot = cached_stale("snap", 20, _fetch_snapshot, empty_snap)
    cfg, holds, prices, valuation = _portfolio_context(snapshot=snapshot)
    cash_balance = valuation["cash_balance"]
    total_assets = valuation["total_assets"]
    engine = qdb.get_engine()

    # 市场状态(简单:上证 vs MA20)
    mstate = "中性"
    try:
        from quant.datasource.index import fetch_index_daily
        sh = fetch_index_daily("sh000001", start="20260101")
        if len(sh) >= 20:
            closes = [x["close"] for x in sh]
            mstate = "偏多" if closes[-1] >= sum(closes[-20:]) / 20 else "防守"
    except Exception:  # noqa: BLE001
        pass

    pf_assets = total_assets if valuation["complete"] and valuation["cash_known"] else None
    pf_findings, ratio = rules.assess_portfolio(holds, prices, pf_assets, mstate)

    # ---- 账户资产概览 ----
    assets_display = fmt(total_assets, sign=False) if total_assets is not None else "数据不完整"
    cash_display = fmt(cash_balance, sign=False) if valuation["cash_known"] else "待校准"
    mv_display = (fmt(valuation["market_value"], sign=False)
                  if valuation["market_value"] is not None else
                  f"≥{fmt(valuation['market_value_partial'], sign=False)}")
    pnl_display = fmt(valuation["unrealized_pnl"]) if valuation["unrealized_pnl"] is not None else "成本/行情不完整"
    missing_display = "、".join(valuation["missing_codes"]) or "无"
    if valuation["cash_source"] == "estimated_from_legacy_total_assets":
        cash_help = "可用现金由旧版总资产减当前持仓市值估算；请核对券商余额并校准一次。"
    elif valuation["cash_source"] == "configured":
        cash_help = "行情变化会自动重估总资产；买入/卖出成交后请按券商可用资金重新校准。"
    else:
        cash_help = "请输入券商账户可用现金以建立资产基准；无需手动填写总资产。"
    asset_note = ("账户总资产=可用现金+实时持仓市值；未计手续费和税费。 " + cash_help
                  if valuation["complete"] and valuation["cash_known"] else
                  f"总资产暂不完整：缺报价或股数的代码（{missing_display}）。 " + cash_help)
    assets_html = (
        "<section class='asset-overview' aria-label='账户资产估值'>"
        f"<div class='asset-primary'><span>账户总资产</span><strong id='asset-total'>{assets_display}</strong><small>元 · 实时估算</small></div>"
        f"<div><span>持仓市值</span><strong id='asset-market-value'>{mv_display}</strong><small>元</small></div>"
        f"<div><span>可用现金</span><strong id='asset-cash'>{cash_display}</strong><small>元</small></div>"
        f"<div><span>持仓浮盈亏</span><strong id='asset-pnl'>{pnl_display}</strong><small>元 · 不含手续费</small></div>"
        f"<p id='asset-note' class='muted'>{html.escape(asset_note)}</p></section>")
    broker_snapshot = (cfg.get("portfolio") or {}).get("broker_snapshot") or {}
    broker_labels = (
        ("total_assets", "券商总资产快照"),
        ("total_market_value", "券商总市值"),
        ("total_profit", "券商当前总盈亏"),
        ("today_profit", "券商当日盈亏"),
        ("today_profit_pct", "券商当日涨跌幅"),
        ("available_funds", "券商可用资金"),
        ("withdrawable_funds", "券商可取资金"),
    )
    broker_items = [(key, label, broker_snapshot.get(key))
                    for key, label in broker_labels
                    if broker_snapshot.get(key) is not None]
    broker_html = ""
    if broker_items:
        captured_at = html.escape(str(broker_snapshot.get("captured_at") or "时间未知"))
        screen_time = html.escape(str(broker_snapshot.get("screenshot_time") or ""))
        stamp = f"识别于 {captured_at}" + (f" · 截图时间 {screen_time}" if screen_time else "")
        reconciliation = None
        if all(broker_snapshot.get(key) is not None for key in
               ("total_assets", "total_market_value", "available_funds")):
            reconciliation = (broker_snapshot["total_assets"]
                              - broker_snapshot["total_market_value"]
                              - broker_snapshot["available_funds"])
        reconciliation_html = (
            f"<p class='broker-reconciliation'>检测到券商对账差额：总资产 − 截图总市值 − 可用资金 = "
            f"<b>{fmt(reconciliation)}</b> 元。不自动调平，也不推测差额来源，请核对账户口径。</p>"
            if reconciliation is not None else "")
        broker_html = (
            "<section class='broker-snapshot' aria-label='券商截图账户摘要'>"
            f"<div class='broker-snapshot-title'><b>券商截图核对</b><span>{stamp} · 静态快照</span></div>"
            + "".join(
                f"<div class='broker-snapshot-value'><span>{label}</span>"
                f"<strong>{fmt(value, sign=key in ('total_profit', 'today_profit', 'today_profit_pct'))}{'%' if key == 'today_profit_pct' else ''}</strong>"
                f"<small>{' ' if key == 'today_profit_pct' else '元'}</small></div>"
                for key, label, value in broker_items)
            + reconciliation_html
            + "<p>券商截图中的总资产、盈亏及资金数据是静态对账快照，不随行情变化；"
              "上方账户总资产仍按可用现金 + 实时持仓市值计算。</p></section>")
    with _CACHE_LOCK:
        snap_entry = _CACHE.get("snap")
        quote_at = dt.datetime.fromtimestamp(snap_entry[0]).strftime("%H:%M:%S") if snap_entry else "—"

    # ---- 持仓实时估值表 ----
    rows = []
    for h in holds:
        code = h["code"]
        p = prices.get(code)
        cost = rules._f(h.get("cost"))
        pl = (p / cost - 1) * 100 if (p and cost) else None
        plcls = "up" if (pl or 0) > 0 else ("down" if (pl or 0) < 0 else "")
        pos = valuation["positions"].get(code, {})
        market_value_text = fmt(pos.get("market_value"), sign=False)
        pct_pos = (f"{pos['market_value'] / total_assets * 100:.1f}%"
                   if pos.get("market_value") is not None and total_assets else "—")
        st = None
        try:
            st = sec_mod.stock_sector_now(engine, code)
        except Exception:  # noqa: BLE001
            pass
        sec_txt = (f"{st['name']} {st['pct']:+.1f}%"
                   if st and st.get("source") == "THS指数实时" else "—")
        rows.append(
            f"<tr data-holding-row='{code}'><td>{code}</td>"
            f"<td>{html.escape(str(h.get('name','')))}</td>"
            f"<td>{fmt(cost, sign=False)}</td><td>{fmt(pos.get('shares'), sign=False)}</td>"
            f"<td>{html.escape(str(h.get('buy_date') or '—'))}</td>"
            f"<td class=n data-price='{code}'>{fmt(p, sign=False)}</td>"
            f"<td class=small data-market-value='{code}'>{market_value_text}</td>"
            f"<td class='{plcls}' data-profit='{code}'>{fmt(pl)}%</td>"
            f"<td data-position-ratio='{code}'>{pct_pos}</td><td class=small>{sec_txt}</td></tr>")

    # ---- 每股止损/止盈具体数字 ----
    stock_cards = []
    for h in holds:
        code = str(h.get("code") or "").zfill(6)
        p = prices.get(code)
        _cost = rules._f(h.get("cost"))
        pl = (p / _cost - 1) * 100 if (p and _cost) else None
        ma20 = None
        try:
            import pandas as _pd
            mdf = _pd.read_sql("SELECT close FROM quant_hist_daily WHERE code=%s "
                               "ORDER BY `date` DESC LIMIT 20", params=(code,), con=engine)
            if len(mdf) >= 20:
                ma20 = float(mdf["close"].mean())
        except Exception:  # noqa: BLE001
            pass
        lv = rules.stop_take_prices(h, ma20)
        plcls = "up" if (pl or 0) > 0 else ("down" if (pl or 0) < 0 else "p")
        pos = valuation["positions"].get(code, {})
        pct_pos = (f"{pos['market_value'] / total_assets * 100:.1f}%"
                   if pos.get("market_value") is not None and total_assets else "—")
        st = None
        try:
            st = sec_mod.stock_sector_now(engine, code)
        except Exception:  # noqa: BLE001
            st = None
        sec_txt = (f"{st['name']} {st['pct']:+.1f}%"
                   if st and st.get("source") == "THS指数实时" else "—")
        lv_txt = "—(未填成本)"
        if lv:
            lv_txt = (f"<div>🛑 止损 <b class=down>{lv['stop']}</b>"
                      f"<span class=small>({lv['stop_src']};成本-7%线 {lv['stop_cost']})</span>"
                      f" ｜ 🎯 止盈:减半线 <b>{lv['tp_half']}</b>(+10%) · "
                      f"保护线 <b>{lv['tp_protect']}</b>(+20%)</div>")
        stock_cards.append(
            f"<div class=card data-stock-card='{code}'><b>{html.escape(str(h.get('name','')))}({code})</b> "
            f"<span class=n data-card-price='{code}'>{fmt(p, sign=False)}</span> "
            f"<span class={plcls} data-card-profit='{code}'>{fmt(pl)}%</span>"
            f"<span class=small> 成本 {h.get('cost') or '—'}"
            f"{(' × ' + str(h.get('shares')) + '股') if h.get('shares') else ''}"
            f" · 仓位 <span data-card-ratio='{code}'>{pct_pos}</span>"
            f" · 买入 {h.get('buy_date') or '—'} · 板块 {sec_txt}</span>"
            f"{lv_txt}</div>")

    # ---- 龙头战法 ----
    ld_html = "<div class=muted>龙头战法数据获取失败(稍后重试)</div>"
    try:
        from quant.strategies.leader import leader_scan
        leader_result = leader_scan()
        if leader_result and leader_result.get("leader"):
            leader = leader_result["leader"]
            ladder = " · ".join(
                f"{item['name']} {item['boards']}板({item['state'][:2]})"
                for item in leader_result["ladder"][:8]) or "无连板梯队"
            ld_html = (
                f"<div>今日涨停 <b>{leader_result['stats']['up_cnt']}</b> 家 · 炸板 {leader_result['stats']['break_cnt']} 家"
                f" ｜ 主线题材 <b>{html.escape(str(leader_result['theme']))}</b>"
                f"(最高 {leader_result['height']} 板,{leader_result['count']} 家)</div>"
                f"<div>龙头:<b>{html.escape(leader['name'])}({leader['code']})</b> "
                f"{leader['boards']} 连板 · {leader['state']} · 封单 {leader['seal_yi']} 亿 · "
                f"{html.escape(leader['limit_time'])}</div>"
                f"<div class=small>梯队:{html.escape(ladder)}</div>" +
                "".join(f"<div class=small>• {html.escape(hint)}</div>"
                        for hint in leader_result["hints"]))
        elif leader_result:
            ld_html = (f"<div class=muted>今日涨停 {leader_result['stats']['up_cnt']} 家,"
                       "暂无连板龙头(不硬凑)</div>")
    except Exception:  # noqa: BLE001
        pass

    # ---- 组合风控与纪律面板 ----

    pend = rules.pending_discipline(engine)
    pend_rows = []
    for _, item in pend.iterrows():
        level = "DISCIPLINE" if item["level"] == "DISCIPLINE" else "WARN"
        pend_rows.append(
            f"<div class='kl-card {'' if level == 'WARN' else 'hit'}'>"
            f"<span class='pill {'res' if level == 'DISCIPLINE' else ''}'>{level}</span> "
            f"<b>{html.escape(str(item['name'] or item['code']))}</b> · {html.escape(str(item['rule']))} · {item['date']}"
            f"<div class=desc>{html.escape(str(item['message'] or ''))}</div>"
            f"<a class=small href='/positions/resolve?code={item['code']}&rule={item['rule']}'>✔ 标记已执行</a></div>")
    if not pend_rows:
        pend_rows.append("<div class=muted>当前无未执行纪律 ✓</div>")

    pf_txt = " | ".join(message for _, _, message in pf_findings) if pf_findings else "—"
    pf_cls = "down" if any(finding[0] == "DISCIPLINE" for finding in pf_findings) else ""
    cash_input = html.escape(str(cash_balance if cash_balance is not None else ""), quote=True)
    edit_rows = []
    for h in holds:
        code = h["code"]
        edit_rows.append(
            f"<tr><td><input name=code_{code} value='{code}' readonly class=ro></td>"
            f"<td>{html.escape(str(h.get('name','')))}</td>"
            f"<td><input name=cost_{code} value='{html.escape(str(h.get('cost') or ''), quote=True)}'></td>"
            f"<td><input name=shares_{code} value='{html.escape(str(h.get('shares') or ''), quote=True)}'></td>"
            f"<td><input name=buy_{code} value='{html.escape(str(h.get('buy_date') or ''), quote=True)}' placeholder='2026-10-15'></td>"
            f"<td><label class=small><input type=checkbox name=del_{code}>删除</label></td></tr>")

    common_script = (
        "<script>(function(){let busy=false;const set=(id,value)=>{const node=document.getElementById(id);"
        "if(node)node.textContent=value??'—';};async function refresh(){if(busy||document.visibilityState==='hidden')return;"
        "busy=true;try{const response=await fetch('/positions/data',{cache:'no-store'});"
        "if(response.ok){const data=await response.json();if(data.ok){"
        "set('asset-total',data.total_assets);set('asset-market-value',data.market_value);"
        "set('asset-cash',data.cash_balance);set('asset-pnl',data.unrealized_pnl);"
        "set('asset-position-ratio',data.position_ratio);set('asset-note',data.note);"
        "set('quote-updated','行情采样 '+data.quote_at+' · 每20秒自动更新');"
        "for(const [code,pos] of Object.entries(data.positions||{})){"
        "for(const [selector,value] of [['[data-price=\"'+code+'\"]',pos.price],"
        "['[data-market-value=\"'+code+'\"]',pos.market_value],"
        "['[data-profit=\"'+code+'\"]',pos.profit_pct],"
        "['[data-position-ratio=\"'+code+'\"]',pos.position_ratio],"
        "['[data-card-price=\"'+code+'\"]',pos.price],"
        "['[data-card-profit=\"'+code+'\"]',pos.profit_pct],"
        "['[data-card-ratio=\"'+code+'\"]',pos.position_ratio]]){"
        "const node=document.querySelector(selector);if(node)node.textContent=value??'—';}}}}}"
        "catch(error){}finally{busy=false;}}"
        "window.__posRefresh=refresh;"
        "if(window.__posTimer)clearInterval(window.__posTimer);window.__posTimer=setInterval(refresh,20000);refresh();"
        "if(!window.__posVisBound){window.__posVisBound=true;"
        "document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'&&window.__posRefresh)window.__posRefresh();});}"
        "function applyBody(formIdx,wasOpen,actName,scrollY,html,msg){"
        "const wrap=document.querySelector('.wrap');"
        "if(!wrap||!html||html.indexOf('data-pos-'+'loading')>=0){window.location.reload();return;}"
        "wrap.innerHTML=html;"
        "wrap.querySelectorAll('script').forEach(old=>{const s=document.createElement('script');"
        "s.textContent=old.textContent;old.parentNode.replaceChild(s,old);});"
        "const dl=document.querySelector('details');if(dl)dl.open=wasOpen;"
        "if(actName){const el=document.querySelector('[name=\"'+actName+'\"]');if(el)el.focus();}"
        "window.scrollTo(0,scrollY);"
        "const forms=document.querySelectorAll('form[data-auto-save]');const f=forms[formIdx];"
        "if(f){const st=f.querySelector('[data-save-status]');if(st){st.textContent=msg||'已保存 ✓';"
        "setTimeout(()=>{if(st.textContent.indexOf('已保存')===0)st.textContent='';},msg?5000:1500);}}}"
        "document.querySelectorAll('form[data-auto-save]').forEach(form=>form.addEventListener('submit',async event=>{"
        "event.preventDefault();const button=form.querySelector('button[type=submit]');"
        "const status=form.querySelector('[data-save-status]');"
        "const idx=[].indexOf.call(document.querySelectorAll('form[data-auto-save]'),form);"
        "const dl=document.querySelector('details');const wasOpen=!!(dl&&dl.open);"
        "const act=document.activeElement;const actName=(act&&act.name)?act.name:null;"
        "const y=window.scrollY;"
        "if(button)button.disabled=true;"
        "if(status)status.textContent='正在保存…';try{const response=await fetch(form.action,{method:'POST',"
        "body:new FormData(form),headers:{'Accept':'application/json'}});const result=await response.json();"
        "if(!response.ok||!result.ok)throw new Error(result.error||'保存失败');"
        "if(status)status.textContent=result.message||'已保存 ✓';"
        "if(window.__posRefresh)window.__posRefresh();"
        "let tries=0;(function pull(){setTimeout(async()=>{"
        "try{const r=await fetch('/positions?partial=1',{cache:'no-store'});"
        "const t=await r.text();"
        "if(t&&t.indexOf('data-pos-'+'loading')<0){"
        "applyBody(idx,wasOpen,actName,y,t,result.message);return;}}"
        "catch(e){}"
        "if(++tries<8)pull();else window.location.reload();},1200);})();"
        "}catch(error){if(status)status.textContent=error.message||'保存失败，请重试';if(button)button.disabled=false;}}));"
        "})();</script>")

    body = (
        "<h2>账户资产实时估值</h2>" + assets_html + broker_html +
        f"<div class=muted id='quote-updated'>行情采样 {quote_at} · 每20秒自动更新</div>"
        "<h2>全局交易系统</h2>"
        "<form action='/positions/save' method='post' data-auto-save class='cash-form'>"
        "<label for='cash-balance'>可用现金(元)</label> "
        f"<input id='cash-balance' name='cash_balance' type='number' min='0' step='0.01' value='{cash_input}' placeholder='券商可用现金' required>"
        "<button type='submit' style='margin-left:10px'>校准现金</button> "
        f"<span class=small>大盘状态 <b>{mstate}</b> · 当前总仓位 <b id='asset-position-ratio'>{(fmt(ratio*100, sign=False)+'%') if ratio is not None else '—'}</b></span>"
        "<div data-save-status class=small aria-live=polite>旧总资产只用于初次估算现金；成交后校准现金即可。</div>"
        f"<div class={pf_cls}>组合纪律:{html.escape(pf_txt)}</div>"
        f"<div class=small>规则:单票 ≤{int(rules.MAX_POS_SINGLE*100)}% · 总仓 ≤{int(rules.MAX_POS_TOTAL_NORMAL*100)}%(防守 {int(rules.MAX_POS_TOTAL_DEFENSE*100)}%)"
        f" · 硬止损 成本-{int(rules.STOP_LOSS_PCT*100)}% · 止盈 +{int(rules.TP_HALF*100)}%减半/+{int(rules.TP_PROTECT*100)}%保护"
        f" · 浮亏>{int(rules.ADD_LOSS_BAN*100)}%禁加仓 · 当日≤{rules.CHG_DROP_TODAY}%禁买</div></form>"
        "<h2>持仓明细与实时市值</h2>"
        "<table><tr><th>代码</th><th>名称</th><th>成本</th><th>股数</th><th>买入日期</th><th>现价</th>"
        "<th>市值(元)</th><th>浮盈亏%</th><th>仓位</th><th>板块</th></tr>"
        + "".join(rows) + "</table>"
        "<details><summary style='cursor:pointer;color:var(--accent)'>✎ 编辑持仓 / 新增(点开)</summary>"
        "<form action='/positions/save' method='post' data-auto-save>"
        "<table><tr><th>代码</th><th>名称</th><th>成本</th><th>股数</th><th>买入日期</th><th>删?</th></tr>"
        + "".join(edit_rows) +
        "<tr><td style='position:relative'>"
        "<input name=new_code id=new-code placeholder='代码或名称,输入即过滤' autocomplete=off>"
        "<div id=new-code-list style='display:none;position:absolute;z-index:40;left:0;top:100%;"
        "min-width:300px;max-height:260px;overflow:auto;background:#141b26;"
        "border:1px solid #3a4b63;border-radius:6px;box-shadow:0 6px 18px rgba(0,0,0,.45)'></div></td>"
        "<td><input name=new_name placeholder='名称(点选自动带出)'></td>"
        "<td><input name=new_cost placeholder='成本'></td><td><input name=new_shares placeholder='股数'></td>"
        "<td><input name=new_buy placeholder='买入日期'></td><td class=small>1手=100股</td></tr></table>"
        "<div style='margin:8px 0'><button type='submit'>保存持仓修改</button> "
        "<span data-save-status class=small aria-live=polite></span></div></form></details>" +
        "<h2>逐股止损 / 止盈(具体价位)</h2>" + "".join(stock_cards) +
        "<div class=muted>止损=成本-7% 与 MA20 取更近者(先到先执行);止盈为分批纪律价。</div>"
        "<h2>龙头战法(连板梯队)</h2>" + ld_html +
        "<div class=muted>战法框架为公开心法规则化转译(连板梯队/辨识度/弱转强/分歧回封)，高波动打法，严格执行破5日线减仓/破10日线清仓。</div>"
        "<h2>纪律对账(未执行项反复提醒)</h2>" + "".join(pend_rows) +
        "<h2>📷 上传券商持仓截图识别</h2>"
        "<div class=muted style='margin-bottom:10px'>上传同花顺或券商 App 持仓截图，系统 OCR 识别后展示预览；核对并确认后才写入。</div>"
        "<form action='/positions/upload_img' method='post' enctype='multipart/form-data'>"
        "<div style='display:flex;align-items:center;gap:12px;flex-wrap:wrap'>"
        "<input type=file name=img accept='image/*' style='flex:1;min-width:200px'>"
        "<button type=submit>识别并预览</button></div></form>"
        "<div class=muted style='margin-top:6px'>支持 PNG / JPG / HEIC；识别后需人工确认才会保存。</div>"
        "<div class=muted>DISCIPLINE 级每轮推送重复出现，直到你在本页标记已执行。</div>" +
        common_script + _SUGGEST_SCRIPT)
    return body


_POSITIONS_LOADING_BODY = (
    "<h2 data-pos-loading>持仓数据正在后台加载，请稍后刷新。</h2>"
    "<script>let tries=0;const timer=setInterval(async()=>{try{"
    "const r=await fetch('/positions?partial=1',{cache:'no-store'});"
    "if(r.ok){const t=await r.text();"
    "if(t&&!t.includes('data-pos-'+'loading')){clearInterval(timer);"
    "const wrap=document.querySelector('.wrap');"
    "if(!wrap){window.location.reload();return;}"
    "wrap.innerHTML=t;"
    "wrap.querySelectorAll('script').forEach(o=>{const s=document.createElement('script');"
    "s.textContent=o.textContent;o.parentNode.replaceChild(s,o);});}}"
    "}catch(e){}if(++tries>=30)clearInterval(timer);},2000);</script>")


class PositionsPage(tornado.web.RequestHandler):
    def get(self):
        # loading 判据用哨兵属性 data-pos-loading:脚本里的判断串拆开拼接
        # ('data-pos-'+'loading'),防止页面自带的脚本文本自我命中(完整页面被
        # 误判成 loading,热更永远走降级刷新——2026-10-21 踩过的坑)。
        body = cached_stale("positions", 60, build_positions_page,
                            _POSITIONS_LOADING_BODY)
        if "data-pos-loading" in body:
            self.set_header("Retry-After", "2")
        if self.get_argument("partial", ""):
            self.write(body)          # 裸 body:前端保存后/轮询热更原位注入
            return
        self.write(_wrap(body))


def _resolve_new_holding(new_code, new_name):
    """新增持仓行解析:六位代码直存(目录带出名称);否则按名称/前缀在目录里唯一解析。

    返回 (code, name, message):解析成功 message 为空;多匹配给「请下拉点选」提示;
    目录不可用时降级为旧规则(仅六位代码可存),不阻塞保存其他行。
    """
    import re

    from quant.analysis import search_directory
    new_code = (new_code or "").strip()
    new_name = (new_name or "").strip()
    if re.fullmatch(r"\d{6}", new_code):
        try:
            rows = search_directory(new_code, 1)
        except Exception as error:  # noqa: BLE001
            print(f"[positions] 目录查询失败(代码直存): {type(error).__name__}", flush=True)
            rows = []
        for row in rows:
            if row["code"] == new_code:
                return new_code, new_name or row["name"], ""
        return new_code, new_name or new_code, ""      # 目录未收录仍允许保存
    key = new_code or new_name
    if not key:
        return None, None, ""
    try:
        rows = search_directory(key, 10)
    except Exception as error:  # noqa: BLE001
        print(f"[positions] 目录查询失败(名称解析放弃): {type(error).__name__}", flush=True)
        return None, None, "新增行未保存：股票目录暂不可用，请直接输入六位代码。"
    if len(rows) == 1:
        return rows[0]["code"], rows[0]["name"], ""
    exact = [row for row in rows if row["name"] == key]
    if len(exact) == 1:
        return exact[0]["code"], exact[0]["name"], ""
    return (None, None,
            f"新增行未保存：「{key}」匹配到多只候选，请在输入框下拉里点选一只后再保存。")


class PositionsSave(tornado.web.RequestHandler):
    def post(self):
        import math
        import re

        args = {}
        for key, values in self.request.arguments.items():
            value = values[-1]
            args[key] = value.decode("utf-8") if isinstance(value, bytes) else value
        wants_json = "application/json" in self.request.headers.get("Accept", "")

        numeric_fields = {}
        for key, value in args.items():
            if key not in ("new_cost", "new_shares") and not key.startswith(("cost_", "shares_")):
                continue
            raw = (value or "").strip()
            if not raw:
                numeric_fields[key] = None
                continue
            try:
                number = float(raw)
                if not math.isfinite(number) or number < 0:
                    raise ValueError
            except (TypeError, ValueError):
                self.set_status(400)
                self.write({"ok": False, "error": "成本和股数必须是大于或等于 0 的有效数字。"})
                return
            numeric_fields[key] = number

        try:
            with _PORTFOLIO_LOCK:
                cfg = _load_config()
                portfolio = cfg.get("portfolio") or {}
                raw_cash = args.get("cash_balance")
                if raw_cash not in (None, ""):
                    try:
                        cash_balance = float(raw_cash)
                        if not math.isfinite(cash_balance) or cash_balance < 0:
                            raise ValueError
                    except (TypeError, ValueError):
                        self.set_status(400)
                        self.write({"ok": False, "error": "现金余额必须是大于或等于 0 的有效数字。"})
                        return
                    portfolio["cash_balance"] = round(cash_balance, 2)
                    portfolio["cash_balance_estimated"] = False
                    cfg["portfolio"] = portfolio

                keeps, codes = [], set()
                for holding in (cfg.get("holdings") or []):
                    code = str(holding.get("code") or "").strip().zfill(6)
                    if not code or code == "000000" or args.get(f"del_{code}"):
                        continue
                    for arg, key in ((f"cost_{code}", "cost"),
                                     (f"shares_{code}", "shares"),
                                     (f"buy_{code}", "buy_date")):
                        if arg in args:
                            holding[key] = (numeric_fields[arg] if arg in numeric_fields
                                            else args[arg] or None)
                    holding["code"] = code
                    keeps.append(holding)
                    codes.add(code)

                new_code = (args.get("new_code") or "").strip()
                new_name = (args.get("new_name") or "").strip()
                note = ""
                if new_code or new_name:
                    code, resolved_name, note = _resolve_new_holding(new_code, new_name)
                    if code and code not in codes:
                        keeps.append({
                            "code": code,
                            "name": resolved_name,
                            "cost": numeric_fields.get("new_cost", args.get("new_cost") or None),
                            "shares": numeric_fields.get("new_shares", args.get("new_shares") or None),
                            "buy_date": args.get("new_buy") or None,
                        })
                    elif code:
                        note = f"新增行未保存：{code} 已在持仓中。"
                cfg["holdings"] = keeps
                _save_config(cfg)
        except (OSError, ValueError) as error:
            self.set_status(500)
            self.write({"ok": False, "error": f"配置保存失败：{type(error).__name__}"})
            return

        invalidate_cache("cards", "positions", "main")
        # 热更新:缓存刚失效,触发后台重建(毫秒级返回;盘中整页构建约 2.4s
        # 不放请求线程)。前端保存成功后 1.2 秒拉 /positions?partial=1 原位
        # 注入新页,拉不到(后台刷新一直失败)重试 8 次后自动降级整页刷新。
        cached_stale("positions", 60, build_positions_page, "")
        if wants_json:
            self.set_header("Cache-Control", "no-store")
            self.write({"ok": True, "redirect": "/positions", "message": note})
        else:
            self.redirect("/positions")


def _live_hist(code):
    """实时决策用:只读本地 quant_hist_daily 最近 90 日 K(不在盘中写入)。"""
    conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT `date`,close,high,low,volume FROM quant_hist_daily "
                        "WHERE code=%s ORDER BY `date` DESC LIMIT 90", (code,))
            return [dict(date=str(r[0]), close=r[1], high=r[2],
                         low=r[3], volume=r[4]) for r in cur.fetchall()]
    finally:
        conn.close()


def _sector_stats():
    """320 个 THS 板块的当日聚合;两表 collation 不同,在 pandas 侧合并。"""
    def load():
        from sqlalchemy import text
        with qdb.get_engine().connect() as conn:
            spot = pd.read_sql(text(
                "SELECT code,name,change_rate,deal_amount FROM cn_stock_spot "
                "WHERE `date`=(SELECT MAX(`date`) FROM cn_stock_spot)"), conn)
            mem = pd.read_sql(text(
                "SELECT code,index_code,index_name FROM quant_ths_membership "
                "WHERE updated=(SELECT MAX(updated) FROM quant_ths_membership)"), conn)
        if spot.empty or mem.empty:
            return pd.DataFrame()
        merged = mem.merge(spot.assign(code=spot["code"].astype(str)),
                           on="code", how="inner")
        return (merged.groupby(["index_code", "index_name"])
                .agg(avg_pct=("change_rate", "mean"),
                     up_ratio=("change_rate", lambda s: float((s > 0).mean())),
                     amount=("deal_amount", "sum"),
                     n=("code", "size")).reset_index())
    return cached_stale("sector_stats", 300, load, pd.DataFrame())


def _index_cycle():
    """大盘周期(上证日 K 近 60 日,缓存 10 分钟)。

    THS/东财间歇限流时空结果按失败处理(抛出异常),让缓存层 10 秒后
    重试,而不是把 None 当成功值缓存 600 秒;页脚如实标注不可用。
    """
    def load():
        import quant.selection.live as live_mod

        from quant.datasource.index import fetch_index_daily
        start = (dt.date.today() - dt.timedelta(days=60)).strftime("%Y%m%d")
        rows = fetch_index_daily("sh000001", start=start) or []
        cycle = live_mod.market_cycle(rows)
        if not cycle:
            raise RuntimeError("指数日K获取失败或不足15根")
        return cycle
    return cached_stale("index_cycle", 600, load, None)


def _announce_audit(code):
    """巨潮公告原文核验(每股缓存 1 小时;冷缓存/临时失败返回核验中)。

    未收录(可能已退市)是终态,按同样 1 小时缓存,不无限重试;
    网络临时异常走 cached_stale 的 10 秒重试。核验结果含标题与原文
    PDF 链接,blocked(停牌/退市/立案等)由调用方剔除,warning 标注。
    """
    from quant.datasource import announce as ann

    def load():
        fetched = ann.fetch_announcements(code)
        if not fetched.get("ok"):
            return {"status": "unknown", "hits": [], "n": 0, "ok": True,
                    "items": [],
                    "summary": fetched.get("error") or "巨潮未收录该代码",
                    "source": fetched.get("source") or "巨潮资讯网"}
        assessed = ann.assess_announcements(fetched["items"])
        assessed.update({"ok": True, "source": fetched["source"],
                         "items": fetched["items"][:12]})
        return assessed

    return cached_stale(f"announce:{str(code).zfill(6)}", 3600, load,
                       {"status": "pending", "hits": [], "n": 0,
                        "summary": "巨潮公告核验中(稍候自动更新)"})


def _hot_sector_rows(stats, spot_all, sentiment, cycle, held, top_n=2):
    """当日热度 top 板块的优质股(战法选股第三路信号,复用板块筛选链路)。

    行情基础为当日喂表,候选用一次 THS 批量快照校准实时价;与 /sector/pick
    同口径(情绪门控 + 周期剔除 + 优质分),每板块最多取 4 只。
    """
    import quant.selection.live as live_mod

    rows = []
    if stats is None or not len(stats) or spot_all is None or not len(spot_all):
        return rows
    hot = (stats[(stats["n"] >= 10) & (stats["avg_pct"].astype(float) > 0)]
           .sort_values("avg_pct", ascending=False).head(top_n))
    if not len(hot):
        return rows
    conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(hot))
            cur.execute(
                "SELECT code,index_code FROM quant_ths_membership "
                "WHERE updated=(SELECT MAX(updated) FROM quant_ths_membership) "
                f"AND index_code IN ({placeholders})",
                tuple(hot["index_code"].tolist()))
            codes_df = pd.DataFrame(cur.fetchall(), columns=["code", "index_code"])
    finally:
        conn.close()
    if not len(codes_df):
        return rows
    # 成分表无股票名,与当日行情在 pandas 侧合并(避开 collation 差异)。
    members = codes_df.assign(
        code=codes_df["code"].astype(str).str.zfill(6)).merge(
        spot_all[["code", "name"]].assign(
            code=spot_all["code"].astype(str).str.zfill(6)),
        on="code", how="inner")
    for _, st in hot.iterrows():
        m = members[members["index_code"] == st["index_code"]]
        if not len(m):
            continue
        cands, gate_note = live_mod.sector_prefilter(m, spot_all, sentiment, held)
        if not len(cands):
            continue
        snaps = {}
        try:
            ths_codes = [ths.to_thscode(str(c)) for c in cands["code"].astype(str)]
            resp = ths.prices_snapshot(ths_codes) or {}
            for item in (resp.get("item") or resp.get("data") or []):
                code = str(item.get("thscode") or item.get("code") or "").split(".")[0]
                if code:
                    snaps[code.zfill(6)] = item
        except Exception:  # noqa: BLE001
            snaps = {}
        rank = int((stats["avg_pct"].astype(float)
                    > float(st["avg_pct"])).sum()) + 1
        sector_stat = {"names": str(st["index_name"]),
                       "avg_pct": round(float(st["avg_pct"]), 2),
                       "up_ratio": float(st["up_ratio"]), "rank": rank,
                       "total": int(len(stats))}
        picked, meta = live_mod.sector_pick(
            cands, snaps, sentiment, cycle, sector_stat, _live_hist, limit=4)
        rows.extend(picked)
    return rows


def _audit_rows(rows):
    """对候选行做巨潮公告核验:blocked(停牌/退市/立案)剔除,其余附状态。

    warning(减持/问询等)把提示追加进行依据原文;pending/pass 只标注。
    返回 (kept_rows, blocked_n);核验本身每股缓存 1 小时,失败不补造。
    """
    kept, blocked = [], 0
    for row in rows or []:
        audit = _announce_audit(str(row.get("code") or "").zfill(6))
        status = audit.get("status")
        if status == "blocked":
            blocked += 1
            continue
        row["announce"] = {
            "status": status,
            "summary": audit.get("summary") or "",
            "url": (audit.get("hits") or [{}])[0].get("url") or "",
        }
        if status == "warning":
            row["evidence"] = (str(row.get("evidence") or "") + "；巨潮公告提示:"
                               + str(audit.get("summary") or ""))[:200]
        kept.append(row)
    return kept, blocked


def _strategy_payload():
    """组装战法选股三路信号(M3 共振 / 龙头梯队 / 热度板块优质股)并算可信度。

    全部复用既有缓存与只读链路:scan("scan")、leader_scan("leader", 30 分钟)、
    当日喂表行情、本地 K 线;不发起全市场资金抓取,不做任何交易。
    """
    import quant.selection.live as live_mod
    import quant.strategies.pick as pick_mod
    from quant.strategies.leader import leader_scan

    try:
        holdings = (_load_config().get("holdings") or [])
    except Exception:  # noqa: BLE001
        holdings = []
    held = {str(h.get("code") or "").zfill(6) for h in holdings}

    empty_pair = (pd.DataFrame(), {})
    pair = cached_stale("scan", 600,
                        lambda: scan(top_n=40, return_stats=True), empty_pair)
    scan_df = pair[0] if isinstance(pair, tuple) else pair
    leader = cached_stale("leader", 1800, lambda: leader_scan() or {}, {})

    conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT code,name,change_rate,deal_amount,new_price,high_price "
                "FROM cn_stock_spot WHERE `date`=(SELECT MAX(`date`) FROM cn_stock_spot)")
            spot_all = pd.DataFrame(cur.fetchall(), columns=[
                "code", "name", "change_rate", "deal_amount",
                "new_price", "high_price"])
    finally:
        conn.close()

    sentiment = live_mod.market_sentiment(spot_all)
    cycle = _index_cycle()

    sector_rows = []
    try:
        sector_rows = _hot_sector_rows(
            _sector_stats(), spot_all, sentiment, cycle, held, top_n=3)
    except Exception as error:  # noqa: BLE001
        print(f"[strategies] 热度板块优质股加载失败: {error}", flush=True)

    # 两阶段:先无公告粗评取候选(15),再对入榜股票逐只巨潮核验(缓存 1h),
    # 最后带公告结果重算(停牌/立案剔除后仍能补足 limit)并重排序。
    rows, env_note = pick_mod.confidence_rows(
        scan_df, leader if leader else None, sector_rows, sentiment, cycle,
        held, limit=15)
    audit_codes = {r["code"] for r in rows}
    audit_codes.update(str(l["code"]).zfill(6) for l in
                       pick_mod.ladder_view(leader if leader else None))
    announces = {code: _announce_audit(code) for code in sorted(audit_codes)}
    rows, env_note = pick_mod.confidence_rows(
        scan_df, leader if leader else None, sector_rows, sentiment, cycle,
        held, limit=12, announces=announces)
    announce_pending = any(a.get("status") == "pending"
                           for a in announces.values())
    ladder_rows = pick_mod.ladder_view(leader if leader else None, announces)
    # 当日资金净额(THS 即时全单口径:流入−流出,含散单):payload 组装在
    # 后台线程,一次全市场拉取(缓存 3 分钟)服务全部候选与梯队;
    # 失败降级为 —,不影响主链。分析页/实时决策只读缓存不主动抓取。
    flow_net = {}
    try:
        from quant.datasource.fundflow_realtime import ths_instant_flow
        flow_df = ths_instant_flow()
        if flow_df is not None and not flow_df.empty:
            want = {str(r["code"]).zfill(6) for r in rows}
            want.update(str(l["code"]).zfill(6) for l in ladder_rows)
            hit = flow_df[flow_df["code"].isin(want)]
            flow_net = {str(r["code"]).zfill(6): float(r["net"])
                        for _, r in hit.iterrows()}
    except Exception as error:  # noqa: BLE001
        print(f"[strategies] 当日资金净额加载失败(降级为 —): "
              f"{type(error).__name__} {error}", flush=True)
    for r in rows:
        r["flow_net"] = flow_net.get(str(r["code"]).zfill(6))
    for l in ladder_rows:
        l["flow_net"] = flow_net.get(str(l["code"]).zfill(6))
    try:
        paper = cached_stale("paper", 120, _fetch_paper, pd.DataFrame())
    except Exception:  # noqa: BLE001
        paper = pd.DataFrame()
    scan_ready = bool(scan_df is not None and not scan_df.empty)
    leader_ready = bool(leader)
    method_stats = {}
    try:
        import quant.strategy_log as slog
        method_stats = slog.stats_summary()
    except Exception as error:  # noqa: BLE001
        print(f"[strategies] 战法可行性统计加载失败: {type(error).__name__} {error}",
              flush=True)
    return {
        # 两路全空说明选股因子/涨停池尚未就绪,由前端提示稍候,不误报"无标的"。
        "loading": not (scan_ready or leader_ready),
        "announce_pending": announce_pending,
        "sentiment": sentiment,
        "cycle": cycle,
        "env_note": env_note,
        "rows": rows,
        "ladder": ladder_rows,
        "theme": (leader or {}).get("theme"),
        "height": (leader or {}).get("height"),
        "count": (leader or {}).get("count"),
        "hints": (leader or {}).get("hints") or [],
        "paper_note": pick_mod.paper_win_note(paper),
        "method_stats": method_stats,
        "scan_ready": scan_ready,
        "leader_ready": leader_ready,
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


class SectorPick(tornado.web.RequestHandler):
    """按板块筛选优质股:当日情绪门控 + 大盘/个股周期判断 + 技术评估。

    行情基础:当日喂表(盘中约 15 分钟新鲜度,收盘后为全天值),最终候选
    再用一次 THS 批量快照校准实时价;技术指标只读本地 K 线。不发起全市场
    资金抓取,只做展示不自动买卖。
    """

    def get(self):
        import quant.selection.live as live_mod

        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.set_header("Cache-Control", "no-store")
        sector = self.get_argument("sector", "").strip()
        try:
            holdings = (_load_config().get("holdings") or [])
        except Exception:  # noqa: BLE001
            holdings = []
        held = {str(h.get("code") or "").zfill(6) for h in holdings}

        try:
            stats = _sector_stats()
        except Exception as error:  # noqa: BLE001
            self.write({"ok": False,
                        "error": f"板块统计异常：{type(error).__name__}"})
            return
        if stats is None or len(stats) == 0:
            self.write({"ok": False,
                        "error": "板块成分或当日行情未就绪，请稍后重试。"})
            return
        codes, names, suggest = live_mod.match_sectors(stats, sector)
        if not codes:
            self.write({"ok": False, "error": "未匹配到板块「" + sector
                        + "」。试试：" + "、".join(suggest)})
            return

        conn = qdb.get_connection()
        try:
            with conn.cursor() as cur:
                placeholders = ",".join(["%s"] * len(codes))
                cur.execute(
                    "SELECT code FROM quant_ths_membership "
                    "WHERE updated=(SELECT MAX(updated) FROM quant_ths_membership) "
                    f"AND index_code IN ({placeholders})", tuple(codes))
                codes_df = pd.DataFrame(cur.fetchall(), columns=["code"])
                cur.execute(
                    "SELECT code,name,change_rate,deal_amount,new_price,high_price "
                    "FROM cn_stock_spot "
                    "WHERE `date`=(SELECT MAX(`date`) FROM cn_stock_spot)")
                spot_all = pd.DataFrame(cur.fetchall(), columns=[
                    "code", "name", "change_rate", "deal_amount",
                    "new_price", "high_price"])
        finally:
            conn.close()
        # 成分表无股票名,与当日行情在 pandas 侧合并(避开 collation 差异)。
        if len(codes_df) and len(spot_all):
            members = codes_df.assign(
                code=codes_df["code"].astype(str).str.zfill(6)).merge(
                spot_all[["code", "name"]].assign(
                    code=spot_all["code"].astype(str).str.zfill(6)),
                on="code", how="inner")
        else:
            members = codes_df

        sentiment = live_mod.market_sentiment(spot_all)
        cycle = _index_cycle()
        hits = stats[stats["index_code"].isin(codes)]
        try:
            avg_pct = float((hits["avg_pct"] * hits["n"]).sum() / hits["n"].sum())
            up_ratio = float((hits["up_ratio"] * hits["n"]).sum() / hits["n"].sum())
        except (TypeError, ZeroDivisionError):
            avg_pct, up_ratio = 0.0, 0.0
        # 热度排名按全量 320 板块计算(命中组里最好板块的全局名次)。
        rank = int((stats["avg_pct"].astype(float)
                    > hits["avg_pct"].astype(float).max()).sum()) + 1
        sector_stat = {"names": "、".join(names), "avg_pct": round(avg_pct, 2),
                       "up_ratio": round(up_ratio, 3), "rank": rank,
                       "total": int(len(stats))}

        cands, gate_note = live_mod.sector_prefilter(
            members, spot_all, sentiment, held)
        note_extra = ""
        if not len(cands):
            note_extra = (f"板块当日普跌（上涨占比 {round(up_ratio * 100, 1)}%），"
                          "情绪门控后无温和上涨候选，不硬凑。")
        snaps = {}
        if len(cands):
            try:
                ths_codes = [ths.to_thscode(str(c)) for c in cands["code"].astype(str)]
                resp = ths.prices_snapshot(ths_codes) or {}
                for item in (resp.get("item") or resp.get("data") or []):
                    code = str(item.get("thscode") or item.get("code") or "").split(".")[0]
                    if code:
                        snaps[code.zfill(6)] = item
            except Exception:  # noqa: BLE001
                snaps = {}
        rows, meta = live_mod.sector_pick(
            cands, snaps, sentiment, cycle, sector_stat, _live_hist, limit=8)
        # 巨潮公告原文核验:停牌/退市/立案等剔除,减持/问询标注进依据。
        rows, ann_blocked = _audit_rows(rows)
        if ann_blocked:
            note_extra += (f" 巨潮公告核验剔除 {ann_blocked} 只"
                           "(停牌/退市/立案)。")

        if not len(cands) and not snaps:
            note_extra += " 实时快照不可用或无候选，暂无实时价校准。"
        elif len(cands) and not snaps:
            note_extra += " 实时快照不可用，现价为当日喂表价（约15分钟新鲜度）。"
        elif any(not r.get("realtime") for r in rows):
            note_extra += " 部分现价为当日喂表价（实时快照缺失个股）。"
        self.write({
            "ok": True,
            "sector": sector_stat,
            "sentiment": sentiment,
            "cycle": cycle,
            "gate_note": gate_note,
            "rows": rows,
            "meta": meta,
            "spot_note": note_extra.strip(),
            "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })


class RecommendLive(tornado.web.RequestHandler):
    """当前时段实时决策:主板可买候选(核心条件+入场/止损价)与持仓卖出信号。

    只做展示,不自动买卖;选股因子复用 "scan" 缓存(收盘口径),时段确认与
    核心技术评估用实时快照 + 本地 K 线(只读),不发起全市场资金抓取。
    """

    def get(self):
        import quant.selection.live as live_mod

        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.set_header("Cache-Control", "no-store")
        try:
            from instock.lib.trade_time import is_trade_date
            trading_day = bool(is_trade_date(dt.date.today()))
        except Exception:  # noqa: BLE001
            trading_day = dt.date.today().weekday() < 5
        slot = live_mod.current_slot()
        if not trading_day:
            slot = live_mod.closed_slot("今日非交易日：按最近快照复核，仅供下一交易时段参考。")

        try:
            holdings = (_load_config().get("holdings") or [])
        except Exception:  # noqa: BLE001
            holdings = []
        held = {str(h.get("code") or "").zfill(6) for h in holdings}

        buys, buys_loading, buy_note = [], False, ""
        try:
            empty = (pd.DataFrame(), {})
            pair = cached_stale("scan", 600,
                                lambda: scan(top_n=40, return_stats=True), empty)
            scan_df = pair[0] if isinstance(pair, tuple) else pair
            if scan_df is None or scan_df.empty:
                buys_loading = True
            else:
                mb = scan_df[scan_df["code"].astype(str).str.match(r"^(60|00)")].head(20)
                codes = [str(c).zfill(6) for c in mb["code"].tolist()]
                snaps = {}
                if codes:
                    ths_codes = [ths.to_thscode(c) for c in codes]
                    if slot["mode"] == "auction":
                        stage = "live" if (dt.datetime.now().hour,
                                           dt.datetime.now().minute) < (9, 26) else "final"
                        resp = ths.auction_snapshot(ths_codes, stage=stage) or {}
                    else:
                        resp = ths.prices_snapshot(ths_codes) or {}
                    for item in (resp.get("item") or resp.get("data") or []):
                        code = str(item.get("thscode") or item.get("code") or "").split(".")[0]
                        if code:
                            snaps[code.zfill(6)] = item
                if snaps:
                    buys, skipped = live_mod.assess_buy_candidates(
                        mb, snaps, held, slot["mode"], _live_hist, limit=5)
                    # 当日资金净额:只读战法页/调度已拉好的缓存
                    # (THS 全单口径),不在本请求发起全市场资金抓取。
                    try:
                        from quant.datasource.fundflow_realtime import (
                            cached_flow_of)
                        for b in buys:
                            b["flow_net"] = cached_flow_of(b.get("code"))
                    except Exception:  # noqa: BLE001
                        for b in buys:
                            b["flow_net"] = None
                    # 巨潮公告原文核验:停牌/退市/立案等剔除,减持/问询标注。
                    buys, ann_blocked = _audit_rows(buys)
                    if ann_blocked:
                        buy_note += (f" 巨潮公告核验剔除 {ann_blocked} 只"
                                     "(停牌/退市/立案),不硬凑。")
                    full = [b for b in buys if b["core_pass"] >= b["core_total"]]
                    if buys and not full:
                        buy_note = ("当前时段无核心条件全过(4/4)的标的；"
                                    "下列为通过大部分(3/4)的候选，未过项已标注，不硬凑。")
                    elif not buys:
                        buy_note = ("候选均未通过时段确认或核心条件不足 3/4"
                                    "（技术趋势/买入位置未过），不硬凑。")
                    if skipped:
                        buy_note += f" {skipped} 只因历史K不足跳过技术评估。"
                else:
                    buy_note = "实时快照不可用，暂无法做时段确认；请稍后重试。"
        except Exception as error:  # noqa: BLE001
            buy_note = f"选股或行情确认异常：{type(error).__name__}，请稍后重试。"

        sells, quiet, failed = live_mod.collect_sell_signals(
            holdings, hd.analyze, hd.decide)
        if not holdings:
            sell_note = "当前无录入持仓。"
        elif sells:
            sell_note = f"其余 {len(quiet)} 只持仓暂无卖出信号。"
        else:
            sell_note = "当前持仓均无卖出信号。"
        if failed:
            names = "、".join(f"{item['name']}({item['error']})" for item in failed[:4])
            sell_note += f" 行情缺失：{names}。"

        self.write({
            "ok": True,
            "slot": slot["slot"],
            "slot_label": slot["label"],
            "slot_note": slot["note"],
            "buys": buys,
            "buys_loading": buys_loading,
            "buy_note": buy_note,
            "sells": sells,
            "sell_note": sell_note,
            "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })


class StrategiesPage(tornado.web.RequestHandler):
    """战法选股页(壳):三路信号按可信度排序,前端拉取 /strategies/data 渲染。"""

    def get(self):
        from quant.analysis_view import strategies_shell

        self.set_header("Cache-Control", "no-store")
        self.write(_wrap(strategies_shell()))


class StrategiesData(tornado.web.RequestHandler):
    """战法选股数据:M3 共振 / 龙头梯队 / 板块优质三路信号 + 可信度评分。

    不做外层缓存:聚合本身只是内存计算 + 一次当日喂表查询,内层数据
    各自缓存(scan 600s / leader 1800s / sector_stats 300s / paper 120s),
    避免把"内层尚冷"的 loading 结果当成功值长期缓存。评分见
    quant/strategies/pick.py;只做展示,不构成投资建议。
    """

    def get(self):
        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.set_header("Cache-Control", "no-store")
        try:
            payload = _strategy_payload()
        except Exception as error:  # noqa: BLE001
            self.write({"ok": False,
                        "error": f"战法信号组装异常：{type(error).__name__}"})
            return
        if payload.get("loading"):
            self.write({"ok": True, "loading": True})
            return
        payload["ok"] = True
        self.write(payload)



class AnalyzeRefresh(tornado.web.RequestHandler):
    def post(self):
        from quant.analysis import fetch_analysis, resolve_stock

        code = self.get_body_argument("code", "")
        if len(code) != 6 or not code.isdigit():
            self.set_status(400)
            return
        try:
            stock, _, _ = resolve_stock(code)
        except Exception:
            self.set_status(503)
            return
        if not stock:
            self.set_status(404)
            return
        key = f"analysis:{code}"
        with _CACHE_LOCK:
            if key not in _CACHE_REFRESHING:
                _CACHE_REFRESHING.add(key)
                generation = _CACHE_GENERATION.get(key, 0)
                try:
                    _CACHE_EXECUTOR.submit(
                        _refresh_cache, key, lambda: fetch_analysis(code), generation)
                except RuntimeError:
                    _CACHE_REFRESHING.discard(key)
                    self.set_status(503)
                    return
        self.redirect(f"/analyze?query={code}")


class AnalyzeStatus(tornado.web.RequestHandler):
    def get(self):
        code = self.get_argument("code", "")
        if len(code) != 6 or not code.isdigit():
            self.set_status(400)
            self.write({"ready": False})
            return
        key = f"analysis:{code}"
        with _CACHE_LOCK:
            entry = _CACHE.get(key)
            age = time.time() - entry[0] if entry else None
            ready = bool(entry and age < 90 and key not in _CACHE_REFRESHING)
        self.set_header("Cache-Control", "no-store")
        self.write({"ready": ready})


class SimPage(tornado.web.RequestHandler):
    """模拟盘页:25,000 起始资金的自动模拟交易(纸面,不触真实账户)。

    目标线 30,000 是挑战线不是承诺线;净值/持仓/流水/纪律全部留痕,
    由 scheduler 尾盘 14:40 触发 sim_engine 执行,收盘链结算刷新。
    """

    def get(self):
        import quant.sim as sim

        self.set_header("Cache-Control", "no-store")
        account = sim.latest_account()
        if not account:
            self.write(_wrap("<h2>⚔ 模拟盘 · 25,000 → 30,000(挑战线)</h2>"
                             "<p class='analysis-hint'>模拟盘尚未初始化,首次运行 "
                             "scripts/sim_engine.py 后建立。</p>"))
            return
        holds = sim.positions()
        trades = sim.recent_trades(20)
        history = sim.net_history(30)
        prices = sim.snapshot_prices([h["code"] for h in holds])
        net = float(account["net"])
        start = float(account["start_cash"])
        target = float(account["target"])
        progress = max(0.0, min(1.0, (net - start) / max(target - start, 1e-9)))
        mv = sum(int(h["qty"]) * prices.get(h["code"], float(h["cost"]))
                 for h in holds)

        parts = ["<h2>⚔ 模拟盘 · 25,000 → 30,000(挑战线,非承诺)</h2>",
                 f"<p class='analysis-hint'>起始 {start:,.0f} · 目标线 "
                 f"{target:,.0f} · 截至 {html.escape(str(account['as_of']))}"
                 " —— 每天 14:40 尾盘按战法信号与纪律自动执行(纸面模拟,"
                 "不构成投资建议;+20%/周无法保证,净值如实呈现)。</p>",
                 f"<div class='analysis-verdict ready' style='display:block'>"
                 f"<div style='flex:1'><span class='analysis-eyebrow'>总净值</span>"
                 f"<strong>{net:,.2f}</strong>"
                 f"<p>现金 {float(account['cash']):,.2f} + 市值 {mv:,.2f}"
                 f" · 距目标 {(net / target - 1) * 100:+.2f}% 待达成</p></div>"
                 f"<div class='analysis-last'><span>目标进度</span>"
                 f"<strong>{progress * 100:.1f}%</strong>"
                 f"<span>{net - start:+,.2f} / {target - start:+,.0f}</span></div></div>",
                 f"<div style='background:#1b2a3a;border-radius:8px;height:14px;"
                 f"margin:10px 0;overflow:hidden'><div style='width:{progress * 100:.1f}%;"
                 f"height:100%;background:linear-gradient(90deg,#0a7d32,#37b24d)"
                 f"'></div></div>"]

        parts.append("<h2>当前持仓(T+1 当日不可卖)</h2>")
        if holds:
            rows = []
            for h in holds:
                price = prices.get(h["code"])
                last = price if price else float(h["cost"])
                pl = (last - float(h["cost"])) / float(h["cost"])
                price_html = (f"{last:.2f}" + ("" if price else "<br><span class=small>快照缺失,按成本</span>"))
                rows.append(
                    "<tr><td><a href='/analyze?query=" + h["code"] + "'>"
                    + html.escape(str(h.get("name") or h["code"])) + " "
                    + h["code"] + "</a></td>"
                    f"<td class=n>{int(h['qty'])}</td><td class=n>{float(h['cost']):.2f}</td>"
                    f"<td class=n>{price_html}</td>"
                    f"<td class=n>{pl:+.1%}</td><td class=n>{float(h['stop'] or 0):.2f}</td>"
                    f"<td class=small>{html.escape(str(h.get('buy_date')))}<br>"
                    f"{html.escape(str(h.get('method') or ''))[:40]}</td></tr>")
            parts.append("<table><tr><th>股票</th><th>数量</th><th>成本</th>"
                         "<th>现价</th><th>浮盈亏</th><th>纪律止损</th>"
                         "<th>买入日/战法</th></tr>" + "".join(rows) + "</table>")
        else:
            parts.append("<p class='analysis-hint'>当前空仓(纪律或无合格信号)。</p>")

        decisions = sim.recent_decisions(days=3, limit=80)
        parts.append("<h2>每日决策留痕 · 买入检查 / 持仓核对(含未成交原因)</h2>")
        if decisions:
            drows = []
            for d in decisions:
                status = str(d.get("status"))
                if status == "done":
                    badge = "<span class='pill res'>成交</span>"
                elif status == "hold":
                    badge = "<span class='pill'>持有·未卖</span>"
                else:
                    badge = "<span class='pill warn'>未买</span>"
                stock = ("整体" if str(d["code"]) == "-" else
                         "<a href='/analyze?query="
                         + html.escape(str(d["code"])) + "'>"
                         + html.escape(str(d.get("name") or d["code"])) + " "
                         + html.escape(str(d["code"])) + "</a>")
                price = (f"{float(d['price']):.2f}"
                         if d.get("price") is not None else "—")
                drows.append(
                    f"<tr><td class=small>{html.escape(str(d['trade_date']))}"
                    f"<br><span class=small>{html.escape(str(d['ts'])[-8:])}</span></td>"
                    "<td>" + ("买" if d["side"] == "buy" else "卖") + "<br>"
                    + badge + "</td>"
                    f"<td>{stock}</td><td class=n>{price}</td>"
                    f"<td class=small>{html.escape(str(d.get('detail') or ''))}</td></tr>")
            parts.append("<table><tr><th>日期/时间</th><th>方向·结果</th>"
                         "<th>股票</th><th>参考价</th>"
                         "<th>原因(为什么买/卖/没买卖)</th>"
                         + "".join(drows) + "</table>"
                         "<p class='analysis-hint'>每日 14:40 尾盘决策与 15:35 "
                         "收盘结算自动记录:触发买卖=成交留痕;候选被纪律拦下"
                         "=未买原因;持仓核对未触发=持有原因(止损未破/趋势完好/"
                         "浮盈未达止盈线)。</p>")
        else:
            parts.append("<p class='analysis-hint'>决策留痕自 2026-10-22 起;"
                         "此前仅有成交流水。为什么买、为什么卖、为什么没买卖,"
                         "此后每日逐条留痕。</p>")

        parts.append("<h2>交易流水(全部留痕)</h2>")
        if trades:
            rows = []
            for t in trades:
                color = "#c0392b" if t["action"] == "buy" else "#0a7d32"
                rows.append(
                    "<tr><td class=small>" + html.escape(str(t["ts"])) + "</td>"
                    "<td><a href='/analyze?query=" + t["code"] + "'>"
                    + html.escape(str(t.get("name") or t["code"])) + "</a></td>"
                    f"<td><b style='color:{color}'>{'买入' if t['action']=='buy' else '卖出'}</b></td>"
                    f"<td class=n>{float(t['price']):.2f}</td><td class=n>{int(t['qty'])}</td>"
                    f"<td class=n>{float(t['amount']):,.0f}</td>"
                    f"<td class=small>{html.escape(str(t.get('method') or '')[:30])}"
                    f" / 可信度 {html.escape(str(t.get('confidence')) or '—')}</td>"
                    f"<td class=small>{html.escape(str(t.get('reason') or ''))[:120]}</td></tr>")
            parts.append("<table><tr><th>时间</th><th>股票</th><th>动作</th>"
                         "<th>价格</th><th>数量</th><th>金额</th><th>战法/可信度</th>"
                         "<th>决策依据</th></tr>" + "".join(rows) + "</table>")
        else:
            parts.append("<p class='analysis-hint'>暂无交易。</p>")

        if history:
            rows = []
            lo = min(float(h["net"]) for h in history)
            hi = max(float(h["net"]) for h in history)
            span = max(hi - lo, 1e-9)
            for h in history:
                width = 10 + 90 * (float(h["net"]) - lo) / span
                rows.append(
                    f"<tr><td class=small>{html.escape(str(h['as_of']))}</td>"
                    f"<td style='min-width:320px'><div style='width:{width:.0f}%;"
                    "height:12px;background:#37b24d;border-radius:4px'></div></td>"
                    f"<td class=n>{float(h['net']):,.2f}</td>"
                    f"<td class=n>{float(h['cash']):,.0f}</td>"
                    f"<td class=n>{float(h['market_value']):,.0f}</td></tr>")
            parts.append("<h2>净值曲线(每日)</h2><table><tr><th>日期</th>"
                         "<th></th><th>净值</th><th>现金</th><th>市值</th></tr>"
                         + "".join(rows) + "</table>")

        parts.append("<h2>执行纪律(全部自动,不可人工干预)</h2>"
                     "<ul class='analysis-risks'>"
                     "<li>买入:战法选股可信度降序,≥50 开仓,单票 ≤40% 净值,"
                     "谨慎情绪(30-45)仓位减半,冰点(<30)不开新仓;主板 only,"
                     "涨停/贴近涨停买不进;单日最多 2 笔;成交按信号价 ±0.2% 滑点。</li>"
                     "<li>卖出:触及纪律止损或收盘破 MA20 清仓;浮盈 ≥10% 减半、"
                     "≥20% 清仓;T+1 当日买入不可卖。</li>"
                     "<li>每天 14:40(scheduler)执行,15:35 收盘链结算重算净值;"
                     "同日同股同动作幂等不重复成交。</li></ul>"
                     "<p class='analysis-hint'>纸面模拟,不触真实账户;"
                     "目标线为挑战目标,系统不承诺收益;历史信号样本胜率 25%"
                     "(4 条,样本不足),+20%/周需要行情配合。规则化信号不构成投资建议。</p>")
        self.write(_wrap("".join(parts)))


def analysis_section(query):
    """买点分析区公共渲染:/analyze 与 /trade(交易台合并页)共用。

    返回 {"body": HTML 片段(不含 _wrap 壳), "status": HTTP 状态码或 None}。
    """
    from quant.analysis import fetch_analysis, resolve_stock
    from quant.analysis_view import empty_page, render_analysis, sector_page

    query = (query or "").strip()
    if not query:
        return {"body": empty_page(), "status": None}
    try:
        stock, candidates, message = resolve_stock(query)
    except Exception as error:  # noqa: BLE001
        return {"body": empty_page(
            query, f"股票目录暂不可用：{type(error).__name__}，请稍后重试。"),
            "status": 503}
    if stock is None and not candidates:
        # 无股票匹配 → 尝试板块分流(命中即渲染板块页,加载后自动筛选优质股)。
        try:
            import quant.selection.live as live_mod

            codes, names, _ = live_mod.match_sectors(_sector_stats(), query)
        except Exception:  # noqa: BLE001
            codes, names = [], []
        if codes:
            return {"body": sector_page(query, names), "status": None}
    if stock is None:
        return {"body": empty_page(query, message, candidates), "status": None}

    code = str(stock["code"]).zfill(6)
    key = f"analysis:{code}"
    facts = cached_stale(key, 90, lambda: fetch_analysis(code), None)
    with _CACHE_LOCK:
        refreshing = key in _CACHE_REFRESHING
    if facts is None:
        body = empty_page(query, "正在获取行情、板块与消息；采集完成后页面会自动显示。")
        body += ("<script>let tries=0;const ticker=setInterval(async()=>{"
                 f"try{{const r=await fetch('/analyze/status?code={code}',{{cache:'no-store'}});"
                 "if((await r.json()).ready){clearInterval(ticker);location.reload();}}"
                 "catch(e){}if(++tries>=40)clearInterval(ticker);},1500);</script>")
        return {"body": body, "status": None}
    # 巨潮公告原文核验(每股缓存 1 小时):候选页核验过的股票这里直接命中。
    facts = dict(facts)
    facts["announcements"] = _announce_audit(code)

    try:
        cfg = _load_config()
        holding = next((item for item in cfg.get("holdings") or []
                        if str(item.get("code", "")).zfill(6) == code), None)
        portfolio = cfg.get("portfolio") or {}
        assets = None
        warning = ""
        try:
            cfg_ctx, _, _, valuation = _portfolio_context()
            assets = valuation["total_assets"]
            legacy = (cfg_ctx.get("portfolio") or {}).get("total_assets")
            if assets is None and (legacy is not None
                                   or portfolio.get("cash_balance") is not None):
                warning = "<p class='analysis-hint'>账户估值数据不完整，单票仓位上限未核验。</p>"
        except Exception:  # noqa: BLE001
            assets = None
        body = render_analysis(facts, holding=holding, total_assets=assets)
        if refreshing:
            body = ("<p class='analysis-hint'>正在后台重新采集；下方为上一次样本，请核对数据时间。</p>"
                    + body +
                    "<script>let tries=0;const ticker=setInterval(async()=>{"
                    f"try{{const r=await fetch('/analyze/status?code={code}',{{cache:'no-store'}});"
                    "if((await r.json()).ready){clearInterval(ticker);location.reload();}}"
                    "catch(e){}if(++tries>=40)clearInterval(ticker);},1500);</script>")
        return {"body": warning + body, "status": None}
    except Exception as error:  # noqa: BLE001
        return {"body": empty_page(
            query, f"分析暂不可用：{type(error).__name__}，请稍后重试。"), "status": 503}


class AnalyzePage(tornado.web.RequestHandler):
    """按代码或名称检索买点分析，后台采集时以轮询自动更新页面。"""

    def get(self):
        sec = analysis_section(self.get_argument("query", "").strip())
        if sec["status"]:
            self.set_status(sec["status"])
        self.write(_wrap(sec["body"]))


_TRADE_TAB_SCRIPT = '''<script>
(function(){
  const tabs=['analysis','positions'];
  const show=name=>{
    tabs.forEach(t=>{const s=document.getElementById('trade-tab-'+t);
      if(s)s.style.display=(t===name?'block':'none');});
    document.querySelectorAll('[data-trade-tab]').forEach(b=>{
      b.classList.toggle('active',b.getAttribute('data-trade-tab')===name);});};
  document.querySelectorAll('[data-trade-tab]').forEach(b=>b.addEventListener(
    'click',()=>{show(b.getAttribute('data-trade-tab'));
      location.hash=b.getAttribute('data-trade-tab');}));
  const init=(location.hash||'').replace('#','');
  show(tabs.indexOf(init)>=0?init:'analysis');
})();
</script>'''


_TRADE_POS_POLL = '''<script>
(function(){
  let tries=0;
  const timer=setInterval(async()=>{
    try{
      const box=document.getElementById('trade-tab-positions');
      if(!box||box.innerHTML.indexOf('正在后台加载')<0){clearInterval(timer);return;}
      const r=await fetch('/positions?partial=1',{cache:'no-store'});
      const t=await r.text();
      if(t&&t.indexOf('data-pos-'+'loading')<0){
        box.innerHTML=t;
        box.querySelectorAll('script').forEach(o=>{const s=document.createElement('script');
          s.textContent=o.textContent;o.parentNode.replaceChild(s,o);});
        clearInterval(timer);}
    }catch(e){}
    if(++tries>=30)clearInterval(timer);
  },2000);
})();
</script>'''


class TradePage(tornado.web.RequestHandler):
    """交易台:买点分析 + 持仓管理合并一页(双 tab,hash 记忆当前页签)。

    /analyze 与 /positions 保留独立入口(兼容直链与既有测试);导航栏只保留
    一个「交易台」链接。持仓区脚本(自动保存热更/20 秒行情/输入过滤)在
    合并页正常工作;分析区链接与表单已重定向到 /trade,切换不跳出本页;
    持仓区冷缓存时轮询 /positions?partial=1 只注入持仓 tab 容器
    (不替换 .wrap,不影响分析区)。
    """

    def get(self):
        self.set_header("Cache-Control", "no-store")   # 动态双 tab 页防缓存
        query = self.get_argument("query", "").strip()
        sec = analysis_section(query)
        body = sec["body"]
        # 搜索与跳转保持在合并页内(表单 action、股票链接、返回按钮)。
        body = (body.replace("action='/analyze'", "action='/trade'")
                   .replace('href="/analyze?query=', 'href="/trade?query=')
                   .replace("location.href='/analyze'", "location.href='/trade'"))
        pos = cached_stale("positions", 60, build_positions_page,
                           "<p class='analysis-hint'>持仓数据正在后台加载，"
                           "两秒内自动显示（最多约 1 分钟）。</p>")
        shell = (
            "<div class='seg' role='tablist' aria-label='交易台页签'>"
            "<button type=button data-trade-tab='analysis' class='active'>买点分析 · 代码 / 名称 / 板块</button>"
            "<button type=button data-trade-tab='positions'>持仓管理 · 纪律</button></div>"
            "<div id='trade-tab-analysis'>" + body + "</div>"
            "<div id='trade-tab-positions'>" + pos + "</div>"
            + _TRADE_TAB_SCRIPT + _TRADE_POS_POLL)
        if sec["status"]:
            self.set_status(sec["status"])
        self.write(_wrap(shell))


class StockSearch(tornado.web.RequestHandler):
    """持仓编辑输入框的候选过滤:/stock/search?q=海得 → 目录前缀/包含匹配前 10 条。"""

    def get(self):
        query = self.get_argument("q", "").strip()
        try:
            from quant.analysis import search_directory
            items = search_directory(query, limit=10) if query else []
        except Exception as error:  # noqa: BLE001
            self.set_status(503)
            self.write({"ok": False, "error": f"股票目录暂不可用：{type(error).__name__}"})
            return
        self.set_header("Cache-Control", "no-store")
        self.write({"ok": True, "items": items})


class PositionsResolve(tornado.web.RequestHandler):
    def get(self):
        import quant.rules as rules
        rules.mark_resolved(self.get_argument("code"), self.get_argument("rule"))
        invalidate_cache("positions")
        self.redirect("/positions")


class PositionsUploadImg(tornado.web.RequestHandler):
    """接收截图 → OCR → 展示预览，等待用户确认写入。"""

    def post(self):
        import tempfile, os, html as _html
        files = self.request.files.get("img")
        if not files:
            self.write(_wrap("<h1>上传失败</h1><div class=muted>没有收到文件</div>"
                             "<div><a href='/positions'>← 返回</a></div>"))
            return
        f = files[0]
        suffix = os.path.splitext(f["filename"])[1] or ".jpg"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(f["body"])
            tmp_path = tmp.name

        try:
            from quant.datasource.ocr_holdings import ocr_image, parse_portfolio_screenshot
            raw_text = ocr_image(tmp_path)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        if not raw_text:
            self.write(_wrap(
                "<h2>OCR 失败</h2>"
                "<div class=muted>无法识别图片文字。请确保截图清晰，文字对比度足够。</div>"
                "<div style='margin-top:12px'><a href='/positions'>← 返回</a></div>"
            ))
            return

        name_code_map = {}
        try:
            name_df = pd.read_sql(
                "SELECT code, name FROM cn_stock_spot "
                "WHERE `date` = (SELECT MAX(`date`) FROM cn_stock_spot)",
                con=qdb.get_engine())
            name_code_map = {
                str(row["name"]).strip(): str(row["code"]).zfill(6)
                for _, row in name_df.iterrows()
                if str(row.get("name") or "").strip()
            }
        except Exception:
            # 数据库不可用时仍展示 OCR 结果，用户可以在预览页手动填写代码。
            pass
        parsed = parse_portfolio_screenshot(raw_text, name_code_map=name_code_map)
        holdings = parsed["holdings"]
        account = parsed["account"]
        captured_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        import re
        screenshot_time = next((line.strip() for line in raw_text.splitlines()
                                if re.fullmatch(r"\d{1,2}:\d{2}", line.strip())), "")
        known_summary = any(value is not None for value in account.values())

        if not holdings and not known_summary:
            escaped = _html.escape(raw_text[:800])
            self.write(_wrap(
                "<h2>识别到文字但未找到持仓</h2>"
                f"<pre style='font-size:12px;white-space:pre-wrap;color:var(--muted)'>{escaped}</pre>"
                "<div class=muted>可能是截图角度或布局不符。你可以手动在编辑持仓里填写。</div>"
                "<div style='margin-top:12px'><a href='/positions'>← 返回</a></div>"
            ))
            return

        # 构建预览表 + 确认表单
        rows = []
        for h in holdings:
            code_v = _html.escape(str(h.get("code", "")), quote=True)
            name_v = _html.escape(str(h.get("name", "")), quote=True)
            cost_v = _html.escape(str(h.get("cost", "")), quote=True)
            sh_v = _html.escape(str(h.get("shares", "")), quote=True)
            rows.append(
                "<tr>"
                f"<td><input name=code value=\"{code_v}\" style='width:80px'></td>"
                f"<td><input name=name value=\"{name_v}\" style='width:100px'></td>"
                f"<td><input name=cost value=\"{cost_v}\" placeholder='成本价' style='width:90px'></td>"
                f"<td><input name=shares value=\"{sh_v}\" placeholder='股数' style='width:80px'></td>"
                "<td><label><input type=checkbox name=skip value=1> 跳过</label></td>"
                "</tr>"
            )

        account_labels = (
            ("total_assets", "截图总资产", "元"),
            ("total_market_value", "截图总市值", "元"),
            ("total_profit", "截图当前总盈亏", "元"),
            ("today_profit", "截图今日盈亏", "元"),
            ("today_profit_pct", "截图今日涨跌幅", "%"),
            ("available_funds", "券商可用资金（确认后作为动态估值现金）", "元"),
            ("withdrawable_funds", "截图可取资金", "元"),
        )
        account_rows = []
        for key, label, unit in account_labels:
            value = account.get(key)
            value_text = "" if value is None else f"{value:.2f}"
            account_rows.append(
                f"<label style='display:flex;flex-direction:column;gap:5px;min-width:220px'>"
                f"{_html.escape(label)} <span style='display:flex;align-items:center;gap:6px'>"
                f"<input type=number step=0.01 name='account_{key}' "
                f"value='{_html.escape(value_text, quote=True)}' "
                "placeholder='未识别，可留空并核对' style='width:100%'>"
                f"<small>{unit}</small></span></label>")

        body = (
            "<h2>📷 OCR 识别结果 — 请核对后确认写入</h2>"
            "<form action='/positions/upload_confirm' method='post'>"
            "<div class=muted style='margin-bottom:10px'>账户汇总和逐股持仓由截图识别并预填，请检查、修正后再确认。"
            "券商总资产/市值/盈亏是静态截图快照；动态总资产仍按可用现金 + 实时持仓市值计算。"
            "未识别的字段留空，不会录入本次快照。</div>"
            + "<h3>账户摘要（可编辑）</h3><div style='display:flex;flex-wrap:wrap;gap:12px'>"
            + "".join(account_rows) + "</div>"
            + f"<input type=hidden name=account_captured_at value='{_html.escape(captured_at, quote=True)}'>"
            + f"<input type=hidden name=account_screenshot_time value='{_html.escape(screenshot_time, quote=True)}'>"
            + "<h3>逐股持仓（合并导入，不删除其它已有持仓）</h3>"
            + ("<div class=muted>无股数字段时按市值/现价推算；请核对代码、成本和股数。</div>"
               if holdings else "<div class=muted>本张截图未识别出逐股持仓；确认后只更新账户摘要。</div>")
            + ("<table><tr><th>代码</th><th>名称</th><th>成本价</th><th>股数</th><th>跳过?</th></tr>"
               + "".join(rows) + "</table>" if holdings else "")
            + "<div style='margin-top:14px;display:flex;gap:12px'>"
            + "<button type=submit>✔ 确认写入账户与持仓</button>"
            + "<a href='/positions'><button type=button style='background:var(--card2);color:var(--txt);border:1px solid var(--line)'>取消</button></a>"
            + "</div></form>"
            + "<div class=muted style='margin-top:8px'>写入后仍可在「编辑持仓」中修改。</div>"
        )
        self.write(_wrap(body))


class PositionsUploadConfirm(tornado.web.RequestHandler):
    """Validate and merge confirmed OCR data into portfolio config."""

    def post(self):
        import math
        import re
        import urllib.parse

        pairs = urllib.parse.parse_qsl(self.request.body.decode("utf-8", errors="replace"))
        rows: list[dict] = []
        row: dict = {}
        account_fields = {
            "total_assets", "total_market_value", "total_profit", "today_profit",
            "today_profit_pct", "available_funds", "withdrawable_funds",
        }
        account_values = {}
        metadata = {"captured_at": "", "screenshot_time": ""}
        for key, value in pairs:
            if key == "code":
                if row:
                    rows.append(row)
                row = {"code": value.strip(), "skip": False}
            elif key == "name":
                row["name"] = value.strip()
            elif key == "cost":
                row["cost"] = value.strip()
            elif key == "shares":
                row["shares"] = value.strip()
            elif key == "skip":
                row["skip"] = True
            elif key.startswith("account_") and key[8:] in account_fields:
                account_values[key[8:]] = value.strip()
            elif key == "account_captured_at":
                metadata["captured_at"] = value.strip()[:64]
            elif key == "account_screenshot_time":
                metadata["screenshot_time"] = value.strip()[:16]
        if row:
            rows.append(row)

        parsed_account = {}
        for key, raw in account_values.items():
            if not raw:
                continue
            try:
                value = float(raw)
                if not math.isfinite(value) or (
                        value < 0 and key not in {"total_profit", "today_profit", "today_profit_pct"}):
                    raise ValueError
            except (TypeError, ValueError):
                self.set_status(400)
                self.write(_wrap(
                    f"<h2>账户字段无效</h2><p class=muted>{html.escape(key)} 必须是有效数值"
                    "（资金/市值不得为负）。请返回后修正。</p><a href='/positions'>返回持仓页</a>"))
                return
            parsed_account[key] = round(value, 2)

        parsed_holdings = []
        seen_codes = set()
        for item in rows:
            if item.get("skip"):
                continue
            code = item.get("code", "")
            if not code and not item.get("name") and not item.get("cost") and not item.get("shares"):
                continue
            if not re.fullmatch(r"\d{6}", code) or code in seen_codes:
                self.set_status(400)
                self.write(_wrap(
                    "<h2>持仓代码无效</h2><p class=muted>请确保每个未跳过的代码都是唯一的六位数字。</p>"
                    "<a href='/positions'>返回持仓页</a>"))
                return
            seen_codes.add(code)
            try:
                cost = float(item.get("cost") or "")
                shares = float(item.get("shares") or "")
                if (not math.isfinite(cost) or not math.isfinite(shares)
                        or cost <= 0 or shares <= 0 or not shares.is_integer()):
                    raise ValueError
            except (TypeError, ValueError):
                self.set_status(400)
                self.write(_wrap(
                    f"<h2>持仓字段无效</h2><p class=muted>{html.escape(code)} 的成本和股数必须为正数，股数须为整数。</p>"
                    "<a href='/positions'>返回持仓页</a>"))
                return
            parsed_holdings.append({"code": code, "name": item.get("name") or code,
                                    "cost": cost, "shares": int(shares)})

        if not parsed_holdings and not parsed_account:
            self.set_status(400)
            self.write(_wrap("<h2>没有可保存的有效字段</h2><a href='/positions'>返回持仓页</a>"))
            return

        try:
            with _PORTFOLIO_LOCK:
                cfg = _load_config()
                old_holdings = cfg.get("holdings") or []
                existing = {str(h.get("code") or "").zfill(6): h for h in old_holdings}
                for item in parsed_holdings:
                    code = item["code"]
                    target = existing.get(code, {"code": code})
                    target.update(item)
                    existing[code] = target
                cfg["holdings"] = list(existing.values())

                if parsed_account:
                    portfolio = cfg.get("portfolio") or {}
                    portfolio["broker_snapshot"] = {
                        **parsed_account,
                        "captured_at": metadata["captured_at"] or
                        dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                        "screenshot_time": metadata["screenshot_time"],
                    }
                    if "available_funds" in parsed_account:
                        portfolio["cash_balance"] = parsed_account["available_funds"]
                        portfolio["cash_balance_estimated"] = False
                    cfg["portfolio"] = portfolio
                _save_config(cfg)
        except (OSError, ValueError) as error:
            self.set_status(500)
            self.write(_wrap(f"<h2>保存失败</h2><p class=muted>{type(error).__name__}</p>"
                             "<a href='/positions'>返回持仓页</a>"))
            return

        invalidate_cache("positions", "cards", "main")
        self.redirect("/positions")


def build_learn_page():
    from quant.candle_dict import CANDLE_DICT
    rows = []
    for _, item in CANDLE_DICT.items():
        rows.append(f"<tr><td>{html.escape(str(item.get('cn','')))}</td>"
                    f"<td>{html.escape(str(item.get('dir','')))}</td>"
                    f"<td>{html.escape(str(item.get('plain','')))}</td></tr>")
    return ("<h2>新手路线(建议顺序)</h2>"
            "<ol><li><b>K线结构</b>: 开盘、收盘、最高、最低四个价位</li>"
            "<li><b>趋势与均线</b>: 观察高低点和 MA5/10/20，不用单根 K 线预测</li>"
            "<li><b>量价关系</b>: 成交量配合涨跌看参与强弱</li>"
            "<li><b>资金与风险</b>: 资金口径是估算值；买入前先定义止损、仓位</li>"
            "<li><b>形态学习</b>: 形态是概率语言，需结合趋势和量能验证</li></ol>"
            "<h2>61 个形态字典</h2>"
            "<div class=muted>形态不能单独作为买卖信号；这里只提供术语解释。</div>"
            "<table><tr><th>形态</th><th>方向</th><th>白话解释</th></tr>"
            + "".join(rows) + "</table>"
            "<div class=muted><a href='/'>返回总览</a></div>")


class LearnPage(tornado.web.RequestHandler):
    def get(self):
        body = cached("learn", 3600, build_learn_page)
        self.write(_wrap(body))


def _wrap(body):
    """Render shared dashboard chrome and responsive styles."""
    if "<h2>" in body:
        parts = body.split("<h2>")
        body = parts[0] + "".join('<div class="card"><h2>' + part + "</div>" for part in parts[1:])
    styles = """
    :root{--bg:#0a0d13;--card:#111722;--card2:#182031;--line:#212c42;--line-soft:#1a2436;
    --txt:#e2e9f2;--muted:#8593ab;--accent:#3d9bff;--accent-soft:rgba(61,155,255,.13);
    --up:#ff5b5b;--down:#38d685;--gold:#f0c24b}
    *{box-sizing:border-box}
    body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Helvetica Neue',sans-serif;
    color:var(--txt);background:radial-gradient(1100px 340px at 65% -8%,#1b2c4f 0%,transparent 56%),var(--bg)}
    .topbar{position:sticky;top:0;z-index:9;display:flex;align-items:center;gap:6px;padding:11px 22px;
    background:rgba(10,13,19,.9);backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}
    .logo{font-size:17px;font-weight:800;letter-spacing:.4px;margin-right:14px;white-space:nowrap;
    background:linear-gradient(92deg,#4da3ff 15%,#8ad5c9);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
    .clock{color:var(--muted);font-size:12px;margin-left:auto;font-variant-numeric:tabular-nums;white-space:nowrap}
    .topbar a.nl{color:var(--muted);text-decoration:none;font-size:13px;font-weight:500;padding:6px 12px;
    border-radius:8px;transition:color .15s,background .15s}
    .topbar a.nl:hover{color:var(--txt);background:rgba(255,255,255,.05);text-decoration:none}
    .topbar a.nl.active{color:#cfe5ff;background:var(--accent-soft);box-shadow:inset 0 0 0 1px rgba(61,155,255,.35)}
    .wrap{max-width:1180px;margin:20px auto;padding:0 16px}h1{display:none}
    .card{background:linear-gradient(180deg,#131b2a,var(--card) 34%);border:1px solid var(--line);border-radius:12px;
    padding:16px 18px;margin:14px 0;box-shadow:0 1px 0 rgba(255,255,255,.04) inset,0 10px 30px rgba(0,0,0,.35);overflow-x:auto}
    .card .card{box-shadow:none;margin:8px 0;padding:10px 12px;background:var(--card2);border-radius:9px}
    h2{font-size:14px;margin:2px 0 12px;padding-left:10px;letter-spacing:.02em;border-left:3px solid var(--accent);
    line-height:1.4;color:var(--txt)}
    table{border-collapse:collapse;width:100%;font-size:12.5px}th{color:var(--muted);font-weight:600;font-size:11.5px;
    letter-spacing:.03em;text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap;background:transparent}
    td{padding:8px 10px;border-bottom:1px solid var(--line-soft);font-variant-numeric:tabular-nums}
    tr:last-child td{border-bottom:0}tr:hover td{background:var(--accent-soft)}
    .small{font-size:11px;color:var(--muted)}.muted{color:var(--muted);font-size:12px}
    a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
    input{background:var(--card2);color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:6px 10px;outline:none}
    input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
    button{background:#2f81f7;color:#fff;border:0;border-radius:8px;padding:6px 14px;cursor:pointer;
    font-weight:600;transition:background .15s}button:hover{background:#4d96ff}
    .n{color:var(--gold)}.p{color:#7fb2ff}.up{color:var(--up)}.down{color:var(--down)}
    .pill{display:inline-block;padding:1px 8px;border-radius:6px;font-size:11px;font-weight:600;
    border:1px solid var(--line);background:var(--card2);color:var(--muted);margin:1px 2px}
    .pill.res{border-color:rgba(56,214,133,.4);color:#38d685;background:rgba(56,214,133,.09)}
    .pill.warn{border-color:rgba(240,194,75,.4);color:var(--gold);background:rgba(240,194,75,.09)}
    .pill.bad{border-color:rgba(255,91,91,.4);color:#ff5b5b;background:rgba(255,91,91,.09)}
    .seg{display:inline-flex;gap:3px;padding:3px;background:var(--card2);border:1px solid var(--line);border-radius:10px;margin:12px 0}
    .seg button{background:transparent;color:var(--muted);border-radius:7px;padding:7px 16px;font-size:13px;font-weight:600}
    .seg button:hover{background:rgba(255,255,255,.05);color:var(--txt)}
    .seg button.active{background:var(--accent-soft);color:#cfe5ff;box-shadow:inset 0 0 0 1px rgba(61,155,255,.4)}
    .seg .stab-n{opacity:.75;font-size:11px;margin-left:3px;padding:0 6px;background:rgba(255,255,255,.08);border-radius:5px}
    .asset-overview{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr));gap:12px}
    .asset-overview>div{display:flex;flex-direction:column;gap:5px;padding:12px;border:1px solid var(--line);border-radius:10px;background:var(--card2)}
    .asset-overview span,.asset-overview small{color:var(--muted);font-size:12px}.asset-overview strong{font-size:22px;font-variant-numeric:tabular-nums}
    .asset-overview .asset-primary{border-color:rgba(77,163,255,.55);background:linear-gradient(135deg,rgba(77,163,255,.16),var(--card2))}
    .asset-overview .asset-primary strong{font-size:28px;color:var(--gold)}.asset-overview>p{grid-column:1/-1;margin:0;line-height:1.6}
    .broker-snapshot{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-top:12px;padding:14px;border:1px solid var(--line);border-radius:10px;background:rgba(20,26,36,.7)}
    .broker-snapshot-title,.broker-snapshot>p{grid-column:1/-1}.broker-snapshot-title{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}.broker-snapshot-title span,.broker-snapshot-value small{color:var(--muted);font-size:12px}
    .broker-snapshot-value{display:flex;flex-direction:column;gap:5px;padding:10px;border-radius:8px;background:var(--card2)}.broker-snapshot-value span{color:var(--muted);font-size:12px}.broker-snapshot-value strong{font-size:18px;font-variant-numeric:tabular-nums}.broker-snapshot>p{margin:4px 0 0;color:var(--muted);font-size:12px;line-height:1.6}.broker-snapshot .broker-reconciliation{color:var(--gold)}
    .cash-form{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}.cash-form>[data-save-status],.cash-form>.down{width:100%}
    .quote{background:linear-gradient(135deg,#131b2a,var(--card2));border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:14px 0;box-shadow:0 1px 0 rgba(255,255,255,.04) inset,0 10px 30px rgba(0,0,0,.35)}
    .q-name{font-size:20px;font-weight:800}.q-code{color:var(--muted);font-weight:500;margin-left:8px;font-size:15px}
    .q-price{display:flex;align-items:baseline;gap:12px;margin:8px 0 2px}.q-big{font-size:36px;font-weight:800;line-height:1}.q-chg{font-size:17px;font-weight:700}
    .q-stats{display:flex;gap:16px;flex-wrap:wrap;margin-top:10px;color:var(--muted);font-size:13px}.q-stats b{color:var(--txt);font-weight:600;margin-left:3px}
    .analysis-intro{padding:24px 0 18px;max-width:840px}.analysis-kicker{color:var(--accent);font-size:12px;letter-spacing:.12em}
    .analysis-title{display:block;font-size:28px;margin:6px 0}.analysis-intro>p{color:var(--muted);line-height:1.7}
    .analysis-search{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:18px}.analysis-search label{font-size:13px}
    .analysis-checks{display:grid;grid-template-columns:repeat(auto-fit,minmax(275px,1fr));gap:10px}
    .analysis-check{border:1px solid var(--line);border-radius:8px;padding:12px 14px;background:var(--card)}
    .analysis-check b{font-size:14px}.analysis-check p{font-size:12px;line-height:1.6;color:var(--muted);margin:8px 0 0;overflow-wrap:anywhere}
    .analysis-tag{font-size:12px;margin-right:8px;color:var(--gold)}.analysis-check.ok .analysis-tag{color:#34d399}
    .analysis-levels{display:grid;grid-template-columns:repeat(auto-fit,minmax(205px,1fr));border:1px solid var(--line);border-radius:8px;overflow:hidden}
    .analysis-levels>div{padding:14px 16px;border-bottom:1px solid var(--line);background:var(--card)}
    .analysis-levels span,.analysis-metrics dt{display:block;font-size:12px;color:var(--muted)}
    .analysis-levels strong{display:block;margin-top:7px;font-size:23px;font-variant-numeric:tabular-nums}
    .analysis-existing{padding:12px 14px;border:1px solid var(--line);border-radius:8px;font-size:13px;line-height:1.7}
    .analysis-minute{border:1px solid var(--line);border-radius:12px;background:var(--card);padding:12px 14px 8px}
    .analysis-minute .minute-quote{font-size:13.5px;font-variant-numeric:tabular-nums;margin-bottom:6px}
    .analysis-minute .minute-quote b{font-size:16px}
    .analysis-metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:0;margin:0}
    .analysis-metrics>div{border-bottom:1px solid var(--line);padding:10px 8px}.analysis-metrics dd{margin:5px 0 0;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
    .analysis-news{padding-left:22px}.analysis-news li{padding:10px 4px;border-bottom:1px solid var(--line);line-height:1.6}
    .analysis-news-meta{color:var(--muted);font-size:12px}.analysis-risks{line-height:1.8;font-size:13px}.analysis-candidates{display:grid;gap:8px}
    .analysis-candidates a{display:block;padding:11px 14px;border:1px solid var(--line);border-radius:8px}.analysis-candidates span{color:var(--muted)}
    .analysis-search input:focus-visible,.analysis-search button:focus-visible,.analysis-candidates a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
    .analysis-refresh{margin:-8px 0 14px;text-align:right}
    .live-reco-panel{margin:0 0 14px}.live-reco-panel h2{margin:12px 0 6px}
    .live-reco-panel h3{margin:10px 0 4px;color:var(--accent)}.live-reco-panel table{margin:6px 0 10px}
    .live-reco-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
    .live-reco-head button{padding:6px 12px;font-size:13px;min-height:36px;cursor:pointer;
      border:1px solid var(--line);border-radius:8px;background:var(--card);color:inherit}
    .live-reco-panel a{color:var(--accent);text-decoration:none;font-weight:600}.analysis-home-search{display:flex;align-items:center;flex-wrap:wrap;gap:10px;margin:6px 0 12px}
    .analysis-home-search form{display:flex;align-items:center;flex-wrap:wrap;gap:6px}.analysis-home-search input{max-width:100%}
    .analysis-home-search a{margin-left:0!important}.analysis-refresh button{background:transparent;color:var(--muted);border:1px solid var(--line);padding:8px 12px;font-size:12px;min-height:44px}
    @media(max-width:650px){.wrap{padding:0 12px}.topbar{flex-wrap:wrap;gap:8px}.asset-overview{grid-template-columns:repeat(2,minmax(120px,1fr))}
    .asset-overview strong{font-size:18px}.asset-overview .asset-primary strong{font-size:23px}.analysis-verdict{flex-direction:column}.analysis-last{text-align:left}
    .analysis-search input{min-width:100%}.analysis-checks{grid-template-columns:1fr}}
    """
    return (
        "<html><head><meta charset=utf-8><title>量化看板</title><style>" + styles + "</style></head><body>"
        '<div class="topbar"><span class="logo">◈ 量化看板</span>'
        '<a class="nl" href="/">实时总览</a><a class="nl" href="/trade">交易台·买点/持仓</a>'
        '<a class="nl" href="/strategies">战法选股</a><a class="nl" href="/sim">模拟盘</a><a class="nl" href="/learn">K线学堂</a>'
        '<a class="nl" href="http://localhost:9988/instock/kline?code=002878" target="_blank">InStock K线</a>'
        '<span class="clock" id="clock"></span></div><div class="wrap">' + body + "</div>"
        "<script>document.querySelectorAll('.topbar a.nl').forEach(a=>{"
        "if(a.getAttribute('href')===location.pathname)a.classList.add('active');});"
        "function tk(){const c=document.getElementById('clock');if(c)c.innerText=new Date().toLocaleString('zh-CN',{hour12:false});}"
        "tk();setInterval(tk,1000);async function refreshDashboard(){const s=document.getElementById('refresh-status');"
        "if(!s)return;s.textContent=' 正在后台刷新…';try{await fetch('/refresh',{method:'POST'});"
        "const poll=async()=>{const r=await fetch('/refresh/status');const d=await r.json();"
        "if(d.running){s.textContent=' 刷新中：'+d.refreshing.join('、');setTimeout(poll,1200);}"
        "else{s.textContent=' 数据已更新';location.reload();}};setTimeout(poll,800);"
        "}catch(e){s.textContent=' 刷新请求失败，请重试';}}</script></body></html>")


def make_app():
    return tornado.web.Application([
        (r"/", Main),
        (r"/refresh", RefreshData),
        (r"/refresh/status", RefreshStatus),
        (r"/stock", StockPage),
        (r"/analyze", AnalyzePage),
        (r"/trade", TradePage),
        (r"/analyze/refresh", AnalyzeRefresh),
        (r"/analyze/status", AnalyzeStatus),
        (r"/recommend/live", RecommendLive),
    (r"/sector/pick", SectorPick),
        (r"/strategies", StrategiesPage),
        (r"/strategies/data", StrategiesData),
        (r"/sim", SimPage),
        (r"/learn", LearnPage),
        (r"/positions", PositionsPage),
        (r"/positions/data", PositionsData),
        (r"/positions/save", PositionsSave),
        (r"/stock/search", StockSearch),
        (r"/positions/resolve", PositionsResolve),
        (r"/positions/upload_img", PositionsUploadImg),
        (r"/positions/upload_confirm", PositionsUploadConfirm),
    ])


if __name__ == "__main__":
    import asyncio
    import tornado.ioloop as _iol
    app = make_app()
    app.listen(9989)
    io = _iol.IOLoop.current()
    asyncio.get_event_loop().run_in_executor(None, warm_all)
    _iol.PeriodicCallback(lambda: io.asyncio_loop.run_in_executor(None, warm_all),
                          15 * 60 * 1000).start()
    print("量化看板已启动: http://0.0.0.0:9989/ (预热后台进行中;盘中喂表由调度器负责)")
    io.start()

