# -*- coding: utf-8 -*-
"""模拟盘记录(M7):推送过的荐股全部落表,追踪 T+N 表现,两周后统计胜率。

口径(规则化、可复核):
  rec_price    = 推荐时点的价格(竞价=竞价价;尾盘=尾盘推送时现价)
  close_tN     = 推荐日之后第 N 个交易日的收盘价(停牌则顺延到有K线的日子取第N日)
  win_t1       = close_t1 > rec_price ⟹ 该信号次日收盘"跑赢买入价"
  评分依据:全部来自 M3 输出(score10/evidence),记录写库时快照,防止后期改口径失忆。
数据源:quant_hist_daily(收盘价)——与全链同一份数据,无新依赖。
"""

import datetime as dt
import logging

from quant import db as qdb

logger = logging.getLogger("paper")

_CREATE = """
CREATE TABLE IF NOT EXISTS `quant_paper_trades` (
  `id`         BIGINT       AUTO_INCREMENT PRIMARY KEY,
  `push_date`  DATE         NOT NULL COMMENT '推荐日',
  `push_slot`  VARCHAR(16)  NOT NULL COMMENT 'auction/tail',
  `code`       VARCHAR(10)  NOT NULL,
  `name`       VARCHAR(32)  DEFAULT NULL,
  `horizon`    VARCHAR(16)  DEFAULT NULL,
  `grade`      VARCHAR(4)   DEFAULT NULL,
  `score10`    FLOAT        DEFAULT NULL COMMENT '推荐时快照(10分制)',
  `evidence`   VARCHAR(512) DEFAULT NULL COMMENT '选定依据快照',
  `rec_price`  FLOAT        DEFAULT NULL COMMENT '推荐时点价',
  `rec_basis`  VARCHAR(200) DEFAULT NULL COMMENT '推送时点的确认依据(如竞价量比)',
  `close_t0`   FLOAT        DEFAULT NULL COMMENT '推荐日收盘',
  `close_t1`   FLOAT        DEFAULT NULL COMMENT 'T+1收盘',
  `close_t3`   FLOAT        DEFAULT NULL COMMENT 'T+3收盘',
  `close_t5`   FLOAT        DEFAULT NULL COMMENT 'T+5收盘',
  `close_t10`  FLOAT        DEFAULT NULL COMMENT 'T+10收盘',
  `win_t1`     TINYINT      DEFAULT NULL COMMENT 'T+1收盘>推荐价=1',
  `win_t5`     TINYINT      DEFAULT NULL,
  `result_note` VARCHAR(64) DEFAULT NULL,
  UNIQUE KEY `uk` (`push_date`, `push_slot`, `code`),
  INDEX `idx_date` (`push_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模拟盘荐股记录与T+N追踪'
"""

_INSERT = ("REPLACE INTO quant_paper_trades "
           "(push_date,push_slot,code,name,horizon,grade,score10,evidence,"
           "rec_price,rec_basis) "
           "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")


def ensure_table(conn=None):
    own = conn is None
    if own:
        conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(_CREATE)
        conn.commit()
    finally:
        if own:
            conn.close()


def record(push_slot, code, name, horizon="", grade="", score10=None,
           evidence="", rec_price=None, rec_basis="", push_date=None):
    """记录一条推送荐股(收盘后入库,与推进时刻同价)。"""
    ensure_table()
    day = push_date or dt.date.today()
    conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(_INSERT, (day, push_slot, code, name, horizon, grade,
                                  score10, evidence[:500], rec_price,
                                  rec_basis[:200]))
        conn.commit()
    finally:
        conn.close()
    logger.info("模拟盘记录 %s %s %s rec_price=%s", day, push_slot, code, rec_price)


def spot_latest_closes(codes):
    """喂表最新日收盘价 {code: price}(收盘后喂表=当日收盘终值)。

    东财逐只断连/THS 快照限流时,当日 K 线可能大面积缺行——回填
    T+N 时用喂表行补 hist 帧缺失的最新交易日行,不必等隔日 dump
    (隔日 dump 补行后重跑得到同值,幂等覆盖)。兜底尽力而为:
    读不到返回 ({}, None),绝不影响主回填链。
    """
    if not codes:
        return {}, None
    try:
        conn = qdb.get_connection()
    except Exception:  # noqa: BLE001
        return {}, None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(`date`) FROM cn_stock_spot")
            row = cur.fetchone()
            day = str(row[0])[:10] if row and row[0] else None
            if not day:
                return {}, None
            cur.execute(
                "SELECT code, new_price FROM cn_stock_spot "
                "WHERE `date`=%s AND new_price IS NOT NULL", (day,))
            want = {str(c).zfill(6) for c in codes}
            out = {}
            for code, price in cur.fetchall():
                code = str(code).zfill(6)
                if code in want:
                    out[code] = float(price)
            return out, day
    except Exception as error:  # noqa: BLE001
        logger.warning("喂表兜底读取失败: %s", error)
        return {}, None
    finally:
        conn.close()


