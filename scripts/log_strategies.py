# -*- coding: utf-8 -*-
"""收盘链战法追踪步骤:三路战法候选落库 + 回填历史 T+N 胜负。

每交易日收盘后执行一次(daily_close.sh 第 4c 步):
  1. log_today:取战法页当前(收盘时点)混合榜,按战法归属拆 top5 落库;
  2. update_tracking:用已完成的 K 线回填全部记录的 T+1/T+3/T+5 与胜负。

样本从记录日起前向累积;战法页可行性卡按此统计,样本 <10 只时
页面明确标注「样本不足,仅供参考」,不硬凑结论。
用法: python scripts/log_strategies.py [--no-log | --no-update]
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (ROOT, os.path.join(ROOT, "scripts"), os.path.join(ROOT, "instock")):
    if path not in sys.path:
        sys.path.insert(0, path)

import quant.strategy_log as slog  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-log", action="store_true",
                        help="只回填,不采集今日候选")
    parser.add_argument("--no-update", action="store_true",
                        help="只采集,不回填历史")
    args = parser.parse_args()

    if not args.no_log:
        import time

        import scripts.quant_web as qw
        # 新进程缓存为空:首次调用只触发后台刷新,轮询等待因子就绪
        # (scan ~5s / leader ~20s / 板块成分),最多 ~90 秒,超时如实退出。
        payload = {}
        for _ in range(18):
            payload = qw._strategy_payload()
            if not payload.get("loading"):
                break
            time.sleep(5)
        if payload.get("loading"):
            print("!! 战法因子尚未就绪(scan/leader 均空),本次不落库")
        else:
            # 连板股不给现价:取当日喂表收盘价作 T+N 胜负基准(连板次日溢价口径)
            spot = {}
            try:
                import quant.db as qdb
                conn = qdb.get_connection()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT code,new_price FROM cn_stock_spot WHERE "
                            "`date`=(SELECT MAX(`date`) FROM cn_stock_spot)")
                        spot = {str(c): p for c, p in cur.fetchall() if p}
                finally:
                    conn.close()
            except Exception as error:  # noqa: BLE001
                print(f"!! 当日喂表价读取失败({type(error).__name__}),"
                      "连板股无法落库")
            counts = slog.log_today(payload.get("rows") or [],
                                    spot_prices=spot)
            print(f"- 战法追踪落库: {counts}")
    if not args.no_update:
        n1, n5 = slog.update_tracking()
        print(f"- T+N 回填完成:T+1 胜 {n1} / T+5 胜 {n5}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
