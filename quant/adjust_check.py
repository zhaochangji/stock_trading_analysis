# -*- coding: utf-8 -*-
"""除权除息口径核验:通达信除权表 × 库内前复权 K 线(独立第三方口径)。

背景:quant_hist_daily 以东财 fqt=1(前复权)为权威口径;2026-10-21/22
曾发生 THS dump(未复权)整段 REPLACE 覆盖事故(两晚共 5.5 万行,10-08
以来除权股 256 只受影响)。本模块用通达信除权表定位每只股票近 N 日内
最近一次除权日,再检查库内 K 线在该除权日边界是否连续——前复权正确时
除权日无跳空(真实涨跌幅仍在涨跌停内);被未复权数据覆盖时,除权日
涨跌幅 = 复权因子 × 真实涨跌 − 1,送转 10 送 10 时约 -50%、大额分红
-3% 起,远超涨跌停,一眼断档。

核验范围(低频定位,收盘链每日一次):实盘持仓 + 模拟盘持仓 + 近 N 日
战法候选(quant_strategy_trades)与推送信号(quant_paper_trades),去重
主板。结果落 quant_adjust_check(REPLACE 幂等,uk=check_date+code)。

局限(如实标注,绝不声称全量核验):①小额现金分红(约 <3%)叠加正常
波动时,跳空可能落在涨跌停缓冲内而漏检——检测目标是"整段被覆盖"类
事故(送转/大额分红必检出),不是逐行精调;②通达信除权表本身取不到
(接口失败/服务器全踩空)时该股记 no_data,不猜测、不补造。
"""

import datetime as dt
import logging
import os

import yaml