def update_tracking(conn=None):
    """按 quant_hist_daily 回填 T+N 追踪列(幂等,每天收盘链后可重跑)。

    conn 参数仅为兼容旧签名保留:读历史走 engine,写回另开连接。
    历史K线只加载模拟盘涉及的股票(quant_hist_daily 全表近千万行,
    不能整表拉进 pandas),并从最早推荐日起取数。
    """
    import re as _re
    import pandas as pd
    ensure_table()
    engine = qdb.get_engine()
    pt = pd.read_sql(
        "SELECT id, code, push_date, rec_price FROM quant_paper_trades", con=engine)
    if pt.empty:
        return 0, 0
    codes = sorted({str(c).strip() for c in pt["code"]})
    codes = [c for c in codes if _re.fullmatch(r"\d{6}", c)]
    if not codes:
        return 0, 0
    min_date = pd.to_datetime(pt["push_date"]).min().strftime("%Y-%m-%d")
    code_list = ",".join(f"'{c}'" for c in codes)
    hist = pd.read_sql(
        f"SELECT code, `date`, `close` FROM quant_hist_daily "
        f"WHERE code IN ({code_list}) AND `date` >= '{min_date}'",
        con=engine)
    hist["date"] = pd.to_datetime(hist["date"]).dt.strftime("%Y-%m-%d")
    # 喂表兜底:东财断连/THS 限流导致当日 K 线缺行时,用喂表收盘价
    # 补 hist 缺失的最新交易日行(close_t0/T+N 即时回填,隔日 dump
    # 补行后重跑同值覆盖,幂等)。
    spot, spot_day = spot_latest_closes(codes)
    if spot and spot_day:
        have = {(str(c), d) for c, d in zip(hist["code"], hist["date"])}
        extra = [{"code": c, "date": spot_day, "close": p}
                 for c, p in spot.items() if (c, spot_day) not in have]
        if extra:
            hist = pd.concat([hist, pd.DataFrame(extra)], ignore_index=True)
    per = {c: g.sort_values("date") for c, g in hist.groupby("code")}

    n1 = n5 = 0
    conn2 = qdb.get_connection()
    try:
        with conn2.cursor() as cur:
            for _, r in pt.iterrows():
                code = r["code"]
                g = per.get(code)
                if g is None or r["rec_price"] is None or pd.isna(r["rec_price"]):
                    continue
                after = g[g["date"] > str(r["push_date"])]
                cls = {}
                for n in (1, 3, 5, 10):
                    cls[n] = float(after["close"].iloc[n - 1]) if len(after) >= n else None
                vals = {}
                for n in (1, 3, 5, 10):
                    vals[f"close_t{n}"] = cls[n]
                    v = cls[n]
                    vals[f"win_t{n}"] = None if v is None else int(v > r["rec_price"])
                n1 += int(vals["win_t1"] == 1)
                n5 += int(vals["win_t5"] == 1)
                cur.execute(
                    "UPDATE quant_paper_trades SET close_t0=%s,close_t1=%s,close_t3=%s,"
                    "close_t5=%s,close_t10=%s,win_t1=%s,win_t5=%s WHERE id=%s",
                    (_close_t0(per, code, r["push_date"]), vals["close_t1"],
                     vals["close_t3"], vals["close_t5"], vals["close_t10"],
                     vals["win_t1"], vals["win_t5"], r["id"]))
        conn2.commit()
    finally:
        conn2.close()
    return n1, n5


def _close_t0(per, code, push_date):
    g = per.get(code)
    if g is None:
        return None
    hit = g[g["date"] == str(push_date)]
    return float(hit["close"].iloc[0]) if len(hit) else None


def stats_summary():
    """当前胜率统计(条件记录数≥10 才有参考意义)。"""
    import pandas as pd
    pt = pd.read_sql(
        "SELECT COUNT(*) n, AVG(win_t1) w1, AVG(win_t5) w5 FROM quant_paper_trades "
        "WHERE win_t1 IS NOT NULL", con=qdb.get_engine())
    r = pt.iloc[0]
    return int(r["n"]), (None if pd.isna(r["w1"]) else float(r["w1"])), \
        (None if pd.isna(r["w5"]) else float(r["w5"]))
