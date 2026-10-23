#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟盘每日执行引擎(尾盘 14:40 由 scheduler 触发;收盘链结算再跑一次)。

每次执行:
  ① 卖出纪律:持仓按当前价核对止损/止盈/破MA20(T+1 当日不可卖),
     每个持仓的核对结果落 quant_sim_decisions(触发=done/未触发=hold
     含「为什么今天没卖」:止损未破/趋势完好/浮盈未达止盈线);
  ② 买入决策:战法选股可信度降序,≥50 开仓,情绪冰点空仓(纪律见
     quant/sim.py);成交与每条未买原因(冰点空仓/可信度不足/涨停买
     不进/现金不足/权限外/blocked)逐条留痕,不静默丢弃;
  ③ 记录当日净值行(现金 + 持仓市值,目标线进度);
  幂等:同日同股同动作不重复成交;收盘链第二次运行只重算净值,
  不覆盖尾盘的买入留痕(allow_buy=False 不落 buy 侧)。

只做纸面模拟,不触真实账户、不构成投资建议;目标线(3 万)是挑战线
不是承诺线,净值页如实展示差距。
"""

import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "instock"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import quant.db as qdb                     # noqa: E402
import quant.sim as sim                    # noqa: E402


def _now():
    return dt.datetime.now()


def _snapshot_prices(codes):
    """持仓/候选的当前快照价 {code: price}。"""
    return sim.snapshot_prices(codes)


def _klines(codes, days=60):
    """持仓近 N 日 K(升序 [(date, close)])——供 MA20/趋势纪律。"""
    import pandas as pd

    engine = qdb.get_engine()
    codes = list(codes)
    if not codes:
        return {}
    frame = pd.read_sql(
        "SELECT code, `date`, close FROM quant_hist_daily WHERE code IN "
        f"({','.join('%s' for _ in codes)}) ORDER BY code, `date` DESC "
        "LIMIT 400", con=engine, params=tuple(codes))
    out = {}
    for _, r in frame.iterrows():
        out.setdefault(str(r["code"]).zfill(6), []).append(
            (str(r["date"]), float(r["close"])))
    for code in out:
        out[code] = out[code][::-1]      # 升序
    return out


def _bought_today(conn, cur, code):
    cur.execute("SELECT 1 FROM quant_sim_trades WHERE trade_date=%s AND "
                "code=%s AND action='buy'", (dt.date.today(), code))
    return cur.fetchone() is not None


def _sold_today(cur, code):
    cur.execute("SELECT 1 FROM quant_sim_trades WHERE trade_date=%s AND "
                "code=%s AND action='sell'", (dt.date.today(), code))
    return cur.fetchone() is not None


def run(allow_buy=True):
    now = _now()
    sim.ensure_tables()
    created = sim.init_account()
    if created:
        print("[sim] 账户已初始化:起始 25,000,目标线(非承诺)30,000")
    account = sim.latest_account()
    cash = float(account["cash"])
    holds = sim.positions()
    codes = [h["code"] for h in holds]

    # ---- ① 卖出纪律(每个持仓的核对结果全部留痕:成交 done / 未触发 hold)----
    sells_done = []
    decisions = []
    if not holds:
        decisions.append(("sell", "-", "整体", None, "hold",
                          "当前空仓,无持仓需核对卖出纪律"))
    if holds:
        prices = _snapshot_prices(codes)
        klines = _klines(codes)
        plans = sim.plan_sells(holds, prices, klines)
        sold_codes = {code for code, _, _, _ in plans}
        for h in holds:               # 未触发卖出的:记录「为什么今天没卖」
            if h["code"] in sold_codes:
                continue
            decisions.append((
                "sell", h["code"], h.get("name"), prices.get(h["code"]),
                "hold", sim.hold_reason(h, prices.get(h["code"]),
                                        klines.get(h["code"]) or [])))
        conn = qdb.get_connection()
        try:
            with conn.cursor() as cur:
                for code, ratio, deal, reason in plans:
                    hold = next(h for h in holds if h["code"] == code)
                    qty = int(hold["qty"] * ratio / 100) * 100
                    if qty <= 0 or _sold_today(cur, code):
                        continue
                    cur.execute(
                        "INSERT IGNORE INTO quant_sim_trades "
                        "(ts, trade_date, code, name, action, price, qty, "
                        "amount, method, confidence, reason) "
                        "VALUES (%s,%s,%s,%s,'sell',%s,%s,%s,%s,NULL,%s)",
                        (now, dt.date.today(), code, hold.get("name"),
                         deal, qty, round(deal * qty, 2),
                         hold.get("method") or "", reason[:280]))
                    cash += round(deal * qty, 2)
                    left = int(hold["qty"]) - qty
                    if left > 0:
                        cur.execute(
                            "UPDATE quant_sim_positions SET qty=%s, "
                            "updated_at=%s WHERE code=%s", (left, now, code))
                    else:
                        cur.execute("DELETE FROM quant_sim_positions "
                                    "WHERE code=%s", (code,))
                    sells_done.append((code, qty, deal, reason))
                    decisions.append(("sell", code, hold.get("name"), deal,
                                      "done", reason))
            conn.commit()
        finally:
            conn.close()
        holds = sim.positions()      # 刷新
        codes = [h["code"] for h in holds]

    # ---- ② 买入决策(成交与每一条未买原因都留痕;收盘结算不覆盖尾盘买入留痕)----
    buys_done = []
    if allow_buy:
        try:
            import scripts.quant_web as qw

            payload = qw._strategy_payload()
            rows = payload.get("rows") or []
            score = (payload.get("sentiment") or {}).get("score")
        except Exception as error:  # noqa: BLE001
            print(f"[sim] 战法信号获取失败,今日不买: {error}", file=sys.stderr)
            rows, score = [], None
            decisions.append(("buy", "-", "全榜", None, "skip",
                              f"战法信号获取失败({type(error).__name__}),"
                              "今日不评估买入"))
        plans, note, skips = sim.plan_buys_verbose(
            rows, cash, account["net"], score, {h["code"] for h in holds})
        for code, name, price, reason in skips:   # 未买候选逐条留痕
            decisions.append(("buy", code, name, price, "skip", reason))
        if not plans and not skips:
            decisions.append(("buy", "-", "全榜", None, "skip",
                              "战法榜无候选标的,今日无买入评估"))
        klines = _klines([p[0] for p in plans])
        conn = qdb.get_connection()
        try:
            with conn.cursor() as cur:
                for code, name, deal, qty, method, conf, reason in plans:
                    amount = round(deal * qty, 2)
                    if amount > cash or _bought_today(conn, cur, code):
                        continue
                    kl = klines.get(code) or []
                    ma20 = (sum(c for _, c in kl[-20:]) / 20.0
                            if len(kl) >= 20 else None)
                    stop = round(max(deal * 0.93,
                                     (ma20 or 0) * 0.995), 2) if ma20 \
                        else round(deal * 0.93, 2)
                    cur.execute(
                        "INSERT IGNORE INTO quant_sim_trades "
                        "(ts, trade_date, code, name, action, price, qty, "
                        "amount, method, confidence, reason) "
                        "VALUES (%s,%s,%s,%s,'buy',%s,%s,%s,%s,%s,%s)",
                        (now, dt.date.today(), code, name, deal, qty, amount,
                         method[:60], conf, reason[:280]))
                    cur.execute(
                        "INSERT INTO quant_sim_positions "
                        "(code, name, qty, cost, buy_date, stop, method, "
                        "updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE qty=qty+VALUES(qty), "
                        "cost=(cost*qty+VALUES(cost)*VALUES(qty))"
                        "/(qty+VALUES(qty)), buy_date=VALUES(buy_date), "
                        "stop=VALUES(stop), updated_at=VALUES(updated_at)",
                        (code, name, qty, deal, dt.date.today(), stop,
                         method[:60], now))
                    cash -= amount
                    buys_done.append((code, name, deal, qty, conf, method))
                    decisions.append(("buy", code, name, deal, "done",
                                      reason))
            conn.commit()
        finally:
            conn.close()
        holds = sim.positions()

    # ---- ②b 决策留痕落库(买入检查/持仓核对/未成交原因;失败不阻断交易)----
    if decisions:
        try:
            sim.log_decisions(decisions)
        except Exception as error:  # noqa: BLE001
            print(f"[sim] 决策留痕落库失败(不影响交易): {error}",
                  file=sys.stderr)

    # ---- ③ 当日净值行 ----
    prices = _snapshot_prices([h["code"] for h in holds])
    mv = sum(int(h["qty"]) * prices.get(h["code"], float(h["cost"]))
             for h in holds)
    net = round(cash + mv, 2)
    conn = qdb.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "REPLACE INTO quant_sim_account "
                "(as_of, cash, market_value, net, start_cash, target, note) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (dt.date.today(), round(cash, 2), round(mv, 2), net,
                 account["start_cash"], account["target"],
                 (account.get("note") or "")[:200]))
        conn.commit()
    finally:
        conn.close()

    print(f"[sim] {dt.date.today()} 净值 {net:,.2f}(现金 {cash:,.2f} + 市值 "
          f"{mv:,.2f});卖出 {len(sells_done)} 笔,买入 {len(buys_done)} 笔;"
          f"目标线 {account['target']:,.0f}(进度 "
          f"{(net / float(account['target']) - 1) * 100:+.1f}% 待达成)")
    for trade in sells_done:
        print(f"[sim]   卖出 {trade[0]} x{trade[1]} @ {trade[2]:.2f} — {trade[3]}")
    for trade in buys_done:
        print(f"[sim]   买入 {trade[0]} {trade[1]} x{trade[3]} @ {trade[2]:.2f} "
              f"(可信度 {trade[4]},{trade[5]})")
    if not sells_done and not buys_done:
        print("[sim]   今日无纪律触发(空仓守则或无合格信号)")
    print(f"[sim]   决策留痕 {len(decisions)} 条"
          f"(成交/持有/未买原因已落 quant_sim_decisions)")
    return {"net": net, "cash": cash, "mv": mv,
            "sells": sells_done, "buys": buys_done}


if __name__ == "__main__":
    allow_buy = "--no-buy" not in sys.argv
    run(allow_buy=allow_buy)
