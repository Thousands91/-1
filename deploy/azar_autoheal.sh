#!/bin/bash
# azar_autoheal.sh v1.2 — 掉线自动愈合（独立进程，与 worker 解耦）
# 背景：v6.6 的看护寄生在 remote_ctl_v6.sh 主循环里，worker 一旦卡在回传/被冻结，
#       看护停摆 + 告警发不出去 = 掉线9小时没人知道（11-19 事故）。
# 职责（只做两件安全的事，不反复 pkill 触发风控）：
#   ① 进程存活守护：大脑/QQ 进程死了就拉起
#   ② 掉线即时告警：走独立 GitHub API 通道，worker 卡住也能发出去
# 边界：QQ 被服务端踢下线后协议层无法免扫码恢复（手册坑69源码级定案），
#       本脚本不承诺免扫码登录，只保证"掉线第一时间报警 + 进程自愈"，把介入成本降到最低。
# v1.1（2026-11-19 用户掉线相关性猜想）：掉线告警自带"距最后发送/最后收信秒数"，
#       从 napcat.log 取最后一条 发送->/接收<- 的时间戳算间隔，长期积累数据验证猜想。
# v1.2（2026-11-20）：① 掉线告警再加"本次在线X分"（会话寿命记账，验证"越扫越短"假设）；
#       ② 每轮循环续持 termux-wake-lock——息屏不深睡、worker 回传不再断（用户不用一直亮屏）。

LD=/root/.autoheal.lock
if mkdir "$LD" 2>/dev/null; then
  echo $$ > "$LD/pid"
else
  LP=$(cat "$LD/pid" 2>/dev/null)
  if [ -n "$LP" ] && kill -0 "$LP" 2>/dev/null && tr -d '\0' < "/proc/$LP/cmdline" 2>/dev/null | grep -q autoheal; then
    exit 0
  fi
  rm -rf "$LD"; mkdir "$LD"; echo $$ > "$LD/pid"
fi
trap 'rm -rf "$LD" 2>/dev/null' EXIT

LOG=/root/autoheal.log
PBOX=/root/.remote_pending
mkdir -p "$PBOX"
say() { echo "[$(date '+%m-%d %H:%M:%S')] $*" >> "$LOG"; }

get_tok() { printf 'protocol=https\nhost=github.com\n\n' | timeout 10 git credential fill 2>/dev/null | sed -n 's/^password=//p' | head -1; }
TOK=$(get_tok)
[ -n "$TOK" ] || TOK=$(grep -oE 'ghp_[A-Za-z0-9]+' /root/.git-credentials 2>/dev/null | head -1)

ALERT_URL="https://api.github.com/repos/Thousands91/-1/contents/remote/alert.txt"

# 告警双通道：① 独立 API 直推 GitHub（绕开卡住的 worker 的 git bundle）；② 留一份进 PBOX 兜底
send_alert() {
  local msg="$(date '+%F %T') $1"
  say "ALERT: $1"
  echo "$msg" > "$PBOX/ALERT.txt" 2>/dev/null
  [ -n "$TOK" ] || { say "告警:无token,仅留PBOX"; return 1; }
  local sha=$(timeout 20 curl -s -H "Authorization: token $TOK" "$ALERT_URL?cb=$(date +%s)" | tr -d ' ' | grep -o '"sha":"[^"]*"' | head -1 | cut -d'"' -f4)
  local b64=$(printf '%s\n' "$msg" | base64 -w0)
  if timeout 30 curl -s -X PUT -H "Authorization: token $TOK" \
      -d "{\"message\":\"掉线告警 $(date '+%m-%d %H:%M')\",\"content\":\"$b64\"${sha:+,\"sha\":\"$sha\"}}" \
      "$ALERT_URL" 2>/dev/null | grep -q '"content"'; then
    say "告警:已推送GitHub"
  else
    say "告警:推送失败,留PBOX待worker回传"
  fi
}

