# -*- coding: utf-8 -*-
"""板块维度(M8b):行业资金流强度——把"板块先于个股"纳入证据链。

数据源:InStock 每日全链维护的 cn_stock_fund_flow_industry(东财资金流,
每日 execute_daily_job 收盘后刷新,故盘中一律为上一交易日口径——如实披露)。

口径(讲证据):
  * 行业主力净流入 fund_amount>0 且流入排名前 1/3 ⟹ 视为「板块共振」
  * 行业名规范:去掉东财行业分级后缀(Ⅰ/Ⅱ/Ⅲ),与个股 spot 行业名对齐;
    对不上的个股显示"未匹配",绝不硬凑。
"""

import logging
import time

import pandas as pd

from quant import db as qdb
from quant.datasource import ths

logger = logging.getLogger("sector")

# 东财行业分级后缀(Ⅱ/Ⅲ 等),规范到可对齐个股行业的粒度
_SUFFIXES = ("Ⅲ", "Ⅱ", "Ⅰ")


def _clean_name(s: str) -> str:
    s = str(s or "").strip()
    for suffix in _SUFFIXES:
        if s.endswith(suffix):
            return s[: -len(suffix)].strip()
    return s


def industry_flow_snapshot(engine) -> dict:
    """读最近一交易日行业资金流,返回 {规范名: dict(fund_amount亿, change_rate%, rank, n)}。"""
    df = pd.read_sql(
        "SELECT name, change_rate, fund_amount FROM cn_stock_fund_flow_industry "
        "WHERE `date` = (SELECT MAX(`date`) FROM cn_stock_fund_flow_industry)",
        con=engine)
    if df.empty:
        return {}
    df["name_clean"] = df["name"].map(_clean_name)
    # 同一规范名取主力净额绝对值更大的一行(保留信息量,同时去分级重复)
    df = df.sort_values("fund_amount", ascending=False).drop_duplicates(
        subset="name_clean", keep="first").set_index("name_clean")
    df["rank"] = df["fund_amount"].rank(ascending=False, method="min")
    n = len(df)
    out = {}
    for name, r in df.iterrows():
        out[name] = {
            "industry": name,
            "fund_amount_yi": float(r["fund_amount"]) / 1e8,
            "change_rate": float(r["change_rate"]),
            "rank": int(r["rank"]),
            "n": n,
            "top33": bool(r["rank"] <= max(1, round(n / 3))),
            "source": "东财昨日",
        }
    for v in out.values():
        v["resonant"] = bool(v["top33"] and v["fund_amount_yi"] > 0)
    return out


def sector_state(engine, industry: str):
    """查某个股行业(个股 spot 的 industry 字符串)的行业状态;查不到返回 None。

    优先 THS 实时口径(快照聚合);失败则回落东财昨日资金流口径。
    """
    for cand in (industry, _clean_name(industry)):
        state = ths_heat_state(cand)
        if state:
            return state
    snap = industry_flow_snapshot(engine)
    if not snap or not industry:
        return None
    return snap.get(industry) or snap.get(_clean_name(industry))


# ---------------- THS 实时口径(快照自治聚合,数值 100% 来自同花顺) ----------------

_ths_cache = {"ts": 0, "data": None}


def industry_heat_ths(min_refresh=180, min_stocks=5):
    """THS 实时板块热度:全市场快照按行业聚合。

    返回 {industry: {pct_mid(中位涨幅%), up_ratio(上涨家占比), amt_yi(成交额亿),
    rank, n, top33}};300 秒内复用缓存(一次全市场快照 ~1.2 秒)。
    行业标签借用东财 spot 表的静态分类名做分组键(THS 无个股→行业端点)。
    """
    import time as _time
    now = _time.time()
    if _ths_cache["data"] is not None and now - _ths_cache["ts"] < min_refresh:
        return _ths_cache["data"]
    from quant import db as qdb
    from quant.datasource import ths as ths_mod
    spot = pd.read_sql("SELECT code, industry FROM cn_stock_spot "
                       "WHERE industry IS NOT NULL AND industry != ''", con=qdb.get_engine())
    ind_map = dict(zip(spot["code"], spot["industry"].map(_clean_name)))
    items = []
    for off in (0, 5000):
        r = ths_mod.prices_snapshot(limit=5000, offset=off) or {}
        batch = r.get("item") or []
        items.extend(batch)
        if len(batch) < 5000:
            break
    rows = []
    for it in items:
        code = (it.get("thscode") or "").split(".")[0]
        ind = ind_map.get(code)
        pct = it.get("price_change_ratio_pct")
        amt = it.get("turnover")
        if ind and isinstance(pct, (int, float)):
            rows.append((ind, float(pct), float(amt or 0) / 1e8))
    df = pd.DataFrame(rows, columns=["industry", "pct", "amt"])
    if df.empty:
        return {}
    g = df.groupby("industry").agg(
        n=("pct", "size"), pct_mid=("pct", "median"), up_ratio=("pct", lambda s: float((s > 0).mean())),
        amt_yi=("amt", "sum")).reset_index()
    g = g[g["n"] >= min_stocks].copy()
    g["rank"] = g["pct_mid"].rank(ascending=False, method="min").astype(int)
    n = len(g)
    g["top33"] = g["rank"] <= max(1, round(n / 3))
    out = {r["industry"]: {
        "industry": r["industry"], "pct_mid": float(r["pct_mid"]),
        "up_ratio": float(r["up_ratio"]), "amt_yi": float(r["amt_yi"]),
        "rank": int(r["rank"]), "n": n, "top33": bool(r["top33"]),
        "source": "THS实时", "n_stocks": int(r["n"]),
    } for _, r in g.iterrows()}
    for v in out.values():
        v["resonant"] = bool(v["top33"] and v["pct_mid"] > 0)
    _ths_cache.update({"ts": now, "data": out})
    return out


