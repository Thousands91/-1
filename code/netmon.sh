#!/bin/bash
# 网络心跳黑匣子：每60s一条记录。掉线后回看这文件，能区分"网络先断"还是"服务端踢人"
LOG=/root/netmon.log
while true; do
  TS=$(date '+%m-%d %H:%M:%S')
  P=$(ping -c1 -W2 223.5.5.5 2>/dev/null | awk -F'time=' '/time=/{print $2}' | cut -d' ' -f1)
  [ -z "$P" ] && P=FAIL
  T=$(cat /sys/class/power_supply/battery/temp 2>/dev/null)
  echo "$TS ping=${P}ms batt=${T}" >> "$LOG"
  if [ "$(stat -c %s "$LOG" 2>/dev/null)" -gt 2097152 ]; then
    tail -c 1048576 "$LOG" > "$LOG.t" && mv "$LOG.t" "$LOG"
  fi
  sleep 60
done
