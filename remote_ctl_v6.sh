#!/bin/bash
# remote_ctl_v6.6 (2026-11-17)：+git push低速保护+PBOX防堆积(根治回传卡死)
# 病根：手机侧 api.github.com 小包 GET 通([api:200])，但回传的 PUT 带 100KB+ 大包
# 被掐死（report:API回传失败 后错误体为空=请求被吞），285710 结果卡待发箱。
# git 通道([git:通])实测健康，拉命令本来就走它——回传搬上同一条路。
# 兜底：git push 失败自动回退 v5 的 api PUT 老路，绝不弄死链路。
termux-wake-lock 2>/dev/null
REPO=/root/asar-sync
LOG=/root/remote_ctl.log
QR=/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/cache/qrcode.png
BASE="https://api.github.com/repos/Thousands91/-1/contents/remote"
CMD_URL="$BASE/cmd.txt"
OUT_URL="$BASE/out.txt"
PBOX=/root/.remote_pending
mkdir -p "$PBOX"

# ---- 启动：原子实例锁 v6.2(mkdir原子性,同秒双开也只有一个能活;含死锁自清) ----
LD=/root/.remote_ctl.lock
if mkdir "$LD" 2>/dev/null; then
  echo $$ > "$LD/pid"
else
  LP=$(cat "$LD/pid" 2>/dev/null)
  if [ -n "$LP" ] && kill -0 "$LP" 2>/dev/null && tr -d '\0' < "/proc/$LP/cmdline" 2>/dev/null | grep -q remote_ctl; then
    echo "[$(date '+%m-%d %H:%M:%S')] 实例锁:已有实例在岗(pid=$LP),本次启动退出——谁拉起的我记下了" >> "$LOG"
    exit 0
  fi
  rm -rf "$LD" && mkdir "$LD" && echo $$ > "$LD/pid"
fi
trap 'rm -rf "$LD" 2>/dev/null' EXIT
echo $$ > /root/.remote_pid

get_tok() {
  printf 'protocol=https\nhost=github.com\n\n' | timeout 10 git credential fill 2>/dev/null | sed -n 's/^password=//p' | head -1
}
TOK=$(get_tok)
[ -n "$TOK" ] || TOK=$(grep -oE 'ghp_[A-Za-z0-9]+' /root/.git-credentials 2>/dev/null | head -1)
cd "$REPO" || { echo "[$(date '+%m-%d %H:%M')] v6致命:asar-sync仓库不在" >> "$LOG"; exit 1; }
LAST=0
[ -f /root/.remote_last ] && LAST=$(cat /root/.remote_last)

# ---- 拉命令（沿用v5：git主路+raw兜底） ----
get_cmd() {
  if timeout 25 git fetch origin main >/dev/null 2>&1; then
    RAW=$(git show origin/main:remote/cmd.txt 2>/dev/null)
    case "$RAW" in
      [0-9]*) echo "$RAW"; return 0 ;;
    esac
  fi
  [ -n "$TOK" ] || TOK=$(get_tok)
  timeout 30 curl -s -H "Authorization: token $TOK" \
    "https://raw.githubusercontent.com/Thousands91/-1/main/remote/cmd.txt?cb=$(date +%s)"
}

run_task() {
  # 双执行保险:落笔前复查LAST,别的实例已执行过就直接放弃
  [ -f /root/.remote_last ] && [ "$1" -le "$(cat /root/.remote_last 2>/dev/null || echo 0)" ] && return 0
  echo "$1" > /root/.remote_last
  LAST=$1
  O="$PBOX/task_$1.txt"
  { echo "===== 任务$1 $(date '+%m-%d %H:%M:%S') ====="; echo "--- 命令 ---"; tail -n +2 "$2"; echo "--- 输出 ---"; tail -n +2 "$2" | timeout 240 bash 2>&1; } > "$O"
  cat "$O" >> "$LOG"
}

make_bundle() {
  # 防堆积:待发文件>3个时只取最新3个,旧的丢弃(回传卡死时不让bundle越滚越大)
  ALL=$(ls -1t "$PBOX" 2>/dev/null)
  [ -n "$ALL" ] || return 1
  DROPPED=$(echo "$ALL" | tail -n +4)
  [ -n "$DROPPED" ] && echo "$DROPPED" | while read -r d; do rm -f "$PBOX/$d" 2>/dev/null; done
  F=$(echo "$ALL" | head -3)
  : > /tmp/remote_bundle.txt
  for f in $F; do echo "<<<$f>>>" >> /tmp/remote_bundle.txt; cat "$PBOX/$f" >> /tmp/remote_bundle.txt; done
  return 0
}

