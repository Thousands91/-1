#!/bin/bash
# health_probe v3.1 (2026-11-16)：charge/batt/uptime字段已删——本机proot实测读不到
# (SELinux拒/sys/class/power_supply;termux-battery-status未装且/data/data不可见;/proc/uptime静态假值)
# Doze裁决改靠:elapsed断档(冻结)+ip.log(时粒IP)+用户充电行为。官方依据:充电中Doze不触发(source.android.com)
# 目的：冻结说 vs IP变动说，不再猜，下次掉线用数据裁决
# 输出：时间 elapsed uptime ip=X if=wlan0 batt=N%（ip取默认路由源地址，本机调用不花钱）
LAST=$(date +%s)
while true; do
  sleep 60
  NOW=$(date +%s)
  TS=$(date '+%m-%d %H:%M:%S')
  EL=$((NOW-LAST)); LAST=$NOW
  RT=$(ip -4 route get 1.1.1.1 2>/dev/null | head -1)
  IP=$(echo "$RT" | grep -o 'src [0-9.]*' | awk '{print $2}')
  IF=$(echo "$RT" | grep -o 'dev [a-zA-Z0-9]*' | awk '{print $2}')
  [ -z "$IP" ] && IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  echo "$TS elapsed=${EL}s ip=${IP:-无} if=${IF:-无}" >> /root/health.log
done
