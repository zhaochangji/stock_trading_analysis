# -*- coding: utf-8 -*-
"""THS 批量日K dump 通道:全市场近 N 交易日一次下载(parquet 预签名链接)。

历史K线三层回补链的第二层(THS 快照 → THS dump → 东财逐只):
  - 快照通道限流/部分失败产生**大批** fallback(> threshold,通常是 THS 自己
    限流而非真除权,如 2026-10-20 快照只写入 404 行、fallback 5176 只)时,
    先用 dump 批量补缺失日,再让剩余走东财,避免东财被高频拉黑;
  - 东财逐只 backfill 之后的失败清单,可用 dump 再尾部救一轮;
  - fallback 数量少(≤ threshold,真除权/新股)时不启用 dump,直接走东财
    前复权,保持口径精确。

口径披露:dump 为未复权(adjusted=none)。近 10 日内未除权的股票数值与前
复权序列完全一致;真除权个股仍由东财前复权重拉修正。只插 (code,date)
缺失行,已有行一律不覆盖(含东财已写的前复权值);盘中(15:05 收盘前)
不写入「今日」行,防止未完成 K 线入库。
"""

from __future__ import annotations

import datetime as dt
import io
import logging

logger = logging.getLogger("ths_dump")

# fallback 数量超过该值才启用 dump 层(真除权/新股远小于此)。
RESCUE_THRESHOLD = 300

# 收盘保护:daily-k 的当日终值 15:05 后才可靠。
_CLOSE_CUTOFF = dt.time(15, 5)


