# -*- coding: utf-8 -*-
"""买卖确认规则(单一事实源,P1 收敛:竞价与尾盘共用,一处修改两处生效)。

全部为可视化阈值,推送与页面会引用这些说明文本。
"""

AUCTION = {
    "pct": (0.0, 7.0),        # 温和高开 0~7%(不追一字/不接大幅高开)
    "volume_ratio": 1.2,      # 竞价量比 ≥1.2(放量确认)
}
TAIL = {
    "pct": (0.0, 7.0),        # 当日涨幅 0~7%(未一字/未拉高出货)
    "prev_close_guard": 0.98, # 未破昨收 2% 以上
    "amount": 1e8,            # 实时成交额 ≥1 亿(活跃)
    "net": 0.0,               # THS 即时净额 ≥0(非资金净流出)
}


def confirm_auction(pct, volume_ratio):
    """竞价确认:(涨幅, 竞价量比) → (bool, 原因)。"""
    lo, hi = AUCTION["pct"]
    if pct is None or not (lo < pct <= hi):
        return False, f"高开幅度 {pct if pct is not None else '?'} 不在 ({lo}~{hi}]"
    if volume_ratio < AUCTION["volume_ratio"]:
        return False, f"竞价量比 {volume_ratio:.1f} < {AUCTION['volume_ratio']}"
    return True, f"温和高开 {pct:+.1f}% + 竞价量比 {volume_ratio:.1f}"


def confirm_tail(pct, price, prev_close, amount, net):
    """尾盘确认:(涨幅, 现价, 昨收, 实时成交额, THS即时净额) → (bool, 原因)。"""
    lo, hi = TAIL["pct"]
    if pct is None or not (lo < pct <= hi):
        return False, f"当日涨幅 {pct if pct is not None else '?'} 不在 ({lo}~{hi}]"
    if price < prev_close * TAIL["prev_close_guard"]:
        return False, f"现价 {price:.2f} 破昨收 {prev_close:.2f} 2% 以上"
    if amount < TAIL["amount"]:
        return False, f"实时成交额 {amount / 1e8:.1f} 亿 < 1"
    if net is None:
        return True, "THS即时净额缺失(不剔,软条件)"
    if net < TAIL["net"]:
        return False, f"THS即时净额 {net / 1e8:+.2f} 亿 < 0(防拉高出货)"
    return True, f"涨幅 {pct:+.1f}% + 未破位 + 额 {amount / 1e8:.1f}亿 + 净额 {net / 1e8:+.2f}亿"
