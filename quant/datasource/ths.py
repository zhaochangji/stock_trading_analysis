# -*- coding: utf-8 -*-
"""同花顺「扶摇」金融数据 API 适配层。

Base URL: https://fuyao.aicubes.cn
认证: X-api-key(读 config/secrets.yaml 或环境变量 THS_API_KEY,严禁硬编码)
响应: 统一信封 {code, message, request_id, data};code=0 成功;
      429 / code=4001 限流 → 指数退避重试。
thscode 格式: 600519.SH / 002636.SZ(带交易所后缀)。
"""

import logging
import os
import re
import time

import requests

logger = logging.getLogger(__name__)

BASE = "https://fuyao.aicubes.cn"

_KEY = None


def get_api_key() -> str:
    """THS API key:环境变量 THS_API_KEY > config/secrets.yaml。"""
    global _KEY
    if _KEY is None:
        key = os.environ.get("THS_API_KEY", "")
        if not key:
            secrets = os.path.join(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))), "config", "secrets.yaml")
            try:
                with open(secrets, "r", encoding="utf-8") as f:
                    m = re.search(r"ths_api_key:\s*(\S+)", f.read())
                    if m:
                        key = m.group(1)
            except FileNotFoundError:
                pass
        if not key:
            raise RuntimeError("未找到 THS API key:请设置环境变量 THS_API_KEY 或 "
                               "在 config/secrets.yaml 中配置 ths_api_key")
        _KEY = key
    return _KEY


def call(path: str, params: dict = None, retries: int = 3, timeout: int = 20):
    """GET 一个端点,返回 data(信封业务数据);失败返回 None。

    限流(429 / code=4001)与网络错误按指数退避重试。
    """
    url = f"{BASE}{path}"
    headers = {"X-api-key": get_api_key()}
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 429:
                raise RuntimeError("rate_limited_429")
            j = r.json()
            code = j.get("code")
            if code == 0:
                return j.get("data")
            if code == 4001:  # 限流
                raise RuntimeError("rate_limited_4001")
            logger.warning(f"THS 业务错误 code={code} {j.get('message')} path={path}")
            return None
        except requests.RequestException as e:
            logger.warning(f"THS 网络异常 {e} path={path} 第{i + 1}次")
        except RuntimeError as e:
            if "rate_limited" in str(e):
                logger.warning(f"THS 限流 path={path} 第{i + 1}次退避")
        if i < retries - 1:
            time.sleep(2 ** (i + 1) + 1)  # 3s/5s/9s
    return None


def to_thscode(code: str) -> str:
    """纯代码 → thscode(6 开头沪市 .SH,其余深市 .SZ)。"""
    code = code.strip()
    return f"{code}.SH" if code.startswith("6") else f"{code}.SZ"


# ---------------- 行情 ----------------

def prices_snapshot(thscodes=None, limit=100, offset=0):
    """A股行情快照。thscodes 传入时批量取;否则全市场分页。"""
    p = {"limit": limit, "offset": offset}
    if thscodes:
        p = {"thscodes": ",".join(thscodes)}
    return call("/api/a-share/prices/snapshot", p)


# ---------------- 集合竞价(M4 竞价选股) ----------------

def auction_snapshot(thscodes, stage="final"):
    """竞价快照(单次最多 100 只)。stage: live 实时 / final 终态。"""
    return call("/api/a-share/auction/snapshot",
                {"thscodes": ",".join(thscodes), "stage": stage})


def short_term_benchmark(date=None):
    """短线风向标竞价基准(tags: 高开/放量 等)。date: YYYY-MM-DD,默认当日。"""
    p = {"date": date} if date else {}
    return call("/api/a-share/auction/short-term-benchmark", p)


# ---------------- 特色数据(M6 打板题材 + 复盘情绪) ----------------

def limit_up_pool(date=None):
    """涨停股票池(含涨停原因/连板)。date 缺省为最近交易日。"""
    return call("/api/a-share/special-data/limit-up-pool", {"date": date} if date else None)


def limit_down_pool(date=None):
    return call("/api/a-share/special-data/limit-down-pool", {"date": date} if date else None)


