# -*- coding: utf-8 -*-
"""真·模拟盘:25,000 起始资金,按战法信号与纪律自动模拟交易(不触真实账户)。

与 quant_paper_trades(信号追踪,无资金)不同,本模块维护完整的
资金账户/持仓/交易流水/净值曲线,每天由调度器在尾盘执行一次决策:
  卖出纪律:止损/止盈(+10%减半/+20%清仓)/断板/破MA20;
  买入纪律:战法可信度降序,≥50 才开仓,冰点情绪(<30)不开新仓,
            谨慎情绪(30-45)仓位减半;主板 only(T+1 当日不可卖);
  成交模拟:尾盘信号价 ±0.2% 滑点;当日涨停(≥9.9%)视为买不进;
  仓位纪律:单票 ≤40% 净值、总仓位 ≤90%、100 股整手。

目标线(target)是挑战线不是承诺线:净值页展示进度,如实呈现差距。
"""

from __future__ import annotations

import datetime as dt
import logging

import quant.db as qdb

logger = logging.getLogger("sim")

START_CASH = 25000.0
TARGET = 30000.0
MIN_CONFIDENCE = 50          # 低于此可信度不开仓
MAX_POSITION_RATIO = 0.40     # 单票 ≤40% 净值
MAX_TOTAL_RATIO = 0.90        # 总仓位 ≤90%
SLIPPAGE = 0.002              # 0.2% 滑点
PRICE_TOLERANCE = 1.011       # 距涨停 1.1% 以内视为买不进

_ACCOUNT_DDL = """
CREATE TABLE IF NOT EXISTS quant_sim_account (
  id INT AUTO_INCREMENT PRIMARY KEY,
  as_of DATE NOT NULL,
  cash FLOAT NOT NULL COMMENT '现金',
  market_value FLOAT NOT NULL COMMENT '持仓市值(收盘)',
  net FLOAT NOT NULL COMMENT '总净值',
  start_cash FLOAT NOT NULL,
  target FLOAT NOT NULL COMMENT '挑战目标线(非承诺)',
  note VARCHAR(200),
  UNIQUE KEY uk_date (as_of)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模拟盘资金账户'
"""

_TRADES_DDL = """
CREATE TABLE IF NOT EXISTS quant_sim_trades (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  ts DATETIME NOT NULL,
  trade_date DATE NOT NULL,
  code VARCHAR(10) NOT NULL,
  name VARCHAR(32),
  action VARCHAR(8) NOT NULL COMMENT 'buy/sell',
  price FLOAT NOT NULL,
  qty INT NOT NULL,
  amount FLOAT NOT NULL,
  method VARCHAR(64) COMMENT '命中战法',
  confidence INT,
  reason VARCHAR(300) COMMENT '决策依据留痕',
  UNIQUE KEY uk_day_code (trade_date, code, action)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模拟盘交易流水'
"""

_POSITIONS_DDL = """
CREATE TABLE IF NOT EXISTS quant_sim_positions (
  code VARCHAR(10) PRIMARY KEY,
  name VARCHAR(32),
  qty INT NOT NULL,
  cost FLOAT NOT NULL COMMENT '摊薄成本',
  buy_date DATE NOT NULL COMMENT '最近买入日(T+1判定)',
  stop FLOAT COMMENT '纪律止损价',
  method VARCHAR(64),
  updated_at DATETIME,
  INDEX idx_buy (buy_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模拟盘持仓'
"""

