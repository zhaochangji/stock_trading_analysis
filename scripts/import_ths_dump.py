#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""THS market-dump Parquet 入库。

日期口径(修正版):date_ms 为交易日的 Asia/Shanghai 零点;
本机时区即 +08,fromtimestamp 直接得到正确交易日,无需任何偏移。
(此前误判"自然日+1错位"实为 pandas to_datetime 按 UTC 渲染造成的视觉偏移。)
表:quant_hist_daily_ths(THS 不复权原始,存档/对照/备份源)
"""

import os
import sys
import time

STOCK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, STOCK_ROOT)

import pyarrow.parquet as pq
from quant import db as qdb

PARQUET = os.path.join(STOCK_ROOT, "data", "ths_daily-k.parquet")

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS `quant_hist_daily_ths` (
  `thscode`   VARCHAR(10) NOT NULL COMMENT '同花顺完整代码',
  `date`      DATE        NOT NULL COMMENT '交易日(date_ms-1天修正)',
  `open`      DOUBLE, `high` DOUBLE, `low` DOUBLE, `close` DOUBLE,
  `volume`    BIGINT   COMMENT '成交量(股)',
  `turnover`  DOUBLE   COMMENT '成交额(元)',
  PRIMARY KEY (`thscode`, `date`),
  INDEX `idx_date` (`date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='A股日线(THS dump,不复权,10年)'
"""

BATCH = 100_000


def main():
    conn = qdb.get_connection()
    with conn.cursor() as cur:
        cur.execute(CREATE_SQL)
    conn.commit()

    pf = pq.ParquetFile(PARQUET)
    total_rows = pf.metadata.num_rows
    print(f"parquet 总行数: {total_rows:,}", flush=True)

    t0 = time.time()
    done = 0
    ins = ("REPLACE INTO quant_hist_daily_ths (thscode, `date`, `open`, `high`, `low`, "
           "`close`, volume, turnover) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)")
    for batch in pf.iter_batches(batch_size=BATCH, columns=[
            "thscode", "date_ms", "open_price", "high_price", "low_price",
            "close_price", "volume", "turnover"]):
        rows = []
        for r in batch.to_pylist():
            import datetime as dt
            d = dt.datetime.fromtimestamp(r["date_ms"] / 1000).date()
            rows.append((r["thscode"], d, r["open_price"], r["high_price"],
                         r["low_price"], r["close_price"],
                         int(r["volume"]) if r["volume"] is not None else None,
                         r["turnover"]))
        with conn.cursor() as cur:
            cur.executemany(ins, rows)
        conn.commit()
        done += len(rows)
        if (done // BATCH) % 10 == 0 and done % BATCH == 0:
            print(f"  已入库 {done:,}/{total_rows:,} ({time.time()-t0:.0f}s)", flush=True)
    print(f"完成: {done:,} 行, 耗时 {time.time()-t0:.0f}s", flush=True)

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(DISTINCT thscode) s, COUNT(*) r, MIN(`date`) a, MAX(`date`) b "
                    "FROM quant_hist_daily_ths")
        s, r, a, b = cur.fetchone()
    print(f"库内: {s:,} 只 / {r:,} 行 / {a} ~ {b}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