def limit_break_pool(date=None):
    """炸板股票池。"""
    return call("/api/a-share/special-data/limit-break-pool", {"date": date} if date else None)


def limit_up_ladder():
    """近 30 个交易日连板天梯矩阵。"""
    return call("/api/a-share/special-data/limit-up-ladder")


def hot_stock_list(level="24h"):
    """热股榜单 Top30。level: 24h / 1h。"""
    return call("/api/a-share/special-data/hot-stock-list", {"level": level})


def skyrocket_list(rank_type="day"):
    """热度飙升榜 Top30。rank_type: day / hour。"""
    return call("/api/a-share/special-data/skyrocket-list", {"rank_type": rank_type})


def anomaly_analysis_list(date=None):
    """当日个股异动原因列表(同花顺异动归因)。"""
    return call("/api/a-share/special-data/anomaly-analysis-list",
                {"date": date} if date else None)


def anomaly_analysis_stock(thscodes):
    """按股票批量查询当日异动原因。"""
    return call("/api/a-share/special-data/anomaly-analysis-stock",
                {"thscodes": ",".join(thscodes)})


def dragon_tiger_list(date=None, board="all"):
    """龙虎榜。board: all / institution 机构榜 / hot_money 游资榜。"""
    p = {"board": board}
    if date:
        p["date"] = date
    return call("/api/a-share/special-data/dragon-tiger-list", p)


# ---------------- 全市场数据导出(M1 历史K线) ----------------

def dump_download_url(kind: str) -> str:
    """获取 dump 的 S3 预签名下载链接(约 5 分钟有效,须立即下载)。

    kind: daily-k(10年日K) / daily-k-10d(近10交易日) / adjustment-factors(复权因子)
    """
    data = call(f"/api/dump/market-dumps/{kind}/download-url")
    return (data or {}).get("presigned_url", "")


# ---------------- 指数/板块(同花顺行业与概念指数) ----------------

def ths_index_list(tag="industry"):
    """同花顺指数清单。tag: industry(行业) / cn_concept(概念) / region / tszs。"""
    return call("/api/a-share-index/catalog/ths-index-list", {"tag": tag})


def ths_index_constituents(thscode):
    """单指数成分股清单(单次仅一个 thscode)。"""
    return call("/api/a-share-index/constituents/ths-stock-list", {"thscode": thscode})


def index_snapshot(thscodes):
    """指数行情快照(批量,逗号分隔 thscodes;支持同花顺行业/概念指数 .TI)。"""
    return call("/api/a-share-index/prices/snapshot", {"thscodes": ",".join(thscodes)})


def index_hist(thscode, start_ms, end_ms):
    """指数历史K线(仅近 5 年,单只,日线)。"""
    return call("/api/a-share-index/prices/historical",
                {"thscode": thscode, "interval": "1d", "start": start_ms, "end": end_ms})


# ---------------- 个股主力资金(实时快照 + 历史) ----------------

def capital_flow_snapshot(thscode):
    """个股主力资金实时快照:超大/大/中/小单 in/out/net(元)。单只。"""
    return call("/api/a-share/capital-flow/snapshot", {"thscode": thscode})


def capital_flow_historical(thscode, start_ms, end_ms, interval="1d"):
    """个股资金流历史(1m 或 1d;历史不含 net,需 in-out 自算)。窗口 1d ≤ 1 年。"""
    return call("/api/a-share/capital-flow/historical",
                {"thscode": thscode, "interval": interval,
                 "start": start_ms, "end": end_ms})


# ---------------- 资讯事件库(AI 分级 A-F + 关联板块) ----------------

def news_events(params=None):
    """资讯事件搜索(GET /api/news/events/search)。params 视服务端支持(如 keyword/date)。"""
    return call("/api/news/events/search", params or {})


# ---------------- 元信息 ----------------

def trading_calendar():
    """A 股近一年交易日序列。"""
    return call("/api/a-share/calendar")


def tickers_list(asset_type=None, limit=100, offset=0):
    """标的代码表(分页)。"""
    p = {"limit": limit, "offset": offset}
    if asset_type:
        p["type"] = asset_type
    return call("/api/meta/tickers/list", p)
