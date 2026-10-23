# -*- coding: utf-8 -*-
"""收盘链 1d 步:除权口径核验(通达信除权表 × 库内前复权 K 线)。

用法:
    python scripts/check_adjust.py                 # 清单默认:持仓+模拟盘+近7日战法/推送
    python scripts/check_adjust.py --days 14 --lookback 120
    python scripts/check_adjust.py --json          # 机器可读输出

输出:汇总一行 + suspicious 明细表(带「!! ADJUST-SUSPECT」标记,便于
收盘链日志检索);发现疑似断档时退出码仍为 0(核验结论,不是执行失败,
不阻断收盘链后续步骤)。结果落 quant_adjust_check(uk=check_date+code,
REPLACE 幂等,每日重跑安全)。
"""

import argparse
import json
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from quant.adjust_check import run_check  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="除权口径核验")
    parser.add_argument("--days", type=int, default=7,
                        help="战法候选/推送信号回看天数(默认 7)")
    parser.add_argument("--lookback", type=int, default=90,
                        help="除权事件回看天数(默认 90)")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s %(message)s")
    summary = run_check(days=args.days, lookback_days=args.lookback)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    print("除权核验:清单 %d 只 | ok %d | 无除权 %d | 无数据 %d | 疑似断档 %d"
          % (summary["total"], summary["ok"], summary["no_event"],
             summary["no_data"], summary["suspicious"]))
    if summary["suspects"]:
        print("!! ADJUST-SUSPECT 疑似未复权覆盖(需以东财 fqt=1 回补):")
        for s in summary["suspects"]:
            print("  %s %s | 除权日 %s 库内涨跌 %+.1f%% | %s"
                  % (s["code"], s["name"], s["ex_date"],
                     (s["jump"] or 0) * 100, s["ex_param"]))
    else:
        print("未发现疑似断档(小额现金分红约 3%% 以内可能漏检,见模块说明)。")


if __name__ == "__main__":
    main()
