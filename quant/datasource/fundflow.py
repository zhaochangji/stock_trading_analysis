# -*- coding: utf-8 -*-
"""东财个股资金流历史(主力/超大/大/中/小单,日线)。

接口:push2his.eastmoney.com/api/qt/stock/fflow/daykline/get(非 clist,限流风险低)
返回每只股票按日的主力资金净流入序列(约近一年+),是 M2 三信号与 M5 回测的数据基础。
"""

import logging

from quant.datasource.kline import to_secid
from quant.lib.httputil import get_json

logger = logging.getLogger(__name__)

_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
# f51日期 f52主力净额 f53小单净额 f54中单净额 f55大单净额 f56超大单净额
# f57主力占比 f58小单占比 f59中单占比 f60大单占比 f61超大单占比 f62收盘 f63涨跌幅
_PARAMS = {
    "lmt": "0",       # 全部历史
    "klt": "101",     # 日线
    "fields1": "f1,f2,f3,f7",
    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63",
    "ut": "b2884a393a59ad64002292a3e90d46a5",
}


def fetch_fundflow_em(code: str):
    """拉单只股票的日线资金流历史。

    返回 list[tuple]:
    (date, main_net, small_net, mid_net, big_net, huge_net,
     main_pct, small_pct, mid_pct, big_pct, huge_pct, close, pct_change)
    金额单位:元;占比单位:%。失败返回 None。
    """
    params = dict(_PARAMS)
    params["secid"] = to_secid(code)
    try:
        j = get_json(_URL, params=params, timeout=15)
        if not j or not j.get("data") or not j["data"].get("klines"):
            return None
        out = []
        for line in j["data"]["klines"]:
            p = line.split(",")
            out.append((p[0], _f(p[1]), _f(p[2]), _f(p[3]), _f(p[4]), _f(p[5]),
                        _f(p[6]), _f(p[7]), _f(p[8]), _f(p[9]), _f(p[10]),
                        _f(p[11]), _f(p[12])))
        return out
    except Exception as e:
        logger.warning(f"fetch_fundflow_em {code} 异常: {e}")
        return None


def _f(v):
    try:
        if v in ("-", "", None):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None