def download_daily_k(kind="daily-k-10d", timeout=240):
    """下载并清洗 dump → DataFrame[code,date,open,close,high,low,volume,amount]。

    失败(链接拿不到/下载异常/空数据)返回 None,由调用方决定降级。
    预签名链接约 5 分钟有效,拿到后立即下载。
    """
    import pandas as pd
    import requests

    from quant.datasource import ths

    try:
        url = ths.dump_download_url(kind)
    except Exception as error:  # noqa: BLE001
        logger.warning("dump 链接获取失败: %s", error)
        return None
    if not url:
        logger.warning("dump 链接为空(kind=%s)", kind)
        return None
    try:
        response = requests.get(url, timeout=timeout,
                                headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        raw = pd.read_parquet(io.BytesIO(response.content))
    except Exception as error:  # noqa: BLE001
        logger.warning("dump 下载/解析失败: %s", error)
        return None
    if raw.empty:
        logger.warning("dump 内容为空(kind=%s)", kind)
        return None
    frame = raw.copy()
    frame["code"] = frame["thscode"].astype(str).str.split(".").str[0]
    frame["date"] = (pd.to_datetime(frame["date_ms"], unit="ms")
                     + pd.Timedelta(hours=8)).dt.strftime("%Y-%m-%d")
    for src, dst in (("open_price", "open"), ("close_price", "close"),
                     ("high_price", "high"), ("low_price", "low")):
        frame[dst] = frame[src].astype(float)
    frame["volume"] = frame["volume"].astype(float)
    frame["amount"] = pd.to_numeric(frame["turnover"], errors="coerce")
    return frame[["code", "date", "open", "close", "high", "low",
                  "volume", "amount"]]


def import_missing(frame, batch=2000, now=None):
    """把 dump 中库内缺失的 (code,date) 行插入 quant_hist_daily。

    返回 (inserted, latest_date, covered):
      inserted     实际插入行数;
      latest_date   dump 覆盖到的最新交易日("YYYY-MM-DD"),无数据返回 None;
      covered       已达到 latest_date 的股票代码集合(用于从东财清单剔除)。
    盘中(now < 15:05)跳过「今日」行,不写未完成 K 线。
    """
    import pandas as pd

    from quant.datasource.hist_store import insert_rows

    if frame is None or frame.empty:
        return 0, None, set()
    now = now or dt.datetime.now()
    if now.time() < _CLOSE_CUTOFF:
        # 「今日」以传入 now 为准(测试可注入;实调为当前时间),勿用 date.today()
        frame = frame[frame["date"] < now.date().strftime("%Y-%m-%d")]
        if frame.empty:
            logger.info("盘中保护:dump 仅含今日数据,不写入未完成K线")
            return 0, None, set()
    latest_date = frame["date"].max()

    import quant.db as qdb

    engine = qdb.get_engine()
    min_date = frame["date"].min()
    rows_db = pd.read_sql(
        f"SELECT code, `date` FROM quant_hist_daily WHERE `date` >= '{min_date}'",
        con=engine).values.tolist()
    # 库里 date 是 datetime.date,dump 帧 date 是 "YYYY-MM-DD" 字符串——两侧
    # 统一取 str[:10] 再比对。此前直接 tuple 对比永不相等,导致「只插缺失行」
    # 失效、每次 dump 全量 REPLACE 覆盖已有行(含东财前复权口径)。
    existing = {(str(code), str(day)[:10]) for code, day in rows_db}
    covered = set(frame[frame["date"] == latest_date]["code"])

    rows = []
    for _, r in frame.iterrows():
        if (r["code"], r["date"]) in existing:
            continue
        rows.append((r["code"], r["date"], r["open"], r["close"], r["high"],
                     r["low"], r["volume"], r["amount"],
                     None, None, None, None))
    inserted = 0
    for i in range(0, len(rows), batch):
        insert_rows(rows[i:i + batch])
        inserted += len(rows[i:i + batch])
    logger.info("dump 导入:插入 %d 行缺失K线(dump 至 %s,已齐 %d 只)",
                inserted, latest_date, len(covered))
    return inserted, latest_date, covered


def rescue_with_dump(codes, threshold=RESCUE_THRESHOLD, target_date=None,
                     logger=None):
    """快照 fallback 大批量时的 dump 救场:返回**仍需东财**的代码清单。

    codes ≤ threshold(真除权/新股,少量)不启用 dump,原样返回,保持
    东财前复权精确口径;大批量(THS 限流特征)先 dump 批量补,再把已补
    齐的剔除。dump 不可用时原样返回(东财兜底,与旧链路一致)。

    target_date(增量链的目标日)与 covered 判定:
    dump 文件的当日收盘数据**隔天才会进文件**。当 fallback 大批量的
    缺口是「当日」时(THS 快照层限流失败,dump 尚无当日),covered 只是
    「已到 dump 最新日」≠ 已到目标日——不能据此把股票从东财清单剔除,
    否则当日 K 线整批断档且东财兜底被错误清空(2026-10-21 即此故障:
    4853 只缺 10-21 被当已补齐,全市场当日只剩 714 只,纸面交易 T+N
    全部无法回填)。因此 latest < target_date 时全量保留给东财逐只。
    """
    codes = list(codes or [])
    if len(codes) <= threshold:
        return codes
    log = logger or logging.getLogger("ths_dump")
    log.info("fallback %d 只 > %d(THS 限流特征),先走 dump 批量层",
             len(codes), threshold)
    frame = download_daily_k()
    if frame is None:
        log.warning("dump 层不可用,全部走东财逐只(旧链路)")
        return codes
    _, latest, covered = import_missing(frame)
    if not latest:
        return codes
    if target_date is not None:
        try:   # import_missing 返回 "YYYY-MM-DD" 字符串,统一成 date 再比较
            latest_day = (latest if isinstance(latest, dt.date)
                          else dt.datetime.strptime(str(latest), "%Y-%m-%d").date())
        except ValueError:
            latest_day = None
        if latest_day is None or latest_day < target_date:
            log.warning("dump 最新日 %s 未到目标日 %s(当日数据隔天才进文件):"
                        "dump 只补历史,本批 %d 只全量保留给东财逐只补当日",
                        latest, target_date, len(codes))
            return codes
    remaining = [c for c in codes if c not in covered]
    log.info("dump 层补齐 %d 只,剩余 %d 只走东财",
             len(codes) - len(remaining), len(remaining))
    return remaining


def rescue_failed(codes, logger=None):
    """东财 backfill 失败清单的 dump 尾救:返回仍失败的代码(无阈值)。

    10 日窗口内的缺口(近几日断档)能救则救;更早的长缺口 dump 覆盖
    不到,如实留在失败清单里等待后续/次日链路。
    """
    codes = list(codes or [])
    if not codes:
        return []
    log = logger or logging.getLogger("ths_dump")
    frame = download_daily_k()
    if frame is None:
        log.warning("dump 尾救不可用,%d 只保持失败", len(codes))
        return codes
    _, latest, covered = import_missing(frame)
    if not latest:
        return codes
    remaining = [c for c in codes if c not in covered]
    log.info("dump 尾救:救回 %d 只,仍失败 %d 只",
             len(codes) - len(remaining), len(remaining))
    return remaining


__all__ = ["download_daily_k", "import_missing", "rescue_with_dump",
           "rescue_failed", "RESCUE_THRESHOLD"]
