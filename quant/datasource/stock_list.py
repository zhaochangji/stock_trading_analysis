# -*- coding: utf-8 -*-
"""股票清单获取。

优先级:
1. MySQL `cn_stock_spot`(InStock 每日全市场快照,最权威)
2. 新浪 Market Center 分页接口(东财 push2 不可用时的备用清单源)

返回 [(date_str, code, name), ...],与 InStock TABLE_CN_STOCK_FOREIGN_KEY 对齐。
"""

import logging
import time
import random

import requests

logger = logging.getLogger(__name__)

# A股代码规则:沪市 60/68 开头,深市 00/30 开头(按用户要求剔除北交所 8/4/92 开头)
def is_main_or_star_growth(code: str) -> bool:
    return code[:2] in ("60", "68", "00", "30")


def get_list_from_db(engine):
    """从 InStock 的 cn_stock_spot 表读取股票清单(某交易日的全市场)。"""
    import pandas as pd
    try:
        df = pd.read_sql(
            "SELECT s.date, s.code, s.name FROM cn_stock_spot s "
            "JOIN (SELECT MAX(date) d FROM cn_stock_spot) t ON s.date = t.d "
            "WHERE s.code REGEXP '^(60|68|00|30)'",
            con=engine)
        if df is None or len(df) == 0:
            return None
        df["date"] = df["date"].astype(str)
        return [tuple(x) for x in df[["date", "code", "name"]].values]
    except Exception as e:
        logger.warning(f"cn_stock_spot 读取失败(表可能未建立): {e}")
        return None


def get_list_from_sina(max_pages: int = 80, sleep: float = 0.6) -> list:
    """新浪 Market Center 拉沪深A股全市场清单(分页,每页 100)。

    接口与东财无关,用于东财 push2 被限流时的备用。
    """
    url = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "Market_Center.getHQNodeData")
    out, page = [], 1
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
               "Referer": "http://finance.sina.com.cn/"}
    while page <= max_pages:
        params = {"page": page, "num": 100, "sort": "symbol", "asc": 1, "node": "hs_a"}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=15)
            rows = r.json()
            if not rows:
                break
            for row in rows:
                code = row.get("code", "")
                if not is_main_or_star_growth(code):
                    continue
                out.append(("", code, row.get("name", "")))
            page += 1
            time.sleep(sleep + random.uniform(0, 0.3))
        except Exception as e:
            logger.error(f"新浪清单第 {page} 页失败: {e};重试")
            time.sleep(3)
            # 单页失败重试一次,仍失败则终止(已收集的数据仍可用)
            try:
                r = requests.get(url, params={"page": page, "num": 100, "sort": "symbol",
                                              "asc": 1, "node": "hs_a"}, headers=headers, timeout=15)
                rows = r.json()
                if not rows:
                    break
                for row in rows:
                    code = row.get("code", "")
                    if is_main_or_star_growth(code):
                        out.append(("", code, row.get("name", "")))
                page += 1
            except Exception:
                break
    return out


def get_stock_list(engine=None, prefer_db: bool = True) -> list:
    """获取全市场股票清单。优先 DB,失败用新浪。"""
    if prefer_db and engine is not None:
        data = get_list_from_db(engine)
        if data:
            return data, "db"
    data = get_list_from_sina()
    return data, "sina"
