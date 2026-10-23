#!/bin/bash
# 运行 InStock 作业
# 用法: bash scripts/run_job.sh [作业名 ...]   不带参数则运行完整每日任务 execute_daily_job
# 示例: bash scripts/run_job.sh init_job
#       bash scripts/run_job.sh basic_data_daily_job
#       bash scripts/run_job.sh execute_daily_job 2025-09-30        # 指定日期
#       bash scripts/run_job.sh execute_daily_job 2025-09-30 2025-10-12  # 日期区间回补
ROOT="/Users/zhaochangji/Documents/stock"
INSTOCK="$ROOT/instock"          # InStock 仓库根目录(包为 $INSTOCK/instock)
PY="$INSTOCK/.venv/bin/python"
export PYTHONPATH="$INSTOCK"     # 供 import instock.* 使用

if [ $# -eq 0 ]; then
  set -- execute_daily_job
fi

for job in "$@"; do
  # 参数形如 name 或 name:arg1,arg2
  name="${job%%:*}"
  echo "==== [$(date '+%H:%M:%S')] 运行作业: $name ===="
  if [[ "$job" == *:* ]]; then
    args="${job#*:}"
    "$PY" "$INSTOCK/instock/job/$name.py" ${args//,/ }
  else
    "$PY" "$INSTOCK/instock/job/$name.py"
  fi
done
echo "==== [$(date '+%H:%M:%S')] 全部作业完成 ===="
