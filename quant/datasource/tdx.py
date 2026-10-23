# -*- coding: utf-8 -*-
"""通达信行情服务器数据源(pytdx,TCP 7709 短连接)。

2026-10-23 实测(104 台内置服务器轮测,多台一致结论,非单点故障):

  可用:
    get_xdxr_info               除权除息表——分红/送转/配股全历史,
                                前复权自算与除权核验的独立口径,
                                不依赖东财 fqt=1 黑盒(2026-10-21/22
                                曾发生 5.5 万行前复权被 dump 未复权
                                覆盖的事故,独立除权表可校验);
    get_history_minute_time_data 历史分时(240 条完整;当日传当日
                                日期同样可用,盘中增量到当前分钟,
                                实测 2026-10-23 14:00 时 184 条价格
                                合理);get_minute_time_data(当日专用)
                                实测返回值字段错乱(price 0.00→1123),
                                **禁用**;
    get_finance_info            财务摘要(流通股本/行业/更新日);
    get_security_list/count     证券目录(全市场代码+名称)。

  不可用(服务器返回 2-4 字节错误响应,协议升级/风控,2018 年停更的
  pytdx 1.72 解析不出数据——多台服务器一致,非单点):
    get_security_bars           K 线(不能作 K 线源!);
    get_security_quotes         实时五档行情;
    get_transaction_data        分笔成交。

设计:短连接(连取断,不留心跳线程);数据类接口在至多 5 台服务器上
重试取**首个非空结果**(实测部分服务器恒连不上、部分对个别市场/
接口返回空,单台随机会踩空);失败返回 None/空,调用方降级,绝不影响
主链。**低频补充源定位**:每次调用都是新建 TCP 连接,不做盘中高频
轮询(建议场景:收盘后除权核对、按需单股分时/财务查询)。
"""

import datetime as dt
import logging
import random
import time

import pandas as pd

logger = logging.getLogger(__name__)

_PORTS = (7709,)
_MAX_HOSTS = 5          # 每次数据获取最多尝试的服务器数


def _hosts(limit=None):
    try:
        from pytdx.config.hosts import hq_hosts
        pool = [h for h in hq_hosts if h[2] in _PORTS]
        random.shuffle(pool)
        return pool[: (limit or _MAX_HOSTS)]
    except Exception:  # noqa: BLE001
        return []


def _connect_one(ip, port):
    from pytdx.hq import TdxHq_API

    api = TdxHq_API(heartbeat=False)
    try:
        if api.connect(ip, port, time_out=4):
            return api
    except Exception:  # noqa: BLE001
        pass
    return None


def _is_empty(result):
    """result 为 None/空 list/空 DataFrame 时 True(DataFrame 不能用
    bool() 判断,2026-10-23 曾因此在第一台成功后仍连续重试全炸)。"""
    if result is None:
        return True
    if isinstance(result, pd.DataFrame):
        return result.empty
    try:
        return len(result) == 0
    except TypeError:
        return False


def _fetch(fetch_fn, accept_empty=False):
    """在至多 5 台服务器上执行 fetch_fn(api),取首个非空结果。

    accept_empty=True 时空结果也算成功(如财务对无数据标的)。
    返回 fetch_fn 的结果;全部失败返回 None。
    """
    result = None
    for _name, ip, port in _hosts():
        api = _connect_one(ip, port)
        if api is None:
            continue
        try:
            result = fetch_fn(api)
            if not _is_empty(result) or accept_empty:
                return result
        except Exception as error:  # noqa: BLE001
            logger.warning("tdx 服务器 %s 请求失败: %s", ip, error)
        finally:
            try:
                api.disconnect()
            except Exception:  # noqa: BLE001
                pass
    return result


def _market(code):
    return 1 if str(code).startswith(("6", "9", "5")) else 0


def fetch_xdxr(code):
    """除权除息表(全历史):list[dict] 或 None。

    字段:date(YYYY-MM-DD),category,name,fenhong(每 10 股现金红利,元)、
    songzhuangu(送转股)、peigujia/peigu(配股价/比例)。
    category 含:1=除权除息 5=股本变化 等。
    """
    mkt, c6 = _market(code), str(code).zfill(6)

    def run(api):
        rows = api.get_xdxr_info(mkt, c6) or []
        out = []
        for r in rows:
            date = None
            if r.get("year"):
                date = "%04d-%02d-%02d" % (r["year"], r.get("month") or 1,
                                           r.get("day") or 1)
            out.append({
                "date": date,
                "category": r.get("category"),
                "name": r.get("name"),
                "fenhong": _num(r.get("fenhong")),
                "songzhuangu": _num(r.get("songzhuangu")),
                "peigujia": _num(r.get("peigujia")),
                "peigu": _num(r.get("peigu")),
            })
        return out

    return _fetch(run)