_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS quant_sim_decisions (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  ts DATETIME NOT NULL,
  trade_date DATE NOT NULL,
  side VARCHAR(8) NOT NULL COMMENT 'buy/sell',
  code VARCHAR(10) NOT NULL DEFAULT '-' COMMENT "'-'=当日整体说明(如情绪冰点空仓)",
  name VARCHAR(32) DEFAULT NULL,
  price FLOAT DEFAULT NULL,
  status VARCHAR(20) NOT NULL COMMENT 'done=成交;hold=持仓未触发卖出;skip=候选未买入',
  detail VARCHAR(400) NOT NULL COMMENT '人话原因(未买/未卖/成交依据)',
  UNIQUE KEY uk_day_side_code (trade_date, side, code),
  INDEX idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模拟盘每日决策留痕(含未成交原因)'
"""


def ensure_tables(conn=None):
    own = conn is None
    conn = conn or qdb.get_connection()
    try:
        with conn.cursor() as cur:
            for ddl in (_ACCOUNT_DDL, _TRADES_DDL, _POSITIONS_DDL,
                        _DECISIONS_DDL):
                cur.execute(ddl)
        conn.commit()
    finally:
        if own:
            conn.close()


def init_account(start_cash=START_CASH, target=TARGET, as_of=None, note=""):
    """建账户首日行(幂等:已有任何账户行则不再创建)。"""
    ensure_tables()
    as_of = as_of or dt.date.today()
    conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM quant_sim_account")
            if cur.fetchone()[0]:
                return False
            cur.execute(
                "INSERT INTO quant_sim_account "
                "(as_of, cash, market_value, net, start_cash, target, note) "
                "VALUES (%s, %s, 0, %s, %s, %s, %s)",
                (as_of, start_cash, start_cash, start_cash, target,
                 note or f"起始 {start_cash:,.0f};目标线(非承诺){target:,.0f}"))
        conn.commit()
        logger.info("模拟盘账户初始化:%s 起始 %.0f 目标线 %.0f", as_of,
                    start_cash, target)
        return True
    finally:
        conn.close()


def latest_account():
    ensure_tables()
    conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT as_of, cash, market_value, net, start_cash, "
                        "target, note FROM quant_sim_account "
                        "ORDER BY as_of DESC, id DESC LIMIT 1")
            row = cur.fetchone()
            return (dict(zip(["as_of", "cash", "market_value", "net",
                              "start_cash", "target", "note"], row))
                    if row else None)
    finally:
        conn.close()


def positions():
    ensure_tables()
    conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code, name, qty, cost, buy_date, stop, "
                        "method FROM quant_sim_positions ORDER BY code")
            cols = ["code", "name", "qty", "cost", "buy_date", "stop",
                    "method"]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def recent_trades(limit=30):
    ensure_tables()
    conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT ts, trade_date, code, name, action, price, "
                        "qty, amount, method, confidence, reason "
                        "FROM quant_sim_trades ORDER BY id DESC LIMIT %s",
                        (limit,))
            cols = ["ts", "trade_date", "code", "name", "action", "price",
                    "qty", "amount", "method", "confidence", "reason"]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def net_history(days=30):
    ensure_tables()
    conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT as_of, net, cash, market_value FROM "
                        "quant_sim_account ORDER BY as_of DESC LIMIT %s",
                        (days,))
            rows = cur.fetchall()
            return [dict(zip(["as_of", "net", "cash", "market_value"], r))
                    for r in reversed(rows)]
    finally:
        conn.close()


def snapshot_prices(codes):
    """批量快照价 {code: price}(THS);失败返回空,调用方退持仓成本。"""
    from quant.datasource import ths

    prices = {}
    if not codes:
        return prices
    try:
        resp = ths.prices_snapshot(
            [ths.to_thscode(str(c).zfill(6)) for c in codes]) or {}
        for item in (resp.get("item") or resp.get("data") or []):
            code = str(item.get("thscode") or item.get("code") or "").split(".")[0]
            if item.get("last_price"):
                prices[code.zfill(6)] = float(item["last_price"])
    except Exception as error:  # noqa: BLE001
        logger.warning("快照获取失败: %s", error)
    return prices


# ---------------- 决策纯函数(便于单测) ----------------

def slippage_price(price, side):
    """模拟成交价:买 +0.2%,卖 -0.2%,两位小数。"""
    factor = 1 + SLIPPAGE if side == "buy" else 1 - SLIPPAGE
    return round(float(price) * factor, 2)


def limit_up_unbuyable(pct, price, high):
    """当日涨停/贴近涨停视为买不进(主板涨停 ±10%,以涨幅与当日高点近似)。"""
    if pct is None or price is None:
        return False, ""
    pct = float(pct)
    if pct >= 9.9:
        return True, f"涨停 {pct:+.1f}%,买不进"
    if high and price and float(high) > 0 and price >= float(high) * 0.989:
        return True, "现价贴近当日高点(≈涨停),排队买不进"
    return False, ""


def plan_buys(candidates, cash, net, sentiment_score, held_codes,
              max_new=2):
    """从战法候选生成买入计划(纯函数,签名保持兼容)。

    candidates:战法选股 rows(可信度降序,含 announce 状态);主板过滤
    与涨停剔除在这里做,资金按纪律分配,100 股整手。
    返回 [(code, name, price, qty, method, confidence, reason)]。
    """
    plans, note, _ = plan_buys_verbose(candidates, cash, net, sentiment_score,
                                       held_codes, max_new)
    return plans, note


def plan_buys_verbose(candidates, cash, net, sentiment_score, held_codes,
                      max_new=2):
    """plan_buys 的全维度版:额外返回每个未买候选的人话原因。

    返回 (plans, note, skips):
      plans 同 plan_buys;
      skips = [(code, name, price, reason)] — 被纪律拦下的合格候选
      (已持仓/非主板/公告 blocked/无价/涨停买不进/现金不足),以及
      code='-' 的整体说明(情绪冰点空仓、榜首可信度不足、无候选、
      单日笔数已满)。「为什么今天没买」全部留痕,不静默丢弃。
    """
    plans = []
    skips = []
    held = {str(c).zfill(6) for c in (held_codes or set())}
    score = sentiment_score
    if score is not None and score < 30:
        return plans, (f"情绪冰点({score}/100)<30,今日不开新仓(纪律)"), [
            ("-", "全榜", None,
             f"情绪冰点({score}/100)<30,空仓守则,今日不买入(候选未评估)")]
    ratio_per = (MAX_POSITION_RATIO * 0.5 if (score is None or score < 45)
                 else MAX_POSITION_RATIO)   # 谨慎情绪仓位减半
    note = ("情绪谨慎,单票仓位减半" if (score is not None and score < 45)
            else "单票仓位上限 40%")
    budget = min(cash, net * MAX_TOTAL_RATIO) if net else cash
    used = 0.0
    for row in candidates or []:
        if len(plans) >= max_new:
            skips.append(("-", "后续候选", None,
                         f"单日最多 {max_new} 笔已满,后续合格候选顺延"))
            break
        code = str(row.get("code") or "").zfill(6)
        name = row.get("name") or code
        if code in held:
            skips.append((code, name, row.get("price"), "已持仓,不重复加仓"))
            continue
        if not code.startswith(("60", "00")):
            skips.append((code, name, row.get("price"),
                          "非主板(300/301/688 等),交易权限外"))
            continue
        if (row.get("announce") or {}).get("status") == "blocked":
            skips.append((code, name, row.get("price"),
                          "公告核验 blocked(停牌/退市/立案等高风险),禁止买入"))
            continue
        conf = int(row.get("confidence") or 0)
        if conf < MIN_CONFIDENCE:
            skips.append(("-", f"榜首 {name}", None,
                         f"榜首可信度 {conf}<{MIN_CONFIDENCE},全榜降序,"
                         "今日不开仓"))
            break
        price = row.get("price")
        if not isinstance(price, (int, float)) or float(price) <= 0:
            skips.append((code, name, None,
                          "无现价(连板股 T+1 竞价口径),无法按信号价成交"))
            continue
        pct = None
        if isinstance(row.get("pct"), (int, float)):
            pct = float(row["pct"])
        unbuyable, why = limit_up_unbuyable(pct, price, None)
        if unbuyable:
            skips.append((code, name, float(price),
                          f"{why or '涨停/贴近涨停'},视为买不进"))
            continue
        deal = slippage_price(price, "buy")
        cap = min(net * ratio_per if net else cash, budget - used)
        qty = int(cap / (deal * 100)) * 100
        if qty < 100:
            skips.append((code, name, deal,
                          f"现金 {cash:,.0f} 扣已用 {used:,.0f} 后不足一手"
                          f"(需 {deal * 100:,.0f}),仓位/预算纪律放弃"))
            continue
        plans.append((code, row.get("name"), deal, qty,
                      row.get("methods") or "", conf,
                      f"{row.get('tag')};{str(row.get('evidence') or '')[:80]}"
                      f";{note}"))
        used += deal * qty
    return plans, note, skips


def plan_sells(holds, closes, klines=None):
    """持仓卖出纪律(纯函数)。

    holds:      [{code,qty,cost,buy_date,stop,method,name}]
    closes:     {code: 当日收盘价}
    klines:     {code: [(date, close), ... 升序, 最近几根]} 用于 MA20/断板
    返回 [(code, qty_ratio, price, reason)]:
      qty_ratio 1=清仓, 0.5=减半;price 为滑点后成交价。
    T+1:buy_date == 今日的持仓不可卖。
    """
    today = dt.date.today()
    out = []
    for h in holds or []:
        code = str(h.get("code") or "").zfill(6)
        price = closes.get(code)
        if not price:
            continue
        buy_date = h.get("buy_date")
        if buy_date and str(buy_date) == str(today):
            continue                    # T+1 当日不可卖
        cost = float(h.get("cost") or 0)
        if cost <= 0:
            continue
        pl = (price - cost) / cost
        stop = h.get("stop")
        ma20 = None
        kl = (klines or {}).get(code) or []
        if len(kl) >= 20:
            ma20 = sum(c for _, c in kl[-20:]) / 20.0
        if stop and price <= float(stop):
            out.append((code, 1.0, slippage_price(price, "sell"),
                        f"触及纪律止损 {float(stop):.2f}(现价 {price:.2f})"))
        elif ma20 and price < ma20:
            out.append((code, 1.0, slippage_price(price, "sell"),
                        f"收盘 {price:.2f} 破 MA20 {ma20:.2f},趋势离场"))
        elif pl >= 0.20:
            out.append((code, 1.0, slippage_price(price, "sell"),
                        f"浮盈 {pl:+.1%} ≥20%,清仓保护"))
        elif pl >= 0.10:
            out.append((code, 0.5, slippage_price(price, "sell"),
                        f"浮盈 {pl:+.1%} ≥10%,减半"))
    return out


def hold_reason(h, price, kl=None):
    """持仓今日未触发卖出的人话原因(与 plan_sells 纪律对齐,纯函数)。

    h: {code,qty,cost,buy_date,stop};price: 当日快照/收盘价;
    kl: [(date, close), ...升序] 供 MA20;返回 str。
    """
    today = dt.date.today()
    buy_date = h.get("buy_date")
    if buy_date and str(buy_date) == str(today):
        cost = float(h.get("cost") or 0)
        pl = (price - cost) / cost if (cost > 0 and price) else 0
        return f"今日买入,T+1 不可卖(浮盈 {pl:+.1%},明日核对纪律)"
    cost = float(h.get("cost") or 0)
    if cost <= 0 or not price:
        return "快照价缺失,纪律核对延后(成本/现价不完整)"
    pl = (price - cost) / cost
    stop = h.get("stop")
    ma20 = None
    if kl and len(kl) >= 20:
        ma20 = sum(c for _, c in kl[-20:]) / 20.0
    parts = []
    if stop and price <= float(stop):
        return f"触及纪律止损 {float(stop):.2f}(现价 {price:.2f}),待执行"
    if ma20 and price < ma20:
        return f"收盘 {price:.2f} 破 MA20 {ma20:.2f},趋势离场待执行"
    if pl >= 0.20:
        return f"浮盈 {pl:+.1%} ≥20%,清仓保护待执行"
    if pl >= 0.10:
        return f"浮盈 {pl:+.1%} ≥10%,止盈减半待执行"
    if stop:
        parts.append(f"止损 {float(stop):.2f} 未破(现价 {price:.2f})")
    if ma20:
        parts.append(f"现价 {price:.2f} 在 MA20 {ma20:.2f} 之上,趋势完好")
    parts.append((f"浮盈 {pl:+.1%} 未达 +10% 止盈线" if pl >= 0
                  else f"浮亏 {pl:+.1%} 未触及止损线"))
    return ";".join(parts)


def log_decisions(rows, trade_date=None):
    """落每日决策留痕(REPLACE 幂等;uk=trade_date+side+code)。

    rows: [(side, code, name, price, status, detail)];code='-' 为整体说明。
    """
    if not rows:
        return 0
    ensure_tables()
    trade_date = trade_date or dt.date.today()
    now = dt.datetime.now()
    conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            for side, code, name, price, status, detail in rows:
                cur.execute(
                    "REPLACE INTO quant_sim_decisions "
                    "(ts, trade_date, side, code, name, price, status, detail) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (now, trade_date, side, str(code or "-")[:10],
                     str(name)[:32] if name else None,
                     float(price) if isinstance(price, (int, float)) else None,
                     str(status)[:20], str(detail)[:400]))
        conn.commit()
    finally:
        conn.close()
    logger.info("模拟盘决策留痕 %s: %s 条", trade_date, len(rows))
    return len(rows)


def recent_decisions(days=3, limit=80):
    """近 N 日决策留痕(页面用),按日期+时间倒序。"""
    ensure_tables()
    conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ts, trade_date, side, code, name, price, status, "
                "detail FROM quant_sim_decisions WHERE trade_date >= "
                "DATE_SUB(CURDATE(), INTERVAL %s DAY) "
                "ORDER BY trade_date DESC, side, id DESC LIMIT %s",
                (days, limit))
            keys = ("ts", "trade_date", "side", "code", "name", "price",
                    "status", "detail")
            return [dict(zip(keys, row)) for row in cur.fetchall()]
    finally:
        conn.close()


__all__ = ["ensure_tables", "init_account", "latest_account", "positions",
           "recent_trades", "net_history", "plan_buys", "plan_buys_verbose",
           "plan_sells", "hold_reason", "log_decisions", "recent_decisions",
           "slippage_price", "limit_up_unbuyable",
           "START_CASH", "TARGET", "MIN_CONFIDENCE",
           "MAX_POSITION_RATIO", "MAX_TOTAL_RATIO", "SLIPPAGE"]