from quant.datasource import tdx
from quant.db import get_connection

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_DDL = """CREATE TABLE IF NOT EXISTS quant_adjust_check (
  check_date CHAR(10) NOT NULL,
  code VARCHAR(10) NOT NULL,
  name VARCHAR(32) DEFAULT '',
  ex_date CHAR(10) DEFAULT '',
  ex_param VARCHAR(120) DEFAULT '',
  prev_close DOUBLE,
  ex_close DOUBLE,
  jump DOUBLE,
  status VARCHAR(16),
  note VARCHAR(255) DEFAULT '',
  PRIMARY KEY (check_date, code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

_INSERT = ("REPLACE INTO quant_adjust_check (check_date,code,name,ex_date,"
           "ex_param,prev_close,ex_close,jump,status,note) "
           "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")

# 跳空判定缓冲:主板涨跌停 ±10%(ST ±5%),留正常波动叠加空间。
# 送转 10 送 10 ≈ -50%、10 送 5 ≈ -33% 必触发;小额分红可能漏检(见 docstring)。
_LIMIT_MAIN = 0.12
_LIMIT_ST = 0.065


def ensure_tables(conn=None):
    conn = conn or get_connection()
    with conn.cursor() as cur:
        cur.execute(_DDL)
    conn.commit()
    return conn


def _describe(ev):
    """除权参数的人话描述。"""
    parts = []
    if ev.get("fenhong"):
        parts.append("10派%.2f元" % ev["fenhong"])
    if ev.get("songzhuangu"):
        parts.append("10送转%.1f股" % ev["songzhuangu"])
    if ev.get("peigu"):
        parts.append("10配%.1f股@%s元" % (ev["peigu"], ev.get("peigujia") or "?"))
    return " / ".join(parts) or (ev.get("name") or "除权除息")


def _latest_event(rows, since):
    """取 date>=since 的最近一次真除权(分红/送转/配股参数任一非零)。

    category=5(股本变化)等无除权参数的事件不算——它们不产生价格跳空。
    """
    best = None
    for r in rows or []:
        d = r.get("date")
        if not d or d < since:
            continue
        if not any((r.get(k) or 0) > 0
                   for k in ("fenhong", "songzhuangu", "peigu")):
            continue
        if best is None or d > best["date"]:
            best = r
    return best


def _rows_around(conn, code, day):
    """库内除权日行与其前一交易日行((date,close) 元组),缺任一返回 None。"""
    with conn.cursor() as cur:
        cur.execute("SELECT `date`,`close` FROM quant_hist_daily "
                    "WHERE code=%s AND `date`=%s", (code, day))
        ex = cur.fetchone()
        if not ex:
            return None
        cur.execute("SELECT `date`,`close` FROM quant_hist_daily "
                    "WHERE code=%s AND `date`<%s ORDER BY `date` DESC LIMIT 1",
                    (code, day))
        prev = cur.fetchone()
    return (prev, ex) if prev else None


def check(code, name="", lookback_days=90, conn=None, _fetch_xdxr=None):
    """单股核验:返回 dict(status/note/jump...),不抛异常不落库。

    status: ok(边界连续)/ suspicious(疑似未复权覆盖)/ no_event(近期
    无除权)/ no_data(通达信失败或 K 线缺行)。
    """
    conn = conn or get_connection()
    fetch = _fetch_xdxr or tdx.fetch_xdxr
    base = {"code": str(code).zfill(6), "name": name or "",
            "ex_date": "", "ex_param": "", "prev_close": None,
            "ex_close": None, "jump": None}
    try:
        rows = fetch(code)
    except Exception as error:  # noqa: BLE001
        logger.warning("tdx 除权表拉取失败 %s: %s", code, error)
        return dict(base, status="no_data", note="通达信除权表拉取失败")
    if not rows:
        return dict(base, status="no_data", note="通达信除权表无数据")
    since = (dt.date.today() - dt.timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    ev = _latest_event(rows, since)
    if ev is None:
        return dict(base, status="no_event",
                    note="近 %d 日无除权除息(通达信表 %d 条)" % (lookback_days, len(rows)))
    day = ev["date"]
    around = _rows_around(conn, code, day)
    if around is None:
        return dict(base, status="no_data", ex_date=day,
                    ex_param=_describe(ev),
                    note="除权日或前一交易日 K 线缺行,无法核验边界")
    (d_prev, prev_close), (d_ex, ex_close) = around
    jump = float(ex_close) / float(prev_close) - 1.0
    limit = _LIMIT_ST if "ST" in (name or "").upper() else _LIMIT_MAIN
    base.update({"ex_date": day, "ex_param": _describe(ev),
                 "prev_close": float(prev_close), "ex_close": float(ex_close),
                 "jump": jump})
    if abs(jump) > limit:
        return dict(base, status="suspicious",
                    note="除权日库内涨跌 %+.1f%% 超涨跌停缓冲,疑似未复权数据"
                         "覆盖(通达信口径 %s),建议以东财 fqt=1 回补该段"
                         % (jump * 100, _describe(ev)))
    return dict(base, status="ok",
                note="除权日边界连续(库内涨跌 %+.2f%%,%s)" % (jump * 100, _describe(ev)))


def _universe(conn, days=7):
    """核验股票清单:实盘持仓+模拟盘持仓+近 N 日战法候选/推送信号(去重)。"""
    codes = {}

    def add(code, source):
        c = str(code).zfill(6)
        if c and len(c) == 6 and c not in codes:
            codes[c] = source

    try:
        with open(os.path.join(ROOT, "config", "config.yaml"),
                  encoding="utf-8") as f:
            for item in (yaml.safe_load(f) or {}).get("holdings") or []:
                add(item.get("code"), "实盘持仓")
    except Exception as error:  # noqa: BLE001
        logger.warning("实盘持仓读取失败: %s", error)

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code FROM quant_sim_positions WHERE qty>0")
            for (code,) in cur.fetchall():
                add(code, "模拟盘持仓")
            since = (dt.date.today() - dt.timedelta(days=days)).strftime("%Y-%m-%d")
            cur.execute("SELECT DISTINCT code FROM quant_strategy_trades "
                        "WHERE log_date>=%s", (since,))
            for (code,) in cur.fetchall():
                add(code, "战法候选")
            cur.execute("SELECT DISTINCT code FROM quant_paper_trades "
                        "WHERE push_date>=%s", (since,))
            for (code,) in cur.fetchall():
                add(code, "推送信号")
    except Exception as error:  # noqa: BLE001
        logger.warning("核验清单数据表读取失败: %s", error)
    return codes


def _names(conn, codes):
    """cn_stock_spot 最新日目录(code→name),用于 ST 判定与展示。"""
    out = {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code,name FROM cn_stock_spot "
                        "WHERE `date`=(SELECT MAX(`date`) FROM cn_stock_spot)")
            for code, name in cur.fetchall():
                out[str(code).zfill(6)] = str(name or "")
    except Exception as error:  # noqa: BLE001
        logger.warning("股票目录读取失败: %s", error)
    return out


def run_check(days=7, lookback_days=90, conn=None, _fetch_xdxr=None):
    """核验全清单并落库,返回汇总 dict。失败不抛异常(收盘链不阻断)。"""
    conn = conn or ensure_tables()
    try:
        codes = _universe(conn, days=days)
        names = _names(conn, codes)
        today = dt.date.today().strftime("%Y-%m-%d")
        rows = []
        for code in sorted(codes):
            try:
                result = check(code, name=names.get(code, ""), conn=conn,
                               lookback_days=lookback_days, _fetch_xdxr=_fetch_xdxr)
            except Exception as error:  # noqa: BLE001
                logger.warning("除权核验异常 %s: %s", code, error)
                result = {"code": code, "name": names.get(code, ""), "ex_date": "",
                          "ex_param": "", "prev_close": None, "ex_close": None,
                          "jump": None, "status": "no_data", "note": f"核验异常:{error}"}
            rows.append(result)
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(_INSERT, (today, r["code"], r["name"], r["ex_date"] or "",
                                      r["ex_param"] or "", r["prev_close"],
                                      r["ex_close"], r["jump"], r["status"],
                                      (r["note"] or "")[:250]))
        conn.commit()
        summary = {"total": len(rows)}
        for key in ("ok", "suspicious", "no_event", "no_data"):
            summary[key] = sum(1 for r in rows if r["status"] == key)
        summary["suspects"] = [
            {"code": r["code"], "name": r["name"], "ex_date": r["ex_date"],
             "jump": r["jump"], "ex_param": r["ex_param"], "note": r["note"]}
            for r in rows if r["status"] == "suspicious"]
        return summary
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
