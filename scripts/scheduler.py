# -*- coding: utf-8 -*-
"""四时段调度器(launchd 按时刻触发,本脚本做全部智能判断)。

时段:竞价 09:20 / 盘中 10:00、11:00、13:05、14:00 / 尾盘 14:35 / 复盘 15:35
职责:
  1. 交易日判断(非交易日直接退出)——用 InStock 本地交易日历,无网络请求
  2. 按「当前时间落在哪个时段窗口」选择任务(休眠唤醒晚触发也只补跑窗口内的)
  3. 单时段文件锁,防 launchd 与其他触发源重复执行
  4. 盘中/尾盘:执行 holding_decision 并把精简版推微信;复盘:跑 daily_close.sh(日报内部自己推)
用法: python scripts/scheduler.py [--force-slot auction|intraday|tail|review]
"""

import argparse
import datetime as dt
import fcntl
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, "instock", ".venv", "bin", "python")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "instock"))

LOG = os.path.join(ROOT, "reports", "scheduler.log")
LOCKS = os.path.join(ROOT, "reports", ".locks")

SLOTS = [
    # (名称, 时:分, 容忍分钟, 命令, 是否推送stdout精简版)
    ("auction", (9, 20), 25, [PY, f"{ROOT}/scripts/auction_push.py"], False),
    ("spot", (10, 0), 40, [PY, f"{ROOT}/scripts/refresh_realtime.py"], False),
    ("intraday", (10, 0), 40, [PY, f"{ROOT}/scripts/holding_decision.py"], True),
    ("spot", (11, 0), 40, [PY, f"{ROOT}/scripts/refresh_realtime.py"], False),
    ("intraday", (11, 0), 40, [PY, f"{ROOT}/scripts/holding_decision.py"], True),
    ("spot", (13, 5), 40, [PY, f"{ROOT}/scripts/refresh_realtime.py"], False),
    ("intraday", (13, 5), 40, [PY, f"{ROOT}/scripts/holding_decision.py"], True),
    ("spot", (14, 0), 30, [PY, f"{ROOT}/scripts/refresh_realtime.py"], False),
    ("intraday", (14, 0), 30, [PY, f"{ROOT}/scripts/holding_decision.py"], True),
    ("tail", (14, 35), 20, [PY, f"{ROOT}/scripts/tail_pick.py"], False),
    ("sim", (14, 40), 30, [PY, f"{ROOT}/scripts/sim_engine.py"], False),
    ("review", (15, 35), 240, ["bash", f"{ROOT}/scripts/daily_close.sh"], False),
]


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    print(msg, file=sys.stderr)


def is_trading_day():
    try:
        from instock.lib.trade_time import is_trade_date
        return is_trade_date(dt.date.today())
    except Exception as e:  # noqa: BLE001
        log(f"交易日历异常({e}),按星期判断(周六日休)")
        return dt.date.today().weekday() < 5


def acquire_lock(name):
    os.makedirs(LOCKS, exist_ok=True)
    fh = open(os.path.join(LOCKS, f"{name}.lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh  # 保持引用,进程存活期间锁住
    except OSError:
        return None


def compact(text):
    """stdout → 微信精简版:保留标题行与关键数据行。"""
    prefix = ("- 实时:", "- 建议", "- 关键位:", "> 规则", "- ★", "- 备选:",
              "- 今日无", "- 入选", "- 消息面:", "- 资金口径", "- 竞价规则")
    keep = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            keep.append(s.lstrip("#> ").strip())
        elif s.startswith(prefix):
            keep.append(s)
    out = "\n".join(dict.fromkeys(keep))
    return out[:900]


def due_slots(now):
    """返回当前窗口内**全部**应执行的时段(spot 与 intraday 同时刻并行)。"""
    out = []
    for name, (h, m), tol, cmd, push in SLOTS:
        t0 = now.replace(hour=h, minute=m, second=0, microsecond=0)
        t1 = t0 + dt.timedelta(minutes=tol)
        if t0 <= now <= t1:
            out.append((name, (h, m), tol, cmd, push))
    return out


def run_slot(slot, now):
    name, (h, m), tol, cmd, push = slot
    lock = acquire_lock(name)
    if lock is None:
        log(f"[{name}] 已有实例在跑,跳过")
        return 1
    log(f"[{name}] 开始执行")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90 * 60,
                           cwd=ROOT)
        out = r.stdout or ""
        if r.returncode != 0:
            log(f"[{name}] 退出码 {r.returncode}: {(r.stderr or '')[-300:]}")
        if push and out:
            try:
                from quant.push import send_notice
                title = {"intraday": "盘中检查", "tail": "尾盘决策"}[name]
                ok = send_notice(f"{title} {now:%H:%M}", compact(out))
                log(f"[{name}] 推送 {'OK' if ok else 'FAIL'}")
            except Exception as e:  # noqa: BLE001
                log(f"[{name}] 推送异常: {e}")
        elif push:
            log(f"[{name}] 无输出,不推送")
        # 盘中/尾盘同时落盘完整版
        if push and out:
            day = now.strftime("%Y%m%d")
            os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
            with open(os.path.join(ROOT, "reports", f"slot_{name}_{day}.md"),
                      "w", encoding="utf-8") as f:
                f.write(out)
        return r.returncode
    finally:
        lock.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-slot", dest="force", default=None,
                    help="auction|intraday|tail|review,忽略时间窗口(测试用)")
    args = ap.parse_args()
    now = dt.datetime.now()

    if not is_trading_day():
        log("非交易日,跳过")
        return 0

    if args.force:
        for s in SLOTS:
            if s[0] == args.force:
                return run_slot(s, now)
        log(f"未知时段 {args.force}")
        return 2

    slots = due_slots(now)
    if not slots:
        log(f"当前 {now:%H:%M} 不在任何时段窗口,无任务")
        return 0
    ret = 0
    for slot in slots:
        ret = run_slot(slot, now) or ret
    return ret


if __name__ == "__main__":
    sys.exit(main())