def fetch_minute(code, date=None):
    """分时行情(当日或历史):DataFrame(datetime/price/vol)或 None。

    date=None → 当日(盘中增量到当前分钟);date="YYYY-MM-DD" 或
    int(20261022)→ 指定日(收盘后 240 条完整)。
    """
    import pandas as pd

    mkt, c6 = _market(code), str(code).zfill(6)

    def run(api):
        # 统一走历史分时接口:当日专用 get_minute_time_data 实测字段
        # 错乱(price 序列出现 0.00→1123),history 接口当日可用且值正确。
        day = int(str(date).replace("-", "")) if date else int(
            dt.date.today().strftime("%Y%m%d"))
        rows = api.get_history_minute_time_data(mkt, c6, day) or []
        if not rows:
            return None
        frame = pd.DataFrame(rows)
        if "datetime" in frame.columns:
            frame = frame[["datetime", "price", "vol"]]
        return frame

    return _fetch(run)


_MINUTE_CACHE = {}


def fetch_minute_cached(code, date=None, max_age=60):
    """fetch_minute 的进程级短缓存(默认 60 秒)。

    分析页等按需场景防抖:每次调用都是新建 TCP 连接,手动刷新的
    间隔内复用;只缓存成功结果,失败不缓存(下次重试)。盘中 60 秒
    过期后重新拉取增量到当前分钟;收盘后 240 条不变,60 秒缓存同样
    安全。
    """
    key = (str(code), str(date or ""))
    hit = _MINUTE_CACHE.get(key)
    if hit is not None and time.time() - hit[0] <= max_age:
        return hit[1]
    frame = fetch_minute(code, date)
    if frame is not None and not frame.empty:
        _MINUTE_CACHE[key] = (time.time(), frame)
    return frame


def fetch_finance(code):
    """财务摘要 dict 或 None(流通股本/总股本/行业/更新日)。"""
    mkt, c6 = _market(code), str(code).zfill(6)

    def run(api):
        r = api.get_finance_info(mkt, c6)
        if not r:
            return None
        return {
            "liutongguben": _num(r.get("liutongguben")),
            "zongguben": _num(r.get("zongguben")),
            "industry": r.get("industry"),
            "updated_date": str(r.get("updated_date") or "")[:10],
        }

    return _fetch(run)


def probe(code="002184"):
    """各接口可用性探测(诊断用,单台服务器直测):dict{接口: 条数或错误}。"""
    hosts = _hosts(limit=1)
    api = _connect_one(*(hosts[0][1:] if hosts else ("115.238.90.165", 7709)))
    if api is None:
        return {"connect": None}
    out = {"connect": True, "server": hosts[0][1] if hosts else "?"}
    mkt, c6 = _market(code), str(code).zfill(6)
    checks = (
        ("xdxr", lambda: len(api.get_xdxr_info(mkt, c6) or [])),
        ("minute_today", lambda: len(
            api.get_history_minute_time_data(
                mkt, c6, int(dt.date.today().strftime("%Y%m%d"))) or [])),
        ("minute_history", lambda: len(
            api.get_history_minute_time_data(
                mkt, c6, (dt.date.today() - dt.timedelta(days=1)).strftime(
                    "%Y%m%d")) or [])),
        ("finance", lambda: 1 if api.get_finance_info(mkt, c6) else 0),
        ("security_list", lambda: len(api.get_security_list(0, 0) or [])),
        ("bars_kline", lambda: len(
            api.get_security_bars(9, mkt, c6, 0, 5) or [])),
        ("quotes", lambda: len(api.get_security_quotes([(mkt, c6)]) or [])),
    )
    for name, fn in checks:
        try:
            out[name] = fn()
        except Exception as error:  # noqa: BLE001
            out[name] = f"ERR {type(error).__name__}"
    try:
        api.disconnect()
    except Exception:  # noqa: BLE001
        pass
    return out


def _num(value):
    try:
        v = float(value)
        return None if v != v else v          # NaN → None
    except (TypeError, ValueError):
        return None


__all__ = ["fetch_xdxr", "fetch_minute", "fetch_minute_cached",
           "fetch_finance", "probe", "probe_cached", "AVAILABLE_NOTE"]

AVAILABLE_NOTE = (
    "通达信(pytdx)可用:除权除息表/当日与历史分时/财务摘要/证券目录;"
    "不可用:K线/实时五档/分笔(服务器协议变更,详见模块 docstring)"
)

_LAST_PROBE = {"ts": 0.0, "result": None}


def probe_cached(max_age=86400):
    """可用性探测的进程级缓存(一天一次足够,供诊断页/接口健康展示)。"""
    now = time.time()
    if _LAST_PROBE["result"] is None or now - _LAST_PROBE["ts"] > max_age:
        _LAST_PROBE["result"] = probe()
        _LAST_PROBE["ts"] = now
    return _LAST_PROBE["result"]
