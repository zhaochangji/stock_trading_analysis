# -*- coding: utf-8 -*-
"""THS 快照高速增量:全市场当日 bar 一次拉齐,替代东财逐只抓取(8 分钟 → ~30 秒)。

原理:收盘后(≥15:05) THS prices_snapshot 即为当日终值,分页两次拉全市场。
口径保障(库内为东财前复权,THS 快照为真实价):
  * 无除权时,前复权价 == 真实价,可直写;
  * 用「快照昨收 vs 库内昨收」逐只校验,不一致(当日除权除息)→ 列入 fallback,
    仍走东财逐只补抓(由 backfill_hist 调用方执行),序列永不被污染;
  * 新股(库内无记录)→ fallback(需完整首日数据);
  * 停牌股快照无行情 → 自然跳过,与东财行为一致。
换手率由昨日「成交量/换手%」反推流通股本再计算(股本日间几乎不变)。
"""

import datetime as dt
import logging

from quant import db as qdb
from quant.datasource import ths
from quant.datasource.hist_store import insert_rows

logger = logging.getLogger("ths_incr")

_MINUTE_CUTOFF = (15, 5)  # 收盘后快照才是当日完整 bar


def _is_after_close():
    now = dt.datetime.now()
    if now.time() < dt.time(*_MINUTE_CUTOFF):
        # 周末/收盘前快照非终值
        return False
    return True


def _fetch_all_snapshot(pages=6, page_size=5000):
    """分页拉全市场快照,返回 list[dict]。"""
    items, offset = [], 0
    for _ in range(pages):
        r = ths.prices_snapshot(limit=page_size, offset=offset) or {}
        batch = r.get("item") or []
        items.extend(batch)
        total = r.get("total") or 0
        offset += len(batch)
        if not batch or (total and offset >= total):
            break
    return items


def _db_states(engine):
    """每股:最新日期、最近一次有换手的(日期, 收盘, 量, 换手%)。"""
    import pandas as pd
    latest = pd.read_sql(
        "SELECT code, MAX(`date`) AS d FROM quant_hist_daily GROUP BY code",
        con=engine).set_index("code")["d"].astype(str).to_dict()
    prev = pd.read_sql(
        "SELECT a.code, a.`date`, a.close, a.volume, a.turnover FROM quant_hist_daily a "
        "JOIN (SELECT code, MAX(`date`) d FROM quant_hist_daily "
        "      WHERE turnover > 0 GROUP BY code) b "
        "  ON a.code=b.code AND a.`date`=b.d",
        con=engine).set_index("code")[["date", "close", "volume", "turnover"]].to_dict("index")
    return latest, prev


def fast_incremental(engine=None):
    """高速增量入口。返回 (写入行数, fallback_codes, stats_dict)。

    fallback_codes:除权/新股等需走东财逐只补抓的代码清单。
    收盘前调用返回 (0, [], {'reason': 'before_close'})。
    """
    engine = engine or qdb.get_engine()
    if not _is_after_close():
        logger.info("未到收盘后(15:05),快照非终值,跳过 THS 增量")
        return 0, [], {"reason": "before_close"}

    today = dt.date.today().isoformat()
    snaps = _fetch_all_snapshot()
    if len(snaps) < 3000:
        return 0, [], {"reason": f"snapshot_too_few:{len(snaps)}"}
    logger.info("快照 %d 条", len(snaps))

    latest, prev = _db_states(engine)
    rows, fallback, seen = [], [], set()
    n_skip_done = 0
    for s in snaps:
        code = (s.get("thscode") or "").split(".")[0]
        if not code or len(code) != 6 or not code.isdigit():
            continue
        seen.add(code)
        if latest.get(code) == today:
            n_skip_done += 1
            continue
        last_price = s.get("last_price")
        if not last_price or float(s.get("volume") or 0) <= 0:
            fallback.append(code)  # 停牌/零成交:交东财判定(补不到即真停牌)
            continue
        p = prev.get(code)
        if not p:
            fallback.append(code)          # 新股/库内无记录
            continue
        prev_close = float(p["close"])
        snap_prev = float(s.get("prev_price") or 0)
        # 除权除息校验:昨收对不上(容差 0.15% + 2 分)→ 前复权序列会错位,走东财
        if not snap_prev or abs(snap_prev - prev_close) > max(0.02, prev_close * 0.0015):
            fallback.append(code)
            continue
        # 反推流通股本算换手率
        shares = None
        try:
            shares = float(p["volume"]) / (float(p["turnover"]) / 100.0)
        except (TypeError, ZeroDivisionError):
            pass
        if not shares or shares <= 0:
            fallback.append(code)
            continue
        o, h, l, c = (float(s["open_price"]), float(s["high_price"]),
                      float(s["low_price"]), float(last_price))
        vol = round(float(s["volume"]) / 100) * 100  # 股,对齐东财整手口径
        amt = float(s.get("turnover") or 0)
        pct = float(s.get("price_change_ratio_pct") or 0)
        chg = float(s.get("price_change") or 0)
        amp = (h - l) / prev_close * 100 if prev_close else 0
        to = vol / shares * 100
        rows.append((code, today, round(o, 2), round(c, 2), round(h, 2), round(l, 2),
                     vol, amt, round(amp, 2), round(pct, 2), round(chg, 2), round(to, 2)))

    # 快照盲区兜底:库内未到今日、快照又完全没见到的代码(含真停牌)→ 东财逐只判定
    holes = [c for c, last_d in latest.items() if last_d < today and c not in seen]
    fallback.extend(holes)

    written = insert_rows(rows)
    stats = {"snapshots": len(snaps), "written": written, "fallback": len(set(fallback)),
             "holes": len(holes), "skip_done": n_skip_done}
    logger.info("THS 增量:写入 %d 行,fallback %d 只(除权/新股 %d + 盲区/停牌 %d),当日已存在跳过 %d 只",
                written, len(set(fallback)), len(set(fallback)) - len(holes), len(holes),
                n_skip_done)
    return written, sorted(set(fallback)), stats
