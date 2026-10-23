#!/bin/bash
# 15:35 收盘全链:①K线增量(THS快照18秒) → ②缓存预热(40秒) → ③InStock每日全链(资金流/spot终值)
#   → ④收盘复盘(内嵌 M3 全市场扫描,选股表带推荐分/依据;推送 APP) → ⑤模拟盘T+N追踪 → ⑥指标/形态/策略/回测
# 顺序原则:报告依赖的资金流/spot 终值来自③;报告本身携带 M3 扫描;模拟盘追踪依赖当日K线,放在K线增量后。
ROOT="/Users/zhaochangji/Documents/stock"
PY="$ROOT/instock/.venv/bin/python"
LOG="$ROOT/reports/daily_close_$(date +%Y%m%d).log"
LOCK="$ROOT/reports/.locks/daily_close.lockd"
cd "$ROOT" || exit 1
mkdir -p "$ROOT/reports"

# 目录锁:已在运行则直接退出(防 launchd 与定时器双触发;macOS 无 flock 命令)。
# 孤儿锁自愈:上次实例被 kill/崩溃时 trap 不会执行,锁目录会残留并让之后每天
# 的收盘链全部静默跳过(2026-10-16/10-19 即此故障:模拟盘 T+N 与 K 线断档)。
# 锁龄超过 300 分钟(review 窗口 240 分钟 + 缓冲)视为孤儿,自动清除重跑。
if ! mkdir "$LOCK" 2>/dev/null; then
  lock_age_min=$(( ( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || echo 0) ) / 60 ))
  if [ "$lock_age_min" -ge 300 ]; then
    echo "$(date '+%F %T') 清除孤儿锁(锁龄 ${lock_age_min} 分钟)后重跑" >> "$LOG"
    rmdir "$LOCK" 2>/dev/null
    mkdir "$LOCK" 2>/dev/null || { echo "$(date '+%F %T') 锁重建失败,放弃" >> "$LOG"; exit 1; }
  else
    echo "$(date '+%F %T') 已有 daily_close 实例(锁龄 ${lock_age_min} 分钟),跳过" >> "$LOG"
    exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "===== daily_close start $(date '+%F %T') ====="
  # 1. K线增量(15:05 后为当日完整K线;THS 快照主通道,除权/盲区自动切东财)
  if "$PY" scripts/backfill_hist.py --incremental --workers 8; then
    # 1b. 预热 InStock 当日K线缓存(~40秒,四作业免全市场网络抓取;失败自动回退)
    "$PY" scripts/warm_instock_cache.py || echo "!! 缓存预热失败,指标作业将回退网络抓取"
    # 1c. 刷新 THS 行业指数成分映射(板块层用,~40秒;失败不影响主链)
    "$PY" scripts/refresh_ths_membership.py || echo "!! THS 板块映射刷新失败(沿用昨日映射)"
    # 1d. 除权口径核验(通达信独立除权表 × 库内前复权K线,防 dump 未复权覆盖复发;
    #     结果落 quant_adjust_check,发现断档打 !! ADJUST-SUSPECT;核验本身失败不阻断主链)
    "$PY" scripts/check_adjust.py || echo "!! 除权核验执行失败(不阻断主链)"
  else
    echo "!! K线增量失败(THS与东财均未成功),日报将按最近交易日生成"
  fi
  # 2. InStock 每日全链(刷新 web 各页数据 + spot/资金流收盘终值)
  bash scripts/run_job.sh execute_daily_job || echo "!! execute_daily_job 失败"
  # 3. 收盘复盘(内嵌 M3 全市场扫描:推荐分10制+选定依据列;完整版推送 APP)
  "$PY" scripts/daily_report.py || echo "!! 日报生成失败"
  # 4. 用完成的历史K线回填模拟盘 close_t0/T+1/T+3/T+5/T+10 与胜负字段
  "$PY" -c 'from quant.paper import update_tracking; print("paper tracking", update_tracking())' \
    || echo "!! 模拟盘T+N追踪失败"
  # 4c. 战法可行性追踪:三路战法候选落库 + 回填 T+N 胜负(战法页统计来源)
  "$PY" scripts/log_strategies.py || echo "!! 战法追踪落库/回填失败(不影响后续步骤)"
  # 4b. 纸面模拟盘账户结算(只重算净值,不重复买入;买入由 14:40 尾盘任务执行)
  "$PY" scripts/sim_engine.py --no-buy || echo "!! 模拟盘结算失败"
  # 5. 指标/形态/策略/回测(读预热缓存,~13 分钟)
  bash scripts/run_job.sh indicators_data_daily_job klinepattern_data_daily_job strategy_data_daily_job backtest_data_daily_job \
    || echo "!! 指标/形态/策略/回测 部分失败(明日重跑)"
  echo "===== daily_close done $(date '+%F %T') ====="
} >> "$LOG" 2>&1
