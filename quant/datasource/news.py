# -*- coding: utf-8 -*-
"""消息面数据源(M8):东财个股新闻 + 财联社电报。

来源:akshare(akfamily/akshare,GitHub 1 万+ star 的开源财经数据接口库)。
设计原则(讲证据):
  * 所有条目保留 发布时间 / 来源 / 原文链接,不加工、不改写;
  * 利好/利空仅作「关键词粗标记」,供快速扫读,不作判定结论(报告内注明);
  * 拉取失败时静默返回空列表,调用方显示"消息面获取失败",绝不编造。
"""

import logging

logger = logging.getLogger(__name__)

_TAG_POS = ("回购", "增持", "预增", "扭亏", "净利增", "利润增", "增长", "中标", "签约",
            "获批", "分红", "送转", "订单", "合作", "获得", "拟派", "重组", "涨停")
_TAG_NEG = ("减持", "质押", "立案", "被查", "诉讼", "预亏", "亏损", "下滑", "下降",
            "罚款", "警示", "退市", "终止", "冻结", "暂停", "解除", "风险提示", "ST")


def tag_news(text):
    """关键词级利好/利空粗标记:返回 '利好' / '利空' / '中性'。

    说明:仅标题关键词匹配,可能误判(如"利润增长"连带"下滑"文中出现);
    展示时注明"关键词粗标记",结论仍以原文为准。
    """
    if not text:
        return "中性"
    pos = sum(1 for w in _TAG_POS if w in text)
    neg = sum(1 for w in _TAG_NEG if w in text)
    if pos > neg:
        return "利好"
    if neg > pos:
        return "利空"
    return "中性"


def stock_news(code, limit=5, timeout=7):
    """东财个股新闻(东财 Choice 采集)。返回 [{title, digest, time, source, url, tag}]。"""
    import akshare as ak
    from concurrent.futures import ThreadPoolExecutor, TimeoutError

    # akshare 的个股新闻方法没有暴露 timeout；限制等待时间，后台线程在源端
    # 自行结束。超时由调用方展示为缺失，不把新闻失败当成没有利空。
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(ak.stock_news_em, symbol=code)
    try:
        df = future.result(timeout=timeout)
    except TimeoutError as exc:
        future.cancel()
        raise TimeoutError("东财个股新闻查询超时") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    items = []
    for _, r in df.head(limit).iterrows():
        title = str(r.get("新闻标题") or "")
        digest = str(r.get("新闻内容") or "")[:160]
        items.append({
            "title": title,
            "digest": digest,
            "time": str(r.get("发布时间") or "")[:16],
            "source": str(r.get("文章来源") or ""),
            "url": str(r.get("新闻链接") or ""),
            "tag": tag_news(title + digest),
        })
    return items


def telegraph(limit=8):
    """财联社电报(全市场快讯,最新在前)。返回 [{title, digest, time, tag}]。"""
    import akshare as ak
    df = ak.stock_info_global_cls()
    items = []
    for _, r in df.head(limit).iterrows():
        title = str(r.get("标题") or "")
        digest = str(r.get("内容") or "")[:160]
        items.append({
            "title": title,
            "digest": digest,
            "time": f"{r.get('发布日期', '')} {r.get('发布时间', '')}"[:16],
            "tag": tag_news(title + digest),
        })
    return items
