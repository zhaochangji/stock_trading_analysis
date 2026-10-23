# -*- coding: utf-8 -*-
"""盘中实时数据直灌(每盘中时段运行一次,约 15~25 秒)。

1. THS 全市场快照 → 更新 cn_stock_spot 当日行情字段(价格/涨跌/量额/振幅/换手/量比),
   InStock web 行情页即刻变成当日数据(财务/市值等静态字段不动);
2. THS 即时资金(全口径,5214 只)→ quant_flow_realtime_today 表(看板/确认用);
3. 持仓股补充东财主力实时(大单口径)写入同表,两种口径同表分列。

口径:volume 单位换算 手(THS 股 ÷100);换手 = 今日量(股)/自由流通股本;
量比 = 今日量(手) / 近 5 个已收盘交易日该股均量(仅计有成交日)。
收盘后 15:35 的 InStock 全量作业会最终覆盖,故盘中作业只覆写当日字段、失败尽力而为。
"""

import datetime as dt
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "instock"))

import pymysql  # noqa: E402
import yaml  # noqa: E402

from quant import db as qdb  # noqa: E402
from quant.datasource import ths  # noqa: E402
from quant.datasource.fundflow_realtime import (  # noqa: E402
    main_flow_today, ths_instant_flow)


def fetch_all_snapshot():
    items, offset = [], 0
    for _ in range(6):
        try:
            r = ths.prices_snapshot(limit=5000, offset=offset) or {}
            batch = r.get("item") or []
            items.extend(batch)
            total = r.get("total") or 0
            offset += len(batch)
            if not batch or (total and offset >= total):
                break
        except Exception:  # noqa: BLE001
            break
    return items


def prev5_avg_volume(engine):
    """近 5 个已收盘交易日(全市场共用的日期集)每只均量(手)。"""
    dfd = pd.read_sql(
        "SELECT DISTINCT `date` FROM quant_hist_daily WHERE `date` < CURDATE() "
        "AND volume > 0 ORDER BY `date` DESC LIMIT 5", con=engine)
    if dfd.empty:
        return {}
    dates = tuple(d.strftime("%Y-%m-%d") for d in dfd["date"])
    df = pd.read_sql(
        f"SELECT code, AVG(volume) v FROM quant_hist_daily WHERE `date` IN {dates} "
        "AND volume > 0 GROUP BY code", con=engine)
    return dict(zip(df["code"], df["v"]))


def to_int(v):
    try:
        if v is None or pd.isna(v):
            return None
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def main():
    engine = qdb.get_engine()
    conn = pymysql.connect(host="localhost", user="root", password="root",
                           database="instockdb")
    cur = conn.cursor()
    today = dt.date.today().isoformat()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ---- ① 行情快照 → cn_stock_spot(当日字段) ----
    snaps = fetch_all_snapshot()
    if len(snaps) < 3000:
        print(f"快照不足({len(snaps)}),放弃本次刷新 — {now}")
        return
    avg_v = prev5_avg_volume(engine)
    cur.execute("SELECT code, free_shares FROM cn_stock_spot")
    free = {r[0]: int(r[1] or 0) for r in cur.fetchall()}

    upd = ("UPDATE cn_stock_spot SET `date`=%s, new_price=%s, change_rate=%s, "
           "ups_downs=%s, volume=%s, deal_amount=%s, amplitude=%s, turnoverrate=%s, "
           "volume_ratio=%s, open_price=%s, high_price=%s, low_price=%s, "
           "pre_close_price=%s WHERE code=%s")
    param_list, n_skip = [], 0
    for it in snaps:
        code = (it.get("thscode") or "").split(".")[0]
        price, prev = it.get("last_price"), it.get("prev_price")
        vol_shares = it.get("volume") or 0
        if not code or not price or not prev or vol_shares <= 0:
            n_skip += 1  # 无行情/停牌:不动旧行
            continue
        vol_hand = round(vol_shares / 100)
        amt = it.get("turnover") or 0
        high, low = it.get("high_price") or price, it.get("low_price") or price
        amp = round((high - low) / prev * 100, 2)
        fs = free.get(code) or 0
        to = round(vol_shares / fs * 100, 2) if fs > 0 else None
        vm = avg_v.get(code)
        # quant_hist_daily.volume 为"股",avg_v 也是股;vol_hand 是手 → 手×100=股
        vr = round(vol_hand * 100 / vm, 2) if (vm and vm > 0) else None
        param_list.append((today, price, it.get("price_change_ratio_pct"),
                           it.get("price_change"), vol_hand, amt, amp, to, vr,
                           it.get("open_price"), high, low, prev, code))
    cur.executemany(upd, param_list)
    n_spot = cur.rowcount
    conn.commit()

    # ---- ② THS 即时资金(全口径) → quant_flow_realtime_today ----
    cur.execute(
        "CREATE TABLE IF NOT EXISTS quant_flow_realtime_today ("
        "`code` VARCHAR(10) NOT NULL PRIMARY KEY, `date` DATE, `name` VARCHAR(32),"
        "new_price FLOAT, change_rate FLOAT, net BIGINT, in_amt BIGINT, out_amt BIGINT,"
        "amount BIGINT, main_net BIGINT NULL, main_caliber VARCHAR(24) NULL,"
        "updated_at DATETIME) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
    conn.commit()
    tid = ths_instant_flow()
    n_flow = 0
    if tid is not None and not tid.empty:
        data = [(r["code"], today, r["name"], r["price"], r["pct"],
                 to_int(r["net"]), to_int(r["in"]), to_int(r["out"]),
                 to_int(r["amount"]), now) for _, r in tid.iterrows()]
        cur.executemany(
            "REPLACE INTO quant_flow_realtime_today "
            "(code,`date`,name,new_price,change_rate,net,in_amt,out_amt,amount,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", data)
        n_flow = cur.rowcount
        conn.commit()

    # ---- ③ 持仓主力实时(东财大单口径)同表补充 ----
    with open(os.path.join(ROOT, "config", "config.yaml"), encoding="utf-8") as f:
        holds = (yaml.safe_load(f) or {}).get("holdings") or []
    for h in holds:
        try:
            mf = main_flow_today(h["code"])
            if mf and mf.get("is_today"):
                cur.execute(
                    "UPDATE quant_flow_realtime_today SET main_net=%s, main_caliber=%s,"
                    " updated_at=%s WHERE code=%s",
                    (to_int(mf["main_net"]),
                     f"东财大单@{(mf.get('last_time') or '')[11:16]}", now, h["code"]))
        except Exception as e:  # noqa: BLE001
            print(f"持仓主力失败 {h['code']}: {e}", file=sys.stderr)
    conn.commit()
    conn.close()
    # 顺带预热 THS 即时资金缓存(约 11 秒,一次全市场):
    # 战法页 payload / live 候选 / 分析页只读缓存展示当日净额,
    # 不在单股请求里发起全市场资金抓取(AGENTS 约束)。
    try:
        from quant.datasource.fundflow_realtime import ths_instant_flow
        flow = ths_instant_flow()
        n_flow_pre = 0 if flow is None else len(flow)
        print(f"资金缓存预热: {n_flow_pre} 只")
    except Exception as e:  # noqa: BLE001
        print(f"资金缓存预热失败(不影响喂表): {e}", file=sys.stderr)
    print(f"实时刷新完成:快照 {len(snaps)} 条 | spot 更新 {n_spot} 行(停牌跳过 {n_skip}) "
          f"| 资金表 {n_flow} 行 — {now}")


if __name__ == "__main__":
    main()
