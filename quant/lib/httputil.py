# -*- coding: utf-8 -*-
"""quant 自有 HTTP 会话。

与 InStock 的 eastmoney_fetcher 解耦:
- 不硬编码 Accept-Encoding(requests 按已装解码库自动协商,根除 br/zstd 解不开的隐患)
- 自带重试与限速配合调用方控制
"""

import time
import random

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_session = None

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        retry = Retry(total=3, backoff_factor=1.0,
                      status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["GET"])
        adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        s.headers.update(_HEADERS)
        _session = s
    return _session


def get_json(url, params=None, timeout=15, retries=3):
    """GET 并返回 json。失败返回 None(不打断批量任务)。"""
    s = get_session()
    for i in range(retries):
        try:
            r = s.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception:
            if i < retries - 1:
                time.sleep(1 + random.uniform(0, 2))
    return None
