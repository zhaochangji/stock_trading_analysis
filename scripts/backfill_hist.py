#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史K线回补入口。

回补链(三层,自动降级):THS 全市场快照 → THS dump 批量文件(近10日,
仅大批量限流或快照失败时启用)→ 东财逐只(兜底,前复权精确口径;失败
清单再由 dump 尾救一轮)。dump 为未复权,只插缺失行不覆盖已有行。

用法(用 InStock 的 venv 运行):
  python scripts/backfill_hist.py                          # 全市场,默认 20210101 至今
  python scripts/backfill_hist.py --start 20230101         # 指定起点
  python scripts/backfill_hist.py --incremental            # 增量(THS快照→dump→东财)
  python scripts/backfill_hist.py --incremental --no-ths  # 增量(纯东财逐只+dump尾救)
  python scripts/backfill_hist.py --codes 002636,603232    # 只回补指定代码(小样本验证)
  python scripts/backfill_hist.py --workers 6             # 调节并发
"""

import argparse
import logging
import os
import sys

STOCK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, STOCK_ROOT)          # quant 包(自包含,不依赖 InStock 代码)

import quant.db as qdb
from quant.datasource.stock_list import get_stock_list
from quant.datasource.hist_store import (backfill, ensure_table, get_latest_date,
                                          get_stock_count)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("backfill")


def _dump_rescue_failed(failed, log):
    """东财逐只失败清单的 dump 尾救:救不回的如实留在失败清单。"""
    if not failed:
        return failed
    from quant.datasource.ths_dump import rescue_failed

    remaining = rescue_failed(list(failed), logger=log)
    if len(remaining) < len(failed):
        log.info("dump 尾救救回 %d 只,仍失败 %d 只",
                 len(failed) - len(remaining), len(remaining))
    return remaining


def _final_summary():
    import pandas as pd
    n = get_stock_count()
    df = pd.read_sql("SELECT MIN(`date`) a, MAX(`date`) b, COUNT(*) c FROM quant_hist_daily",
                     con=qdb.get_engine())
    log.info("=== 增量汇总:库内共 %d 只股票 / %s 行,日期范围 %s ~ %s",
             n, int(df.iloc[0]["c"]), df.iloc[0]["a"], df.iloc[0]["b"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20210101", help="起始日期 YYYYMMDD(默认 20210101)")
    ap.add_argument("--end", default=None, help="结束日期 YYYYMMDD(默认至今)")
    ap.add_argument("--codes", default=None, help="逗号分隔的代码列表(小样本模式)")
    ap.add_argument("--incremental", action="store_true",
                    help="增量模式:优先 THS 快照高速通道,除权/新股自动东财补抓")
    ap.add_argument("--no-ths", action="store_true", help="增量模式禁用 THS 快照,纯东财逐只(兜底)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--resume", action="store_true",
                    help="跳过已回补至最近交易日(>=2026-10-09)的股票,只补缺")
    args = ap.parse_args()

    ensure_table()

    start = args.start
    if args.incremental:
        latest = get_latest_date()
        if latest is None:
            log.info("表为空,增量模式退化为全量(默认起点 %s)", start)
        else:
            import datetime as dt
            d = dt.datetime.strptime(latest, "%Y%m%d").date() - dt.timedelta(days=5)
            start = d.strftime("%Y%m%d")
            log.info("增量模式:从 %s 起回补", start)

    # 股票清单:优先 InStock 库(catalog 来自 cn_stock_spot),否则新浪备用源
    stocks, src = get_stock_list(engine=qdb.get_engine())
    if not stocks:
        log.error("无法获取股票清单(cn_stock_spot 未建且新浪拉取失败),退出。")
        sys.exit(1)
    log.info("股票清单 %d 只(来源: %s)", len(stocks), src)

    # ---- THS 快照高速增量(收盘后全市场一次拉齐;除权/新股仍走东财保证前复权一致) ----
    if args.incremental and not args.no_ths:
        import datetime as dt
        from quant.datasource.ths_incremental import fast_incremental
        from quant.datasource.ths_dump import rescue_with_dump
        written, fb, stats = fast_incremental()
        if not stats.get("reason"):
            if not fb:
                log.info("=== THS 增量完成:%d 行,0 只需东财补抓", written)
                _final_summary()
                sys.exit(0)
            fset = set(fb)
            # 第二层 THS dump:fallback 大批量(>300,THS 限流特征)先批量补缺失日,
            # 只有真除权/新股(≤300)直接走东财,保持前复权精确口径。
            # target_date=今日(收盘链 15:35 后跑,目标即当日终值);dump 当日数据
            # 隔天才进文件,latest<今日时全量保留给东财,防止当日断档被误判已补齐。
            fset = set(rescue_with_dump(sorted(fset), target_date=dt.date.today(),
                                        logger=log))
            stocks = [s for s in stocks if s[1] in fset]
            if not stocks:
                log.info("=== THS 增量完成:%d 行;fallback %d 只已补齐(dump/不在清单忽略),无需东财",
                         written, len(fb))
                _final_summary()
                sys.exit(0)
            latest = get_latest_date()
            d = dt.datetime.strptime(latest, "%Y%m%d").date() - dt.timedelta(days=5)
            log.info("THS 主通道写入 %d 行;剩余 %d 只走东财(除权/新股/dump 未覆盖):%s",
                     written, len(stocks), ",".join(s[1] for s in stocks[:30]))
            ok, failed = backfill(stocks, start=d.strftime("%Y%m%d"), end=args.end,
                                 workers=args.workers)
            failed = _dump_rescue_failed(failed, log)
            _final_summary()
            sys.exit(0 if not failed else 1)
        log.warning("THS 快速通道不可用(%s),先尝试 dump 批量层再回退东财逐只",
                    stats.get("reason"))
        codes_now = [s[1] for s in stocks]
        codes_now = rescue_with_dump(codes_now, threshold=0,
                                     target_date=dt.date.today(), logger=log)
        if not codes_now:
            log.info("=== dump 批量层已补齐全市场缺失日,无需东财")
            _final_summary()
            sys.exit(0)
        kept = set(codes_now)
        stocks = [s for s in stocks if s[1] in kept]

    if args.resume:
        import pandas as pd
        try:
            df_done = pd.read_sql(
                "SELECT DISTINCT code FROM quant_hist_daily WHERE `date` >= '2026-10-09'",
                con=qdb.get_engine())
            done = set(df_done["code"])
            stocks = [s for s in stocks if s[1] not in done]
            log.info("resume: 跳过已完成的 %d 只,待补 %d 只", len(done), len(stocks))
        except Exception as e:
            log.warning("resume 判断失败(%s),将全量回补", e)

    if args.codes:
        want = set(args.codes.split(","))
        stocks = [s for s in stocks if s[1] in want]
        log.info("小样本模式:只回补 %s", [s[1] for s in stocks])

    if not stocks:
        log.error("过滤后无股票可回补,退出。")
        sys.exit(1)

    ok, failed = backfill(stocks, start=start, end=args.end, workers=args.workers)
    failed = _dump_rescue_failed(failed, log)

    # 汇总
    n = get_stock_count()
    import pandas as pd
    df = pd.read_sql("SELECT MIN(`date`) a, MAX(`date`) b, COUNT(*) c FROM quant_hist_daily",
                     con=qdb.get_engine())
    log.info("=== 回补汇总:本次成功 %d 只,失败 %d 只;库内共 %d 只股票 / %s 行,日期范围 %s ~ %s",
             len(ok), len(failed), n, int(df.iloc[0]["c"]),
             df.iloc[0]["a"], df.iloc[0]["b"])
    if failed:
        log.warning("失败代码(%d): %s", len(failed), ",".join(failed[:100]))


if __name__ == "__main__":
    main()
