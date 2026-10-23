# -*- coding: utf-8 -*-
"""交易纪律系统(严格模式,单一事实源)。

用户输入:总资产、持仓(代码/成本/股数/买入时间);本模块输出:
  1. 仓位控制(单票上限/总仓位上限,随大盘状态调整)
  2. 止损纪律线(硬性:成本-7% 与 技术破位 取更近者)
  3. 止盈纪律(分批:>+10% 减半、>+20% 降到成本线保护)
  4. 加仓禁令(浮亏加仓 = 违规,系统记录并催办)
  5. 纪律对账:触发记录入 quant_discipline,未执行的反复提醒

级别:INFO(提示)/ WARN(预警)/ DISCIPLINE(必须执行)。
"严格遵守"的含义:DISCIPLINE 级每轮推送都出现,直到用户标记已执行。
"""

import datetime as dt

# ---- 硬性参数(修改须在页面"交易系统说明"同步) ----
MAX_POS_SINGLE = 0.25        # 单票市值 / 总资产 上限 25%
MAX_POS_TOTAL_NORMAL = 0.60  # 大盘中性/偏多时 总持仓上限 60%
MAX_POS_TOTAL_DEFENSE = 0.30  # 大盘防守时 总持仓上限 30%
STOP_LOSS_PCT = 0.07         # 硬止损:成本 ×(1-7%)
WARN_LOSS_PCT = 0.05         # 浮亏 5% 预警
TP_HALF = 0.10               # 浮盈 10% → 减半
TP_PROTECT = 0.20            # 浮盈 20% → 降至成本线保护
ADD_LOSS_BAN = 0.03          # 浮亏 >3% 时禁止加仓(摊平=违规)
CHG_DROP_TODAY = -5.0        # 当日跌幅 ≤-5% → 当日禁买(接飞刀)


def _fmt(v, d=2):
    try:
        return f"{float(v):+.{d}f}" if d else f"{float(v):.0f}"
    except (TypeError, ValueError):
        return "—"


def _f(v):
    """配置里的 cost/shares 可能是字符串('15.10'/'300')→ float;失败 None。"""
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def assess_holding(h, price, market_state="中性", change_rate=None, ma20=None):
    """单持仓评估。h: {code,name,cost,shares,buy_date};price: 现价。

    返回 findings: [(level, rule, message)],级别最重者决定该票的行动提示。
    """
    cost, shares = _f(h.get("cost")), _f(h.get("shares"))
    if not cost or not price:
        return []
    out = []
    pl = price / cost - 1
    # ① 硬止损(纪律级):成本-7% 与技术位(MA20)取更近者
    stop_cost = cost * (1 - STOP_LOSS_PCT)
    stop_tech = ma20 if (ma20 and stop_cost < ma20 < cost) else None
    stop = stop_cost
    stop_src = f"成本-{int(STOP_LOSS_PCT*100)}%"
    if stop_tech:
        stop, stop_src = stop_tech, "MA20技术位"
    if price <= stop:
        out.append(("DISCIPLINE", "止损",
                    f"现价 {_fmt(price)} 已触发止损线 {stop_src}({_fmt(stop)})——"
                    f"纪律要求执行,亏损 {_fmt(pl*100)}% 不因幻想扩大"))
    elif pl <= -WARN_LOSS_PCT:
        out.append(("WARN", "止损预警",
                    f"浮亏 {_fmt(pl*100)}%(预警线 {int(WARN_LOSS_PCT*100)}%),"
                    f"止损线 {stop_src}={_fmt(stop)};跌破即执行"))
    # ② 止盈纪律
    if pl >= TP_PROTECT:
        out.append(("WARN", "止盈",
                    f"浮盈 {_fmt(pl*100)}% ≥{int(TP_PROTECT*100)}%——"
                    f"纪律:降至成本线保护(至少减半),让利润落袋"))
    elif pl >= TP_HALF:
        out.append(("INFO", "止盈",
                    f"浮盈 {_fmt(pl*100)}% ≥{int(TP_HALF*100)}%——纪律:减仓 1/2,剩余上移止损到成本线"))
    # ③ 加仓禁令(浮亏加仓=违规)
    if pl <= -ADD_LOSS_BAN:
        out.append(("DISCIPLINE", "加仓禁令",
                    f"浮亏 {_fmt(pl*100)}% > {int(ADD_LOSS_BAN*100)}%——禁止加仓摊平;"
                    f"加仓仅在 回踩支撑+主力净流入+板块共振 三条件齐时进行"))
    # ④ 当日大跌禁买
    if change_rate is not None and change_rate <= CHG_DROP_TODAY:
        out.append(("WARN", "当日禁买",
                    f"今日 {_fmt(change_rate,1)}% ≤ {CHG_DROP_TODAY}%——当日不接飞刀,等企稳"))
    # ⑤ 单票仓位上限
    if shares:
        mktv = price * shares
        out.append(("INFO", "仓位",
                    f"市值 {_fmt(mktv/1e4,0)} 万;单票上限 {int(MAX_POS_SINGLE*100)}%"
                    f"(需总资产数据校验)"))
    return out