api_report() {
  make_bundle || return 0
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

# ---- v6核心：回传走git push（与拉命令同通道） ----
git_report() {
  make_bundle || return 0
  cp /tmp/remote_bundle.txt "$REPO/remote/out.txt"
  if git add remote/out.txt 2>/dev/null && \
     git -c user.name=asar-x10 -c user.email=asar-x10@localhost commit -m "遥控输出(git) $(date '+%m-%d %H:%M')" >/dev/null 2>&1; then
    if timeout 120 git -c http.lowSpeedLimit=0 -c http.lowSpeedTime=999 pull --rebase origin main >/dev/null 2>&1 && timeout 120 git -c http.lowSpeedLimit=0 -c http.lowSpeedTime=999 push origin main >/dev/null 2>&1; then
      rm -f "$PBOX"/*
      echo "[$(date '+%m-%d %H:%M')] report:git回传成功" >> "$LOG"
      return 0
    fi
    echo "[$(date '+%m-%d %H:%M')] report:git push失败,回退API" >> "$LOG"
    git rebase --abort 2>/dev/null
    git reset --hard HEAD~1 2>/dev/null
  else
    echo "[$(date '+%m-%d %H:%M')] report:git commit失败,回退API" >> "$LOG"
  fi
  api_report
}

NAP_FUSE=/root/.napcat_pulls
restart_napcat() {
  # 坑117终:重启取证快照——拍下临终日志送GitHub云端,本地日志清了也有案底
  { echo "===== 重启取证 $(date '+%m-%d %H:%M:%S') ====="
    echo "--- napcat.log尾150行 ---"; tail -n 150 /root/napcat.log 2>/dev/null
    echo "--- napcat-restart.log尾80行 ---"; tail -n 80 /root/napcat-restart.log 2>/dev/null
  } > "$PBOX/forensic_restart_$(date +%m%d_%H%M).txt" 2>/dev/null
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
  cd /root/Napcat && nohup xvfb-run -a /root/Napcat/opt/QQ/qq -q 3427943187 --no-sandbox >> /root/napcat-restart.log 2>&1 &
  cd "$REPO"
  # 坑117:重启验尸——60s/300s后各记一次登录判定+最新登录错误(证据在napcat-restart.log,grep必须带-a防二进制静默)
  ( sleep 60; E1=$(grep -a "Login Error" /root/napcat-restart.log 2>/dev/null | tail -1); S1=$(bash /root/logincheck.sh 2>/dev/null | grep -aE "\[在线\]|\[未登录\]" | head -1); echo "[$(date '+%m-%d %H:%M')] 验尸+60s:[$S1] 最新登录错误:${E1:-无}" >> "$LOG"; sleep 240; S2=$(bash /root/logincheck.sh 2>/dev/null | grep -aE "\[在线\]|\[未登录\]" | head -1); echo "[$(date '+%m-%d %H:%M')] 验尸+300s:[$S2]" >> "$LOG" ) &
  return 0
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
        git_report
      fi
      ;;
    "") echo "[$(date '+%m-%d %H:%M')] intake:API无响应" >> "$LOG" ;;
    *) echo "[$(date '+%m-%d %H:%M')] intake:API响应异常" >> "$LOG" ;;
  esac
  git_report
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
  if [ $((C % 60)) -eq 0 ]; then
    echo "[$(date '+%m-%d %H:%M')] 心跳:循环正常(v6.6-git,LAST=$LAST)" >> "$LOG"
    for LF in /root/napcat.log /root/napcat-restart.log "$LOG"; do
      if [ -f "$LF" ] && [ "$(wc -l < "$LF")" -gt 20000 ]; then
        tail -n 20000 "$LF" > "$LF.t" && cat "$LF.t" > "$LF" && rm -f "$LF.t"
        echo "[$(date '+%m-%d %H:%M')] 日志保全:$LF超2万行,已裁剪保留尾部" >> "$LOG"
      fi
    done
  fi
  fi
  sleep 10
done
