# -*- coding: utf-8 -*-
"""战法可行性追踪:三路战法(M3 共振/龙头战法/板块优质)每日候选落库,
收盘链自动回填 T+1/T+3/T+5 与胜负,按战法聚合胜率——「哪路战法可行」
用前向样本说话,不做没有历史数据支撑的回测(历史涨停梯队/板块热度
快照无法回放,如实标注)。

口径说明:
- strategy_log 的样本 = 战法页每路战法 top5 候选(收盘时点);
- quant_paper_trades 的推送信号(2026-10-14 起)是「M3+时段确认」的
  精选(每日 1-3 只),作为 M3 战法的**推送口径参考**并列展示,
  不与战法页 top5 样本混算。
"""

import logging

import quant.db as qdb
from quant.paper import spot_latest_closes

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS `quant_strategy_trades` (
  `id`         BIGINT       AUTO_INCREMENT PRIMARY KEY,
  `log_date`   DATE         NOT NULL COMMENT '记录日(收盘时点)',
  `method`     VARCHAR(16)  NOT NULL COMMENT 'm3/leader/sector',
  `code`       VARCHAR(10)  NOT NULL,
  `name`       VARCHAR(32)  DEFAULT NULL,
  `confidence` FLOAT        DEFAULT NULL COMMENT '战法页可信度',
  `price`      FLOAT        DEFAULT NULL COMMENT '记录时点价(T+N胜负基准)',
  `close_t1`   FLOAT        DEFAULT NULL COMMENT 'T+1收盘',
  `close_t3`   FLOAT        DEFAULT NULL,
  `close_t5`   FLOAT        DEFAULT NULL,
  `win_t1`     TINYINT      DEFAULT NULL COMMENT 'T+1收盘>记录价=1',
  `win_t5`     TINYINT      DEFAULT NULL,
  UNIQUE KEY `uk` (`log_date`, `method`, `code`),
  INDEX `idx_date` (`log_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='战法选股追踪与可行性统计'
"""

# row["methods"] 字符串里的战法关键词 → 战法键
_METHOD_KEYS = (
    ("m3", "M3共振"),
    ("leader", "龙头战法"),
    ("sector", "板块优质"),
)

METHOD_LABELS = {
    "m3": "M3 多共振",
    "leader": "龙头战法",
    "sector": "板块优质",
}


def ensure_table():
    conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
    finally:
        conn.close()


def log_today(rows, log_date=None, top=5, spot_prices=None):
    """把战法页混合榜按战法归属拆分,每路取可信度前 top 只落库(REPLACE 幂等)。

    rows 来自 scripts.quant_web._strategy_payload()["rows"];price 缺失的行
    (连板股不给现价,T+1 竞价思路)用 spot_prices[code](当日喂表收盘价)
    作 T+N 胜负基准——龙头战法的 T+1 胜负即「连板次日溢价」;仍无价则跳过。
    返回 {method: 记录条数}。
    """
    import datetime as dt
    import re as _re

    ensure_table()
    log_date = log_date or dt.date.today()
    spot_prices = spot_prices or {}

    def _price(row):
        value = row.get("price")
        if isinstance(value, (int, float)):
            return float(value)
        fallback = spot_prices.get(str(row.get("code") or ""))
        return float(fallback) if fallback else None

    picked = {}
    for key, word in _METHOD_KEYS:
        mine = []
        for r in (rows or []):
            if word not in str(r.get("methods") or ""):
                continue
            if not _re.fullmatch(r"\d{6}", str(r.get("code") or "")):
                continue
            price = _price(r)
            if price is None:
                continue
            mine.append((r, price))
        mine.sort(key=lambda item: (-(item[0].get("confidence") or 0),
                                     item[0].get("code")))
        picked[key] = mine[:top]
    conn = qdb.get_connection()
    counts = {}
    try:
        with conn.cursor() as cur:
            for key, mine in picked.items():
                for r, price in mine:
                    cur.execute(
                        "REPLACE INTO quant_strategy_trades "
                        "(log_date,method,code,name,confidence,price) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        (log_date, key, r["code"], r.get("name"),
                         float(r.get("confidence") or 0), price))
                counts[key] = len(mine)
        conn.commit()
    finally:
        conn.close()
    logger.info("战法追踪落库 %s: %s", log_date, counts)
    return counts


def update_tracking():
    """用本地 K 线回填 T+1/T+3/T+5 收盘与胜负(与 quant.paper 同思路)。

    东财断连当日 K 线缺行时,用喂表收盘价兜底补 hist 帧(收盘后喂表
    =当日终值),隔日 dump 补行后重跑同值覆盖,幂等。
    """
    import pandas as pd

    ensure_table()
    engine = qdb.get_engine()
    st = pd.read_sql(
        "SELECT id, code, log_date, price FROM quant_strategy_trades",
        con=engine)
    if st.empty:
        return 0, 0
    codes = sorted({str(c).strip() for c in st["code"]})
    min_date = pd.to_datetime(st["log_date"]).min().strftime("%Y-%m-%d")
    code_list = ",".join(f"'{c}'" for c in codes)
    hist = pd.read_sql(
        f"SELECT code, `date`, `close` FROM quant_hist_daily "
        f"WHERE code IN ({code_list}) AND `date` >= '{min_date}'", con=engine)
    hist["date"] = pd.to_datetime(hist["date"]).dt.strftime("%Y-%m-%d")
    # 喂表兜底:当日 K 线缺行时补 hist 最新交易日行(与 paper 同机制)
    spot, spot_day = spot_latest_closes(codes)
    if spot and spot_day:
        have = {(str(c), d) for c, d in zip(hist["code"], hist["date"])}
        extra = [{"code": c, "date": spot_day, "close": p}
                 for c, p in spot.items() if (c, spot_day) not in have]
        if extra:
            hist = pd.concat([hist, pd.DataFrame(extra)], ignore_index=True)
    per = {c: g.sort_values("date") for c, g in hist.groupby("code")}

    n1 = n5 = 0
    conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            for _, r in st.iterrows():
                g = per.get(r["code"])
                if g is None or r["price"] is None or pd.isna(r["price"]):
                    continue
                after = g[g["date"] > str(r["log_date"])]
                vals = {}
                for n in (1, 3, 5):
                    close = (float(after["close"].iloc[n - 1])
                             if len(after) >= n else None)
                    vals[f"close_t{n}"] = close
                    vals[f"win_t{n}"] = (None if close is None
                                         else int(close > r["price"]))
                n1 += int(vals["win_t1"] == 1)
                n5 += int(vals["win_t5"] == 1)
                cur.execute(
                    "UPDATE quant_strategy_trades SET close_t1=%s,close_t3=%s,"
                    "close_t5=%s,win_t1=%s,win_t5=%s WHERE id=%s",
                    (vals["close_t1"], vals["close_t3"], vals["close_t5"],
                     vals["win_t1"], vals["win_t5"], r["id"]))
        conn.commit()
    finally:
        conn.close()
    return n1, n5


def _method_stat(frame):
    """frame: 该战法已回填的记录;返回统计 dict。"""
    import math

    import pandas as pd

    if frame is None or frame.empty:
        return {"n": 0, "n_t1": 0, "win1": None, "win5": None,
                "avg_t1": None, "conf": None}
    done = frame[frame["win_t1"].notna()]
    w1 = done["win_t1"].mean() if len(done) else None
    d5 = frame[frame["win_t5"].notna()]
    w5 = d5["win_t5"].mean() if len(d5) else None
    if len(done):
        rets = (done["close_t1"] / done["price"] - 1.0).clip(-1, 10)
        avg_t1 = float(rets.mean())
        avg_t1 = None if math.isnan(avg_t1) else round(avg_t1 * 100, 2)
    else:
        avg_t1 = None
    conf_col = frame.get("confidence")
    conf = None
    if conf_col is not None:
        nums = pd.to_numeric(conf_col, errors="coerce").dropna()
        if len(nums):
            conf = round(float(nums.mean()), 1)
    return {
        "n": int(len(frame)),
        "n_t1": int(len(done)),
        "win1": None if w1 is None else round(float(w1) * 100, 1),
        "win5": None if w5 is None else round(float(w5) * 100, 1),
        "avg_t1": avg_t1,
        "conf": conf,
    }


def stats_summary(paper_hint=True):
    """每路战法的可行性统计(供战法页可行性卡)。

    paper_hint=True 时附 m3_push(M3+时段确认推送口径,来自
    quant_paper_trades,样本与战法页 top5 分开,不混算)。
    """
    import pandas as pd

    ensure_table()
    engine = qdb.get_engine()
    try:
        frame = pd.read_sql(
            "SELECT method,confidence,price,close_t1,win_t1,win_t5 "
            "FROM quant_strategy_trades", con=engine)
    except Exception as error:  # noqa: BLE001
        logger.warning("战法统计读取失败: %s", error)
        frame = pd.DataFrame()
    out = {key: _method_stat(frame[frame["method"] == key] if not frame.empty
                             else None) for key in METHOD_LABELS}
    out["since"] = None
    if not frame.empty:
        try:
            out["since"] = str(pd.read_sql(
                "SELECT MIN(log_date) d FROM quant_strategy_trades",
                con=engine)["d"].iloc[0])[:10]
        except Exception:  # noqa: BLE001
            pass
    if paper_hint:
        try:
            pt = pd.read_sql(
                "SELECT rec_price AS price, close_t1, win_t1, win_t5 "
                "FROM quant_paper_trades", con=engine)
            out["m3_push"] = _method_stat(pt)   # 无 confidence 列→均可信度显示 —
        except Exception as error:  # noqa: BLE001
            logger.warning("推送样本读取失败: %s", error)
    return out