def assess_portfolio(holdings, prices, total_assets, market_state="中性"):
    """组合级评估:总仓位与分散度。prices: {code: price}。

    返回 (findings, 总仓位比例)。
    """
    out = []
    cap = MAX_POS_TOTAL_DEFENSE if market_state == "防守" else MAX_POS_TOTAL_NORMAL
    total_assets = _f(total_assets)
    total_mktv, per = 0.0, []
    for h in holdings:
        p = prices.get(h.get("code"))
        sh = _f(h.get("shares"))
        if p and sh:
            m = p * sh
            total_mktv += m
            per.append((h.get("code"), h.get("name", ""), m))
    ratio = (total_mktv / total_assets) if total_assets else None
    if ratio is not None:
        state_txt = f"大盘[{market_state}]上限 {int(cap*100)}%"
        if ratio > cap:
            out.append(("DISCIPLINE", "总仓位",
                        f"当前 {ratio*100:.0f}% 超过{state_txt}——纪律:减仓至 {int(cap*100)}% 以内"))
        else:
            out.append(("INFO", "总仓位", f"当前 {ratio*100:.0f}%,{state_txt} 内"))
    for code, name, m in sorted(per, key=lambda x: -x[2]):
        if total_assets and m / total_assets > MAX_POS_SINGLE:
            out.append(("DISCIPLINE", "单票仓位",
                        f"{name}({code}) 市值 {m/total_assets*100:.0f}% 超单票上限 "
                        f"{int(MAX_POS_SINGLE*100)}%——纪律:减仓"))
    return out, ratio


def stop_take_prices(h, ma20=None):
    """逐股止损/止盈具体价位。

    止损 = 成本-7% 与 MA20 取更近者(更高,先触发先执行);
    止盈减半线 = 成本×1.10;止盈保护线 = 成本×1.20。
    h 无成本时返回 None。
    """
    cost = _f(h.get("cost"))
    if not cost:
        return None
    stop_cost = round(cost * (1 - STOP_LOSS_PCT), 2)
    stop, src = stop_cost, f"成本-{int(STOP_LOSS_PCT*100)}%"
    if ma20 and stop_cost < ma20 < cost:
        stop, src = round(ma20, 2), "MA20技术位"
    return {"stop": stop, "stop_src": src, "stop_cost": stop_cost,
            "tp_half": round(cost * (1 + TP_HALF), 2),
            "tp_protect": round(cost * (1 + TP_PROTECT), 2)}


def overall_action(findings):
    """按最重级别给出行动词。"""
    levels = [f[0] for f in findings]
    if "DISCIPLINE" in levels:
        return "执行纪律", "DISCIPLINE"
    if "WARN" in levels:
        return "警惕", "WARN"
    return "观察", "INFO"


# ---------------- 纪律对账(quant_discipline 表) ----------------

_CREATE = """
CREATE TABLE IF NOT EXISTS `quant_discipline` (
  `id`         BIGINT      AUTO_INCREMENT PRIMARY KEY,
  `date`       DATE        NOT NULL,
  `code`       VARCHAR(10) NOT NULL,
  `name`       VARCHAR(32) DEFAULT NULL,
  `rule`       VARCHAR(24) NOT NULL,
  `level`      VARCHAR(12) NOT NULL COMMENT 'WARN/DISCIPLINE',
  `message`    VARCHAR(300) DEFAULT NULL,
  `action`     VARCHAR(120) DEFAULT NULL COMMENT '要求动作',
  `resolved`   TINYINT     DEFAULT 0 COMMENT '用户标记已执行=1',
  `created_at` DATETIME,
  UNIQUE KEY `uk` (`date`,`code`,`rule`),
  INDEX `idx_resolved` (`resolved`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='交易纪律对账(未执行项反复提醒)'
"""


def record_findings(findings_map, conn=None):
    """findings_map: {code: (name, findings)};WARN/DISCIPLINE 写表(当日去重)。"""
    import pandas as pd
    own = conn is None
    if own:
        from quant import db as qdb
        conn = qdb.get_connection()
    today = dt.date.today().isoformat()
    n = 0
    try:
        with conn.cursor() as cur:
            cur.execute(_CREATE)
            for code, (name, fs) in (findings_map or {}).items():
                for level, rule, msg in fs:
                    if level == "INFO":
                        continue
                    cur.execute(
                        "REPLACE INTO quant_discipline "
                        "(`date`,`code`,`name`,`rule`,`level`,`message`,`created_at`) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (today, code, name, rule, level, msg[:300],
                         dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                    n += 1
        conn.commit()
    finally:
        if own:
            conn.close()
    return n


def pending_discipline(engine=None):
    """未执行的纪律项(每轮推送反复提醒)。返回 DataFrame。"""
    import pandas as pd
    from quant import db as qdb
    engine = engine or qdb.get_engine()
    # 幂等建表(首次查询前保证表存在)
    conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(_CREATE)
        conn.commit()
    finally:
        conn.close()
    return pd.read_sql(
        "SELECT * FROM quant_discipline WHERE resolved=0 "
        "ORDER BY level DESC, id DESC LIMIT 20", con=engine)


def mark_resolved(code, rule, conn=None):
    """用户标记已执行(页面上点"已执行")。"""
    own = conn is None
    if own:
        from quant import db as qdb
        conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE quant_discipline SET resolved=1 WHERE code=%s AND rule=%s",
                (code, rule))
        conn.commit()
    finally:
        if own:
            conn.close()
