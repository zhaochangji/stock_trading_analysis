#!/bin/bash
# 等到指定 HH:MM 再执行命令(nohup 挂后台后防会话重启丢任务)
# 用法: nohup bash scripts/wait_and_run.sh 15:35 bash scripts/daily_close.sh >/dev/null 2>&1 &
TARGET="$1"; shift
while :; do
  now=$(date +%H:%M)
  if [[ "$now" == "$TARGET" || "$now" > "$TARGET" ]]; then
    break
  fi
  sleep 20
done
exec "$@"
