#!/bin/bash
# 阿萨尔同步脚本：手机日志上仓库 / 仓库代码下手机
REPO=/root/asar-sync
LOG=/tmp/fakelog
BOT_DIR=/root/bot/bot
BRANCH=main

cd "$REPO" || exit 1
git config user.name "asar-phone" 2>/dev/null
git config user.email "asar@phone.local" 2>/dev/null

push() {
    [ -f "$LOG" ] || { echo "找不到日志 $LOG"; return 1; }
    mkdir -p logs
    tail -3000 "$LOG" > logs/bot.log
    git add logs/bot.log
    if [ -z "$(git status --porcelain -- logs/bot.log)" ]; then
        echo "日志无新内容"
        return 0
    fi
    git add logs/bot.log
    git commit -m "日志 $(date '+%m-%d %H:%M')" -q
    if git push origin $BRANCH 2>&1 | grep -q rejected; then
        git pull --rebase origin $BRANCH && git push origin $BRANCH
    fi
    echo "✅ 日志已推送 $(date '+%H:%M')"
}

pull() {
    git add -A
    git diff --cached --quiet || git commit -q -m "本地快照 $(date '+%H:%M')"
    git pull --rebase origin $BRANCH || return 1
    # 只覆盖代码，绝不动 data/（记忆和棋局都在里面）
    if [ -d "$REPO/code/bot" ]; then
        cp -a "$REPO/code/bot/." "$BOT_DIR/"
        find "$BOT_DIR" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null
        echo "✅ 代码已更新到 $BOT_DIR（data/ 未动）"
        echo "如需生效请重启大脑：pkill -f 'python main.py'; cd $BOT_DIR && nohup python main.py > /root/bot/bot/bot.log 2>&1 &"
    else
        echo "仓库里还没有 code/bot/，无需更新"
    fi
}

loop() {
    while true; do
        sleep 3600
        push > /root/asar_sync.log 2>&1
    done
}

case "$1" in
    push) push ;;
    pull) pull ;;
    loop) loop ;;
    *) echo "用法: ~/asar_sync.sh push|pull|loop" ;;
esac
