#!/bin/bash
# remote_ctl_v5 (2026-11-12)：拉命令改走 git 协议，根治 raw CDN 缓存卡 7 分钟问题
# 证据：cmd 19:29 写入 → X10 19:36:49 才拉到，raw.githubusercontent CDN
# （+运营商缓存）吞掉 ?cb= 参数，缓存窗口 5 分钟级。回传侧 api.github.com
# 直写本来就快，不动。git fetch 实测 X10 上 3~10s 且无缓存。
# 兜底：git 拉取失败自动回退 v4 的 raw curl 老路，绝不因升级把链路弄死。
termux-wake-lock 2>/dev/null
REPO=/root/asar-sync
LOG=/root/remote_ctl.log
QR=/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/cache/qrcode.png
BASE="https://api.github.com/repos/Thousands91/-1/contents/remote"
CMD_URL="$BASE/cmd.txt"
OUT_URL="$BASE/out.txt"
PBOX=/root/.remote_pending
mkdir -p "$PBOX"

# ---- 启动：接管旧循环 ----
OLDPID=$(cat /root/.remote_pid 2>/dev/null)
sleep 12   # 宽限：让旧循环把手上任务的回传跑完
if [ -n "$OLDPID" ] && kill -0 "$OLDPID" 2>/dev/null; then
  kill "$OLDPID" 2>/dev/null
  echo "[$(date '+%m-%d %H:%M')] v5接管:杀掉旧循环pid=$OLDPID" >> "$LOG"
fi
echo $$ > /root/.remote_pid

get_tok() {
  printf 'protocol=https\nhost=github.com\n\n' | timeout 10 git credential fill 2>/dev/null | sed -n 's/^password=//p' | head -1
}
TOK=$(get_tok)
[ -n "$TOK" ] || TOK=$(grep -oE 'ghp_[A-Za-z0-9]+' /root/.git-credentials 2>/dev/null | head -1)
cd "$REPO" || { echo "[$(date '+%m-%d %H:%M')] v5致命:asar-sync仓库不在" >> "$LOG"; exit 1; }
LAST=0
[ -f /root/.remote_last ] && LAST=$(cat /root/.remote_last)

# ---- 拉命令（v5核心改动） ----
get_cmd() {
  # 主路：git 协议（无CDN缓存）
  if timeout 25 git fetch origin main >/dev/null 2>&1; then
    RAW=$(git show origin/main:remote/cmd.txt 2>/dev/null)
    case "$RAW" in
      [0-9]*) echo "$RAW"; return 0 ;;
    esac
  fi
  # 兜底：v4 的 raw curl 老路
  [ -n "$TOK" ] || TOK=$(get_tok)
  timeout 30 curl -s -H "Authorization: token $TOK" \
    "https://raw.githubusercontent.com/Thousands91/-1/main/remote/cmd.txt?cb=$(date +%s)"
}

run_task() {
  echo "$1" > /root/.remote_last
  LAST=$1
  O="$PBOX/task_$1.txt"
  { echo "===== 任务$1 $(date '+%m-%d %H:%M:%S') ====="; echo "--- 命令 ---"; tail -n +2 "$2"; echo "--- 输出 ---"; tail -n +2 "$2" | timeout 240 bash 2>&1; } > "$O"
  cat "$O" >> "$LOG"
}