say "===== autoheal v1.2 启动 $(date '+%F %T') ====="
PREV=""
ON_TS=""   # 本次在线的起始时刻（v1.2 会话寿命记账用）
while true; do
  # ⓪ 息屏保活：续持 termux-wake-lock（每60秒刷一次；proot内可直调；失败静默不影响主职）
  _WL=/data/data/com.termux/files/usr/bin/termux-wake-lock
  [ -x "$_WL" ] && "$_WL" 2>/dev/null
  # ① 进程存活守护（掉线后的 NapCat 重启由 worker 的 restart_napcat 负责，这里只保"进程在"）
  pgrep -f '/root/bot/venv/bin/python main.py' >/dev/null || {
    say "大脑进程不在,拉起"
    send_alert "大脑进程异常退出,已自动拉起"
    ( cd /root/bot/bot && setsid nohup /root/bot/venv/bin/python main.py >> /root/bot/bot/bot.log 2>&1 < /dev/null & )
  }
  pgrep -f 'opt/QQ/[q]q' >/dev/null || {
    say "QQ进程不在,拉起(-q快速登录)"
    send_alert "QQ进程异常退出,已自动拉起"
    ( cd /root/Napcat && setsid nohup xvfb-run -a /root/Napcat/opt/QQ/qq -q 3427943187 --no-sandbox --disable-gpu --disable-software-rasterizer --disable-dev-shm-usage --js-flags=--max-old-space-size=512 >> /root/napcat-restart.log 2>&1 < /dev/null & )
  }

  # ② 登录态监测（权威判定走 logincheck）
  ST=$(timeout 20 bash /root/logincheck.sh 2>/dev/null | grep -oE '\[在线\]|\[掉线\]|\[未登录\]' | head -1)
  [ -z "$ST" ] && ST="[未知]"
  if [ "$ST" != "$PREV" ]; then
    say "登录态: ${PREV:-初始} -> $ST"
    case "$ST" in
      *未登录*|*掉线*)
        # v1.2：本次在线时长（优先本进程记账；进程重启丢了就查 login_state.log 最后一次上线时刻）
        _ON=""
        if [ -n "$ON_TS" ]; then
          _ON=$(( ( $(date +%s) - ON_TS + 30 ) / 60 ))
        else
          _OT=$(grep -E "状态变化.*-> \[在线\]" /root/login_state.log 2>/dev/null | tail -1 | grep -oE "^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")
          if [ -n "$_OT" ]; then
            _OB=$(date -d "$_OT" +%s 2>/dev/null) && _ON=$(( ( $(date +%s) - _OB + 30 ) / 60 ))
          fi
        fi
        # v1.1：掉线告警带"距最后发送/最后收信秒数"，积累掉线-交互相关性数据
        _LT=$(grep "发送 ->" /root/napcat.log 2>/dev/null | grep -oE "11-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}" | tail -1)
        _LR=$(grep "接收 <-" /root/napcat.log 2>/dev/null | grep -oE "11-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}" | tail -1)
        _G=""
        if [ -n "$_LT" ]; then
          _B=$(date -d "$(date +%Y)-${_LT}" +%s 2>/dev/null)
          [ -n "$_B" ] && _G="距最后发送$(( $(date +%s) - _B ))秒"
        fi
        if [ -n "$_LR" ]; then
          _B2=$(date -d "$(date +%Y)-${_LR}" +%s 2>/dev/null)
          [ -n "$_B2" ] && _G="${_G}/距最后收信$(( $(date +%s) - _B2 ))秒"
        fi
        send_alert "阿萨尔掉线($ST)! 本次在线${_ON:-?}分/${_G:-间隔未知};已自愈,5分钟不恢复需本人扫码" ;;
      *在线*)
        ON_TS=$(date +%s)
        [ -n "$PREV" ] && send_alert "阿萨尔已恢复在线" ;;
    esac
    PREV="$ST"
  fi
  sleep 60
done
