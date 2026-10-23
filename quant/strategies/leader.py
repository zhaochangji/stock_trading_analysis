# -*- coding: utf-8 -*-
"""龙头战法(数据驱动 v1):连板梯队 → 主线题材 → 龙头唯一性识别。

数据源(同花顺扶摇,已验证):limit_up_pool(涨停池:题材/连板数/涨停时间/封单)、
limit_break_pool(炸板池:开板次数)。

战法框架(来源:龙头战法公开心法综述——复盘网/技术博客等,规则化转译,
胜率数字为经验值未经我们回测验证,如实标注):
  1. 只做龙头,不做跟风——辨识度 = 板块内涨停时间最早 + 封单最大
  2. 低位(1-2板):关注 弱转强/分歧回封;二板转一致是经典买点
  3. 中位(3-4板):只做分歧转一致
  4. 高标(≥5板):只看不追,等首阴反包(仅第一次)
  5. 断板/炸板:离场警示;龙头专属纪律:破5日线减仓,破10日线清仓
  6. 板块梯队完整(题材内多股涨停)才叫主线;孤板不算

风险如实:龙头战法属于高波动短线打法,胜率依赖执行纪律,本模块只做
识别与提示,不构成投资建议。
"""

import datetime as dt

from quant.datasource import ths

MAX_WATCH_BOARDS = 5  # ≥5板为高标,只看不追


def _reason_tags(reason):
    """涨停原因串 → 题材标签列表('水产饲料+鱼粉涨价+青岛国资' → [...])。"""
    if not reason:
        return ["未知"]
    return [t.strip() for t in str(reason).replace("、", "+").split("+") if t.strip()]


def leader_scan():
    """扫描今日涨停池 → 主线题材/龙头/梯队/战法提示。

    返回 dict:
      theme 主线题材, height 主线最高连板, count 主线家数,
      leader {code,name,boards,reason,state,seal_yi,limit_time},
      ladder [{code,name,boards,reason,state,seal_yi}] (连板≥2,按高度排序),
      hints [战法提示], stats {up_cnt, break_cnt}
    失败/无数据返回 None(调用方如实显示)。
    """
    try:
        d = ths.limit_up_pool() or {}
        items = d.get("item") or []
        brk = ths.limit_break_pool() or {}
        break_codes = {(i.get("thscode") or "").split(".")[0] for i in (brk.get("item") or [])}
    except Exception:  # noqa: BLE001
        return None
    if not items:
        return {"theme": None, "height": 0, "count": 0, "leader": None,
                "ladder": [], "hints": [], "stats": {"up_cnt": 0, "break_cnt": len(break_codes)}}

    # ---- 题材聚合:标签 → 最高连板/家数 ----
    tag_stat = {}
    for it in items:
        boards = int(it.get("continue_day_cnt") or 1)
        for tag in _reason_tags(it.get("limit_up_reason")):
            s = tag_stat.setdefault(tag, {"height": 0, "count": 0})
            s["height"] = max(s["height"], boards)
            s["count"] += 1
    # 主线题材:高度优先,家数次之(孤板不算主线的原则体现在排序)
    theme, stat = max(tag_stat.items(),
                      key=lambda kv: (kv[1]["height"], kv[1]["count"]))
    # 主线题材内的股票(题材标签命中),取连板最高者做龙头
    theme_items = [i for i in items
                   if theme in _reason_tags(i.get("limit_up_reason"))]
    theme_items.sort(key=lambda i: (-(i.get("continue_day_cnt") or 1),
                                    i.get("limit_up_time") or "99:99",
                                    -(i.get("max_seal_money") or 0)))
    lead = theme_items[0] if theme_items else None

    def _state(it):
        code = (it.get("thscode") or "").split(".")[0]
        return "炸板(分歧)" if code in break_codes else "封板"

    def _pack(it):
        code = (it.get("thscode") or "").split(".")[0]
        return {"code": code, "name": it.get("name", ""),
                "boards": int(it.get("continue_day_cnt") or 1),
                "reason": it.get("limit_up_reason", ""),
                "state": _state(it),
                "seal_yi": round((it.get("seal_money") or 0) / 1e8, 2),
                "limit_time": it.get("limit_up_time", ""),
                "pct": it.get("price_change_ratio_pct")}

    leader = _pack(lead) if lead else None
    ladder = [_pack(i) for i in sorted(items, key=lambda x: -(x.get("continue_day_cnt") or 1))
              if (i.get("continue_day_cnt") or 1) >= 2]

    # ---- 战法提示(规则化) ----
    hints = []
    if leader:
        b = leader["boards"]
        pos_txt = ("低位(1-2板):可关注 弱转强/分歧回封/二板转一致 类买点" if b <= 2 else
                   "中位(3-4板):只做分歧转一致,不追高开秒板" if b <= 4 else
                   f"高标({b}板 ≥{MAX_WATCH_BOARDS}):只看不追,等首阴反包(仅第一次)")
        hints.append(f"主线题材【{theme}】最高 {stat['height']} 板、{stat['count']} 家涨停——板块梯队"
                     f"{'完整' if stat['count'] >= 3 else '单薄(孤板要警惕)'}")
        hints.append(f"龙头 {leader['name']}({leader['code']}) {b} 连板 · {leader['state']} · "
                     f"封单 {leader['seal_yi']} 亿 · 首板时间 {leader['limit_time']} → {pos_txt}")
        if leader["code"] in break_codes:
            hints.append(f"⚠ 龙头今日炸板(分歧):烂板弱转强/分歧回封是买点,"
                         f"巨量开板不回封是离场信号——以竞价与量能为准")
        hints.append("龙头专属纪律:破 5 日线减仓,破 10 日线清仓;断板(连板中断)即离场;"
                     "跟风股不做——只认板块内涨停时间最早、封单最大的那只")
    else:
        hints.append("今日涨停池无有效数据")
    return {"theme": theme, "height": stat["height"], "count": stat["count"],
            "leader": leader, "ladder": ladder, "hints": hints,
            "stats": {"up_cnt": len(items), "break_cnt": len(break_codes)},
            "date": dt.date.today().isoformat()}
