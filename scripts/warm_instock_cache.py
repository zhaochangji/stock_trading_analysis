# -*- coding: utf-8 -*-
"""K线缓存预热器:用 quant_hist_daily 直灌 InStock 的文件缓存。

背景:InStock 缓存目录按「3年窗口起点」命名,起点每天变 → 缓存天生只活一天,
     每天全链 ~2.3 万次网络抓取(5 个作业 × 4594 只)。本工具把当天的缓存从
     本地 MySQL 直接灌好,作业启动时秒读缓存,零网络请求。
格式(与 stock_hist_cache 写入严格一致):
     RangeIndex / 11列 date(str YYYY-MM-DD)+10数值列 / volume 单位=手(int) / gzip pickle
     列名顺序 = tablestructure.CN_STOCK_HIST_DATA['columns'] 的键序
路径: instock/instock/cache/hist/<YYYYMM>/<date_start>/<code>qfq.gzip.pickle
     (stockfetch.py: cpath_current=dirname(dirname(__file__))=instock/instock)
     date_start = 作业日期 - 365*3 天(get_trade_hist_interval 同规则)
已存在的缓存文件不覆盖(InStock 自己写的优先)。
用法: python scripts/warm_instock_cache.py [--date 2026-10-13] [--workers 8]
"""

import argparse
import datetime as dt
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "instock"))  # 读取 InStock 列定义

import quant.db as qdb  # noqa: E402

CACHE_ROOT = os.path.join(ROOT, "instock", "instock", "cache", "hist")
COLS = ["date", "open", "close", "high", "low", "volume", "amount",
        "amplitude", "quote_change", "ups_downs", "turnover"]


def date_start_of(job_date: str) -> str:
    """与 instock get_trade_hist_interval 完全同规则的 3 年回溯。"""
    d = dt.datetime.strptime(job_date, "%Y-%m-%d")
    return (d + dt.timedelta(days=-(365 * 3))).strftime("%Y%m%d")


def cleanup(job_date, ds, keep_days=5):
    """删除过期 date_start 缓存目录(目录名每天前移,旧目录永不再命中;InStock 自身不清理)。"""
    import shutil
    d = dt.datetime.strptime(job_date, "%Y-%m-%d")
    cutoff = (d + dt.timedelta(days=-(365 * 3) - keep_days)).strftime("%Y%m%d")
    removed = skipped = 0
    if not os.path.isdir(CACHE_ROOT):
        return
    for month in os.listdir(CACHE_ROOT):
        mdir = os.path.join(CACHE_ROOT, month)
        if not os.path.isdir(mdir):
            continue
        for day in os.listdir(mdir):
            ddir = os.path.join(mdir, day)
            if os.path.isdir(ddir) and day.isdigit() and day < cutoff:
                shutil.rmtree(ddir, ignore_errors=True)
                removed += 1
        if not os.listdir(mdir):
            os.rmdir(mdir)
    print(f"清理:删除 {removed} 个过期目录(阈值 {cutoff})", file=sys.stderr)


def warm(job_date, workers=8, only_codes=None):
    ds = date_start_of(job_date)
    cache_dir = os.path.join(CACHE_ROOT, ds[:6], ds)
    os.makedirs(cache_dir, exist_ok=True)

    sql = ("SELECT code,`date`,`open`,`close`,`high`,`low`,`volume`,`amount`,"
           "`amplitude`,`quote_change`,`ups_downs`,`turnover` "
           "FROM quant_hist_daily WHERE `date` >= %s AND `date` <= %s ORDER BY code,`date`")
    eng = qdb.get_engine()
    print(f"读取 MySQL({ds} ~ {job_date})…", file=sys.stderr)
    hist = pd.read_sql(sql, con=eng, params=(ds, job_date))
    if hist.empty:
        print("MySQL 无该区间数据", file=sys.stderr)
        return
    # 转成缓存格式:volume 股→手(int),date→字符串
    hist["volume"] = (hist["volume"] / 100).round().astype("int64")
    hist["date"] = hist["date"].astype(str)

    def _write(code_g):
        code, g = code_g
        out = os.path.join(cache_dir, f"{code}qfq.gzip.pickle")
        if os.path.exists(out):
            return code, "skip"
        g = g[COLS].reset_index(drop=True)
        try:
            g.to_pickle(out, compression="gzip")
            return code, "ok"
        except Exception as e:  # noqa: BLE001
            return code, f"ERR {e}"

    groups = [(c, g) for c, g in hist.groupby("code", sort=False)
              if only_codes is None or c in only_codes]
    print(f"预热 {len(groups)} 只 → {cache_dir}", file=sys.stderr)
    ok = skip = err = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for code, r in ex.map(_write, groups):
            if r == "ok":
                ok += 1
            elif r == "skip":
                skip += 1
            else:
                err += 1
                print(f"  {code}: {r}", file=sys.stderr)
    print(f"完成:写入 {ok},已存在跳过 {skip},失败 {err}", file=sys.stderr)
    if only_codes is None:
        cleanup(job_date, ds)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().isoformat(), help="作业日期(默认今天)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--codes", default=None, help="仅预热指定代码(逗号分隔,测试用)")
    a = ap.parse_args()
    warm(a.date, a.workers, set(a.codes.split(",")) if a.codes else None)
