# -*- coding: utf-8 -*-
"""巨潮资讯网(cninfo)公告原文核验:沪深两市法定信息披露平台。

对给出的每只候选股票拉取近 N 日公告列表(标题 + 原文 PDF 链接,用户可
点开核验原文),并按标题关键词分级:
  blocked  停牌(不含复牌)/终止上市/退市/立案/风险警示(*ST)——不进可买榜;
  warning  减持/问询函/关注函/监管函/预亏/平仓/冻结/诉讼/终止重组等
           ——标注并下调可信度;
  info     异常波动(连板股例行)/增持/其他常规公告——仅展示原文链接。
接口失败或未收录时如实返回 ok=False,绝不补造「无公告」结论。

IO 约束:每股两请求(orgId 查询 + 公告列表),调用方须做缓存(建议 1 小时),
不在单股请求里对全市场批量核验。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse
import urllib.request

BASE = "http://www.cninfo.com.cn"
PDF_BASE = "http://static.cninfo.com.cn/"
TIMEOUT = 12

# (命中正则, 排除正则): 标题命中且不被排除 → 硬剔除。
_BLOCK_RULES = (
    (re.compile("终止上市|暂停上市|退市|重大违法"), None),
    (re.compile("立案"), None),
    (re.compile("停牌"), re.compile("复牌")),   # 复牌是恢复交易,不算停牌
    (re.compile(r"\*ST|风险警示"), None),
)
_WARN_WORDS = re.compile(
    "减持|问询函|关注函|监管函|工作函|警示函|纪律处分|预亏|业绩大幅下降|业绩下降"
    "|平仓|冻结|诉讼|仲裁|终止重组|重组失败|终止筹划|违规担保|资金占用|被执行")


def _post(path, data):
    request = urllib.request.Request(
        BASE + path, data=urllib.parse.urlencode(data).encode(),
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Referer": BASE + "/"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_orgid(code):
    """代码 → (巨潮 orgId, plate);未收录/已退市返回 (None, None)。"""
    data = _post("/new/information/topSearch/detailOfQuery",
                 {"keyWord": code, "maxSecNum": 10, "maxListNum": 5})
    for item in data.get("keyBoardList") or []:
        if (item.get("code") == str(code).zfill(6)
                and item.get("delisted") != "true"):
            return item.get("orgId"), item.get("plate")
    return None, None


def fetch_announcements(code, days=30, page_size=30):
    """一只股票近 days 日公告(标题+原文 PDF 链接);失败返回 ok=False。

    返回 {ok, items:[{title,date,url}], total, source, window_days}。
    巨潮的公告时间戳为毫秒;PDF 链接指向 static.cninfo.com.cn 原文。
    """
    code = str(code).zfill(6)
    org, plate = fetch_orgid(code)
    if not org:
        return {"ok": False, "error": "巨潮未收录该代码(可能已退市)",
                "items": []}
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    data = _post("/new/hisAnnouncement/query", {
        "pageNum": 1, "pageSize": page_size,
        "column": "sse" if plate == "sse" else "szse",
        "tabName": "fulltext", "stock": f"{code},{org}",
        "searchkey": "", "secid": "", "plate": plate, "category": "",
        "trade": "", "seDate": f"{start}~{end}",
        "sortName": "", "sortType": "", "isHLtitle": "true"})
    items = []
    for item in data.get("announcements") or []:
        stamp = item.get("announcementTime")
        date = (dt.datetime.fromtimestamp(stamp / 1000).strftime("%Y-%m-%d")
                if stamp else "")
        items.append({
            "title": str(item.get("announcementTitle") or "").strip(),
            "date": date,
            "url": PDF_BASE + str(item.get("adjunctUrl") or "")})
    return {"ok": True, "items": items,
            "total": int(data.get("totalRecordNum") or 0),
            "source": "巨潮资讯网(沪深法定披露平台)",
            "window_days": days}


def assess_announcements(items):
    """按标题关键词分级;纯函数,便于单测与规则审计。

    返回 {status: pass|warning|blocked, hits:[{title,date,level,url}],
          n, summary}。无高风险词即 pass;异常波动/增持等例行公告不扣分。
    """
    items = items or []
    hits = []
    for item in items:
        title = item.get("title") or ""
        level = None
        for pattern, exclude in _BLOCK_RULES:
            if pattern.search(title) and not (exclude and exclude.search(title)):
                level = "blocked"
                break
        if level is None and _WARN_WORDS.search(title):
            level = "warning"
        if level:
            hits.append({"title": title, "date": item.get("date"),
                         "level": level, "url": item.get("url")})
    blocked = [h for h in hits if h["level"] == "blocked"]
    warnings = [h for h in hits if h["level"] == "warning"]
    if blocked:
        status = "blocked"
        summary = "高风险公告:" + ";".join(h["title"] for h in blocked[:2])
    elif warnings:
        status = "warning"
        summary = "公告风险提示:" + ";".join(h["title"][:36] for h in warnings[:2])
    else:
        status = "pass"
        summary = f"近30日 {len(items)} 条公告,无高风险项"
    return {"status": status, "hits": hits, "n": len(items), "summary": summary}


def check(code, days=30):
    """一步核验:拉取 + 分级。调用方负责缓存与频控。"""
    fetched = fetch_announcements(code, days=days)
    if not fetched.get("ok"):
        return {"ok": False, "status": "unknown", "hits": [], "n": 0,
                "summary": fetched.get("error") or "巨潮公告核验失败",
                "source": "巨潮资讯网"}
    assessed = assess_announcements(fetched["items"])
    assessed["ok"] = True
    assessed["source"] = fetched["source"]
    return assessed


__all__ = ["fetch_orgid", "fetch_announcements", "assess_announcements",
           "check"]
