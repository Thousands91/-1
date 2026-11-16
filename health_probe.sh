#!/bin/bash
# health_probe v2 (2026-11-16)：+IP/网卡记录——掉线归因仪器
# 目的：冻结说 vs IP变动说，不再猜，下次掉线用数据裁决
# 输出：时间 elapsed uptime ip=X if=wlan0 batt=N%（ip取默认路由源地址，本机调用不花钱）
LAST=$(date +%s)
while true; do
  sleep 60
  NOW=$(date +%s)
  TS=$(date '+%m-%d %H:%M:%S')
  EL=$((NOW-LAST)); LAST=$NOW
  UP=$(cut -d. -f1 /proc/uptime 2>/dev/null)
  RT=$(ip -4 route get 1.1.1.1 2>/dev/null | head -1)
  IP=$(echo "$RT" | grep -o 'src [0-9.]*' | awk '{print $2}')
  IF=$(echo "$RT" | grep -o 'dev [a-zA-Z0-9]*' | awk '{print $2}')
  [ -z "$IP" ] && IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  BATT=$(cat /sys/class/power_supply/battery/capacity 2>/dev/null || echo "?")
  echo "$TS elapsed=${EL}s uptime=${UP}s ip=${IP:-无} if=${IF:-无} batt=${BATT}%" >> /root/health.log
done
