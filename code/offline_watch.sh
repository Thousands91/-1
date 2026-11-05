#!/bin/bash
# 掉线取证器：检测到"账号状态变更为离线"立即抓现场快照到 offline_forensics.log
CONF=/root/Napcat/opt/QQ/resources/app/app_launcher/napcat
OUT=/root/offline_forensics.log
NEWEST=$(ls -t "$CONF/napcat/logs/"*.log /root/napcat-restart.log 2>/dev/null | head -1)
PREV=0
[ -n "$NEWEST" ] && PREV=$(grep -c "账号状态变更为离线" "$NEWEST" 2>/dev/null || echo 0)
while true; do
  NEWEST=$(ls -t "$CONF/napcat/logs/"*.log /root/napcat-restart.log 2>/dev/null | head -1)
  if [ -n "$NEWEST" ]; then
    N=$(grep -c "账号状态变更为离线" "$NEWEST" 2>/dev/null || echo 0)
    if [ "${N:-0}" -gt "$PREV" ]; then
      {
        echo "===== 掉线事件 $(date '+%m-%d %H:%M:%S') ====="
        echo "来源: $NEWEST"
        echo "-- 事发前网络心跳(最近8条) --"
        tail -8 /root/netmon.log 2>/dev/null
        echo "-- 即时网络 --"
        ping -c2 -W2 223.5.5.5 2>&1 | tail -2
        echo "-- 系统状态 --"
        uptime
        free -m | head -2
        df -h / | tail -1
        echo "电量: $(cat /sys/class/power_supply/battery/capacity 2>/dev/null)"
        echo "QQ进程数: $(pgrep -fc 'opt/QQ/qq' 2>/dev/null)"
        echo "-- 日志尾部 --"
        tail -15 "$NEWEST" 2>/dev/null
      } >> "$OUT" 2>&1
      PREV=$N
    fi
  fi
  sleep 10
done