api_report() {
  ls "$PBOX" >/dev/null 2>&1 || return 0
  F=$(ls -A "$PBOX" 2>/dev/null); [ -n "$F" ] || return 0
  : > /tmp/remote_bundle.txt
  for f in $F; do echo "<<<$f>>>" >> /tmp/remote_bundle.txt; cat "$PBOX/$f" >> /tmp/remote_bundle.txt; done
  [ -n "$TOK" ] || TOK=$(get_tok)
  [ -n "$TOK" ] || { echo "[$(date '+%m-%d %H:%M')] report:无token,留待下轮" >> "$LOG"; return 1; }
  SHA=$(timeout 30 curl -s -H "Authorization: token $TOK" "$OUT_URL?cb=$(date +%s)" | tr -d ' ' | grep -o '"sha":"[^"]*"' | head -1 | cut -d'"' -f4)
  B64=$(base64 -w0 /tmp/remote_bundle.txt) || return 1
  R=$(timeout 60 curl -s -X PUT -H "Authorization: token $TOK" -d "{\"message\":\"遥控输出(API) $(date '+%m-%d %H:%M')\",\"content\":\"$B64\"${SHA:+,\"sha\":\"$SHA\"}}" "$OUT_URL")
  if echo "$R" | grep -q '"content"'; then
    rm -f "$PBOX"/*
    echo "[$(date '+%m-%d %H:%M')] report:API回传成功" >> "$LOG"
  else
    echo "[$(date '+%m-%d %H:%M')] report:API回传失败:$(echo "$R" | head -c 120)" >> "$LOG"
  fi
}

NAP_FUSE=/root/.napcat_pulls
restart_napcat() {
  NOW=$(date +%s)
  touch "$NAP_FUSE"
  : > /tmp/napf
  while IFS= read -r t; do
    [ -n "$t" ] && [ $((NOW - t)) -lt 86400 ] && echo "$t" >> /tmp/napf
  done < "$NAP_FUSE"
  CNT=$(wc -l < /tmp/napf)
  if [ "$CNT" -ge 3 ]; then
    echo "[$(date '+%m-%d %H:%M')] 保险丝熔断:NapCat 24h内已自动拉起${CNT}次,停止自动重启,需人工排查" >> "$LOG"
    return 1
  fi
  echo "$NOW" >> /tmp/napf && cp /tmp/napf "$NAP_FUSE"
  echo "[$(date '+%m-%d %H:%M')] 看护:自动拉起NapCat(24h内第$((CNT+1))次)" >> "$LOG"
  pkill -f "opt/QQ/qq" 2>/dev/null; sleep 3
  cd /root/Napcat && nohup xvfb-run -a /root/Napcat/opt/QQ/qq --no-sandbox >> /root/napcat-restart.log 2>&1 &
  cd "$REPO"
}

C=0
while true; do
  [ -n "$TOK" ] || TOK=$(get_tok)
  RAW=$(get_cmd)
  case "$RAW" in
    [0-9]*)
      RID=$(printf '%s\n' "$RAW" | head -1 | tr -dc 0-9)
      if [ -n "$RID" ] && [ "$RID" -gt "$LAST" ]; then
        printf '%s\n' "$RAW" > "/tmp/remote_cmd_$RID.txt"
        run_task "$RID" "/tmp/remote_cmd_$RID.txt"
        api_report
      fi
      ;;
    "") echo "[$(date '+%m-%d %H:%M')] intake:API无响应" >> "$LOG" ;;
    *) echo "[$(date '+%m-%d %H:%M')] intake:API响应异常" >> "$LOG" ;;
  esac
  api_report
  C=$((C+1))
  if [ $((C % 6)) -eq 0 ]; then
  ST=$(bash /root/logincheck.sh 2>/dev/null | grep -c "\[未登录\]\|\[掉线\]")
  if [ "$ST" = "1" ] && [ -f "$QR" ]; then
    NOW=$(date +%s); MT=$(stat -c %Y "$QR" 2>/dev/null || echo "$NOW")
    AGE=$((NOW - MT))
    if [ "$AGE" -gt 600 ]; then
      echo "[$(date '+%m-%d %H:%M')] 看护：未登录且码${AGE}s未刷新，尝试重启NapCat" >> "$LOG"
      restart_napcat
    fi
  fi
  pgrep -f "opt/QQ/qq" >/dev/null || { echo "[$(date '+%m-%d %H:%M')] 看护：NapCat不在" >> "$LOG"; restart_napcat; }
  pgrep -f "main.py" >/dev/null || { echo "[$(date '+%m-%d %H:%M')] 看护：大脑不在，拉起" >> "$LOG"; cd /root/bot/bot && nohup /root/bot/venv/bin/python main.py > /root/bot.log 2>&1 & cd "$REPO"; }
  [ $((C % 60)) -eq 0 ] && echo "[$(date '+%m-%d %H:%M')] 心跳:循环正常(v5-git,LAST=$LAST)" >> "$LOG"
  fi
  sleep 10
done
