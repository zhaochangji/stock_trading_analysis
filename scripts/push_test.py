# -*- coding: utf-8 -*-
"""推送通道测试:python scripts/push_test.py [--channel wechat_test|wxpusher]"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from quant.push import send_notice  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default=None, help="wechat_test / wxpusher,缺省用 config")
    args = ap.parse_args()
    ok = send_notice(
        "股票量化系统·通道测试",
        "**推送通道测试**\n\n如果你在微信里看到这条消息,说明通道已打通。\n\n"
        "- 来源:本地量化系统(MyFlicker)\n- 用途:每日四时段推送(竞价/盘中/尾盘/复盘)\n"
        "- 失败兜底:reports/push_inbox.md\n\n"
        "看到请回消息确认。", channel=args.channel)
    print("发送结果:", "成功" if ok else "失败(见 reports/push_failed.log)")
    sys.exit(0 if ok else 1)
