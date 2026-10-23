# -*- coding: utf-8 -*-
"""刷新 THS 行业指数成分映射表(quant_ths_membership)。

用途:板块层(THS 优先)需要「个股 → 同花顺行业指数」的归属映射。
数据:THS 行业指数目录(320 个,含一二三级行业)+ 逐指数成分股;
     并发 8 约 30~60 秒。幂等:REPLACE 按 (code, index_code)。
调度:每日复盘链中执行一次(daily_close.sh),周末无行情也可跑。
"""

import datetime as dt
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

STOCK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, STOCK_ROOT)

from quant import db as qdb  # noqa: E402
from quant.datasource import ths  # noqa: E402


def refresh():
    d = ths.ths_index_list("industry") or {}
    idx = d.get("item") or []
    print(f"THS 行业指数 {len(idx)} 个,抓成分股…", file=sys.stderr)

    def grab(it):
        code, name = it["thscode"], it["name"]
        try:
            dd = ths.ths_index_constituents(code) or {}
            cons = dd.get("item") or []
            return [(x.get("ticker", (x.get("thscode") or "").split(".")[0]), code, name)
                    for x in cons]
        except Exception:  # noqa: BLE001
            return []

    pairs = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for rows in ex.map(grab, idx):
            for c, ic, iname in rows:
                pairs.setdefault(c, []).append((ic, iname))
    today = dt.date.today().isoformat()
    conn = qdb.get_connection()
    data = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS quant_ths_membership ("
                "code VARCHAR(10) NOT NULL, index_code VARCHAR(16) NOT NULL,"
                "index_name VARCHAR(64) NOT NULL, updated DATE NOT NULL,"
                "PRIMARY KEY (code, index_code)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
            for c, lst in pairs.items():
                for ic, iname in lst:
                    data.append((c, ic, iname, today))
            cur.executemany(
                "REPLACE INTO quant_ths_membership "
                "(code,index_code,index_name,updated) VALUES (%s,%s,%s,%s)", data)
        conn.commit()
    finally:
        conn.close()
    print(f"quant_ths_membership 写入 {len(data)} 行,覆盖 {len(pairs)} 只({today})",
          file=sys.stderr)


if __name__ == "__main__":
    refresh()
