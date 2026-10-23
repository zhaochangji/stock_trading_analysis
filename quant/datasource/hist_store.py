# -*- coding: utf-8 -*-
"""历史K线落库(自研模块,InStock 不存K线,由本模块补齐)。

数据源:东财 push2his 历史K线接口(复用 InStock 爬虫 stock_zh_a_hist,前复权 qfq)。
目标表:quant_hist_daily(与 InStock 同库 instockdb,便于 JOIN)。
节奏控制:多线程 + 随机延迟 + 分批休整,避免触发东财风控(实测 push2 列表接口
在连续高频请求后会被临时拉黑,push2his 容忍度更高但仍需控制)。
"""

import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from quant import db as qdb
from quant.datasource.kline import fetch_kline_em

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `quant_hist_daily` (
  `code`       VARCHAR(6)  NOT NULL COMMENT '股票代码',
  `date`       DATE        NOT NULL COMMENT '交易日期',
  `open`       DOUBLE      COMMENT '开盘价(前复权)',
  `close`      DOUBLE      COMMENT '收盘价(前复权)',
  `high`       DOUBLE      COMMENT '最高价',
  `low`        DOUBLE      COMMENT '最低价',
  `volume`     BIGINT      COMMENT '成交量(股)',
  `amount`     DOUBLE      COMMENT '成交额(元)',
  `amplitude`  FLOAT       COMMENT '振幅%',
  `quote_change` FLOAT     COMMENT '涨跌幅%',
  `ups_downs`  FLOAT       COMMENT '涨跌额',
  `turnover`   FLOAT       COMMENT '换手率%',
  PRIMARY KEY (`code`, `date`),
  INDEX `idx_date` (`date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='A股日线历史(前复权,自研回补)'
"""

_INSERT_SQL = ("REPLACE INTO `quant_hist_daily` (`code`,`date`,`open`,`close`,`high`,`low`,"
               "`volume`,`amount`,`amplitude`,`quote_change`,`ups_downs`,`turnover`) "
               "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")


def ensure_table(conn=None):
    """创建 quant_hist_daily 表(幂等)。"""
    own = conn is None
    if own:
        conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE_SQL)
        conn.commit()
    finally:
        if own:
            conn.close()


def fetch_hist(code: str, start: str, end: str = None):
    """拉单只股票日线(东财 push2his 直连,前复权)。返回 list[tuple] 或 None。

    start/end 格式:YYYYMMDD
    """
    try:
        rows = fetch_kline_em(code, start, end)
        if rows is None:
            return None
        return [(code,) + tuple(r) for r in rows]
    except Exception as e:
        logger.warning(f"fetch_hist {code} 异常: {e}")
        return None


def insert_rows(rows, conn=None):
    """批量 REPLACE 入库。"""
    if not rows:
        return 0
    own = conn is None
    if own:
        conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            cur.executemany(_INSERT_SQL, rows)
        conn.commit()
        return len(rows)
    finally:
        if own:
            conn.close()


def backfill(stocks, start: str, end: str = None, workers: int = 8,
             batch_size: int = 50, pause_every: int = 500, pause_sec: float = 45.0,
             per_req_sleep: float = 0.25):
    """批量回补。

    stocks: [(date, code, name), ...](来自 stock_list)
    start:  YYYYMMDD
    workers: 并发线程数(控制对 push2his 的压力,默认 8)
    pause_every/pause_sec: 每抓 N 只休整,给接口降压
    返回 (成功股票数, 失败清单)
    """
    codes = [s[1] for s in stocks]
    total = len(codes)
    logger.info(f"回补开始: {total} 只, start={start}, workers={workers}")
    ensure_table()

    ok, failed = [], []
    buffer = []
    conn = qdb.get_connection()
    done = 0
    t0 = time.time()

    def _grab(code):
        time.sleep(per_req_sleep + random.uniform(0, 0.3))
        rows = fetch_hist(code, start, end)
        if rows is None:
            time.sleep(2 + random.uniform(0, 2))  # 失败歇一下再试一次
            rows = fetch_hist(code, start, end)
        return code, rows

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_grab, c): c for c in codes}
            for fu in as_completed(futures):
                code, rows = fu.result()
                if rows is None:
                    failed.append(code)
                else:
                    ok.append(code)
                    buffer.extend(rows)
                done += 1
                if len(buffer) >= batch_size * 60:  # 攒批入库
                    insert_rows(buffer, conn)
                    logger.info(f"已入库累计 {len(buffer)} 行")
                    buffer = []
                if done % 100 == 0:
                    speed = done / max(time.time() - t0, 1)
                    logger.info(f"进度 {done}/{total} ({speed:.1f} 只/秒),失败 {len(failed)}")
                if done % pause_every == 0 and done < total:
                    logger.info(f"休整 {pause_sec}s 降压…")
                    time.sleep(pause_sec)
        if buffer:
            insert_rows(buffer, conn)
    finally:
        conn.close()

    logger.info(f"回补完成: 成功 {len(ok)}/{total}, 失败 {len(failed)}")
    if failed:
        logger.warning(f"失败清单: {failed[:50]}{'…' if len(failed) > 50 else ''}")
    return ok, failed


def get_latest_date(engine=None) -> str:
    """表内最新日期(YYYYMMDD),用于增量模式。空表返回 None。"""
    import pandas as pd
    if engine is None:
        engine = qdb.get_engine()
    try:
        df = pd.read_sql("SELECT MAX(`date`) AS d FROM quant_hist_daily", con=engine)
        v = df.iloc[0]["d"]
        return None if v is None or v != v else str(v).replace("-", "")
    except Exception:
        return None


def get_stock_count(engine=None) -> int:
    if engine is None:
        engine = qdb.get_engine()
    try:
        with qdb.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(DISTINCT code) FROM quant_hist_daily")
                return cur.fetchone()[0]
    except Exception:
        return 0
