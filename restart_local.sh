#!/bin/sh
# 本地调试用的服务重启脚本（verify.py 的 RESTART_CMD）
cd /workspace
if [ -f /tmp/rulesvc.pid ]; then
  kill "$(cat /tmp/rulesvc.pid)" 2>/dev/null
  sleep 1
fi
PORT=18080 DATA_DIR=/tmp/rulesvc-data SITES=alpha,beta,gamma,delta \
  nohup python3 -m app.server >/tmp/rulesvc.log 2>&1 &
echo $! > /tmp/rulesvc.pid