def ths_heat_state(industry: str):
    """THS 实时口径查单行业;无数据返回 None。"""
    if not industry:
        return None
    heat = industry_heat_ths()
    if not heat:
        return None
    return heat.get(industry) or heat.get(_clean_name(industry))


# --------- 优先级最高:THS 行业指数实时(成分股映射,纯同花顺分类+数值) ---------

_idx_cache = {"date": None, "list": []}
_heat_cache = {"ts": 0.0, "data": {}}


def ths_index_heat(min_refresh=300):
    """THS 行业指数实时热度榜。

    返回 {index_code: {index_code, name, pct(当日涨幅%), rank, n, top33,
    resonant(pct>0且前1/3), source='THS指数实时'}};
    指数目录按天缓存,快照 300 秒缓存(一次批量调用 ~0.2s)。
    """
    now = time.time()
    if _heat_cache["data"] and now - _heat_cache["ts"] < min_refresh:
        return _heat_cache["data"]
    today = time.strftime("%Y-%m-%d")
    if _idx_cache["date"] != today:
        try:
            d = ths.ths_index_list("industry") or {}
            _idx_cache.update({"date": today, "list": d.get("item") or []})
        except Exception:  # noqa: BLE001
            pass
    items = _idx_cache["list"]
    if not items:
        return {}
    name_map = {x["thscode"]: x["name"] for x in items}
    sn = (ths.index_snapshot(list(name_map)) or {}).get("item") or []
    rows = []
    for it in sn:
        mA = it.get("price_change_ratio_pct")
        if mA is None:
            continue
        rows.append((it["thscode"], name_map.get(it["thscode"], ""), float(mA)))
    if not rows:
        return {}
    df = pd.DataFrame(rows, columns=["index_code", "name", "pct"])
    n = len(df)
    df["rank"] = df["pct"].rank(ascending=False, method="min").astype(int)
    df["top33"] = df["rank"] <= max(1, round(n / 3))
    out = {r["index_code"]: {
        "index_code": r["index_code"], "name": r["name"], "pct": float(r["pct"]),
        "rank": int(r["rank"]), "n": n, "top33": bool(r["top33"]),
        "source": "THS指数实时",
        "resonant": bool(r["top33"] and r["pct"] > 0),
    } for _, r in df.iterrows()}
    _heat_cache.update({"ts": now, "data": out})
    return out


def code_heat_map(engine=None):
    """code → 板块状态映射,三级回落:THS指数(membership) → THS快照聚合 → 东财昨日资金流。

    状态统一含 resonant/source;供 M3 与持仓决策共用。
    """
    heat = {}
    try:
        heat = ths_index_heat()
    except Exception:  # noqa: BLE001
        heat = {}
    if heat:
        try:
            mem = pd.read_sql(
                "SELECT code, index_code FROM quant_ths_membership "
                "WHERE updated=(SELECT MAX(updated) FROM quant_ths_membership)",
                con=engine or qdb.get_engine())
            if not mem.empty:
                out = {}
                for code, ic in zip(mem["code"], mem["index_code"]):
                    s = heat.get(ic)
                    if not s:
                        continue
                    prev = out.get(code)
                    # 多层归属选共振优先,其次排名靠前(更具体的一级)
                    if prev is None or (s["resonant"] and not prev["resonant"]) \
                            or (s["resonant"] == prev["resonant"] and s["rank"] < prev["rank"]):
                        out[code] = s
                return out
        except Exception:  # noqa: BLE001
            pass
    try:
        agg = industry_heat_ths()  # THS 快照聚合(名义:东财行业标签)
        if agg:
            return {k: v for k, v in agg.items()}
    except Exception:  # noqa: BLE001
        pass
    try:
        return industry_flow_snapshot(engine or qdb.get_engine())
    except Exception:  # noqa: BLE001
        return {}


def stock_sector_now(engine, code, industry_name=None):
    """单只股票的板块状态(THS 指数优先)。供持仓/竞价/尾盘决策用。"""
    bench = code_heat_map(engine)
    s = bench.get(code)
    if s:
        return s
    if industry_name:
        return ths_heat_state(industry_name) or industry_flow_snapshot(engine).get(
            _clean_name(industry_name))
    return None
