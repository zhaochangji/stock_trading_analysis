#!/bin/bash
# THS market-dump 并行下载脚本(nohup 脱离会话运行,Range 分片加速)
# 用法: nohup bash scripts/download_ths_dump.sh [daily-k|adjustment-factors|daily-k-10d] > data/download.log 2>&1 &
set -u
KIND="${1:-daily-k}"
ROOT="/Users/zhaochangji/Documents/stock"
DATA="$ROOT/data"
PY="$ROOT/instock/.venv/bin/python"

KEY=$(awk -F': ' '/ths_api_key/{print $2}' "$ROOT/config/secrets.yaml" | tr -d ' ')
URL=$("$PY" - "$KEY" "$KIND" <<'EOF'
import sys, json, urllib.request
key, kind = sys.argv[1], sys.argv[2]
req = urllib.request.Request(f"https://fuyao.aicubes.cn/api/dump/market-dumps/{kind}/download-url",
                             headers={"X-api-key": key})
with urllib.request.urlopen(req, timeout=20) as r:
    print((json.load(r).get("data") or {}).get("presigned_url", ""))
EOF
)
if [ -z "$URL" ]; then echo "$(date +%T) 获取 $KIND 链接失败"; exit 1; fi
echo "$(date +%T) $KIND 链接获取成功"

TOTAL=$("$PY" - "$URL" <<'EOF'
import sys
import requests
try:
    r = requests.get(sys.argv[1], timeout=30, stream=True, allow_redirects=True)
    print(r.headers.get("Content-Length") or 0)
    r.close()
except Exception:
    print(0)
EOF
)
if [ -z "$TOTAL" ] || [ "$TOTAL" -le 0 ]; then echo "无法获取文件大小"; exit 1; fi
echo "$(date +%T) 文件总大小: $TOTAL bytes ($((TOTAL/1024/1024)) MB)"

N=16
CHUNK=$(( (TOTAL + N - 1) / N ))
OUT="$DATA/ths_${KIND}.parquet"
rm -f "$DATA"/part_* "$OUT"

for i in $(seq 0 $((N-1))); do
  START=$((i*CHUNK)); END=$(( (i+1)*CHUNK - 1 ))
  [ $END -ge $TOTAL ] && END=$((TOTAL-1))
  [ $START -ge $TOTAL ] && break
  curl -s -m 270 --range ${START}-${END} -o "$DATA/part_$(printf %02d $i)" "$URL" &
done
wait

# 合并并校验
cat "$DATA"/part_* > "$OUT" 2>/dev/null
GOT=$(stat -f %z "$OUT" 2>/dev/null || echo 0)
echo "$(date +%T) 已下载: $GOT / $TOTAL"
if [ "$GOT" = "$TOTAL" ]; then
  rm -f "$DATA"/part_*
  echo "$(date +%T) $KIND 下载完成并校验通过"
else
  echo "$(date +%T) 大小不匹配,分片保留待补"
  exit 2
fi
