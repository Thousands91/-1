#!/bin/bash
# 阿萨尔一键诊断：不回复时跑这个，直接看结论
BOT_LOG=/root/bot/bot/bot.log
NAP_LOG=/root/napcat.log

echo "================ 阿萨尔诊断 $(date '+%m-%d %H:%M:%S') ================"

# 1. 进程
nap=$(ps aux | grep "opt/QQ/qq" | grep -v grep | wc -l)
brain=$(ps aux | grep "python main.py" | grep -v grep | wc -l)
echo "NapCat进程: ${nap}个 | 大脑进程: ${brain}个"

# 2. 关键时间点
last_connect=$(grep -i "connected" "$BOT_LOG" 2>/dev/null | tail -1 | awk '{print $1, $2}')
last_closed=$(grep -i "closed by peer" "$BOT_LOG" 2>/dev/null | tail -1 | awk '{print $1, $2}')
last_msg=$(grep "message.private\|message.group" "$BOT_LOG" 2>/dev/null | tail -1 | awk '{print $1, $2}')
last_err=$(grep "\[ERROR\]" "$BOT_LOG" 2>/dev/null | tail -1 | awk '{print $1, $2}')
last_qr=$(grep "二维码已保存" "$NAP_LOG" 2>/dev/null | tail -1 | grep -o "^[0-9-]* [0-9:]*" )

echo "最近一次连上: ${last_connect:-无记录}"
echo "最近一次被踢: ${last_closed:-无}"
echo "最近收到的消息: ${last_msg:-无}"
echo "最近一次报错: ${last_err:-无}"
echo "napcat最近出二维码: ${last_qr:-无}"

# 3. 结论
echo "---------------- 诊断结论 ----------------"
# 时间都是 "MM-DD HH:MM:SS" 格式，直接用字符串比较（同年内可靠）
if [ "$brain" = "0" ]; then
    echo "❌ 大脑进程死了。修复: pkill -f 'python main.py'; sleep 2; cd /root/bot/bot && nohup python main.py > /root/bot/bot/bot.log 2>&1 &"
elif [ "$nap" = "0" ]; then
    echo "❌ NapCat死了。需要重启并扫码（用之前的扫码一条命令）。"
elif [ -n "$last_closed" ] && [ -n "$last_connect" ] && [[ "$last_closed" > "$last_connect" ]]; then
    echo "❌ 掉线了：被踢($last_closed)晚于连上($last_connect)。是腾讯踢的，需要重新扫码。"
elif [ -n "$last_qr" ] && [[ "$last_qr" > "${last_connect:-00-00 00:00:00}" ]]; then
    echo "❌ NapCat在等扫码（二维码 $last_qr 晚于上线 $last_connect）。扫码登录即可。"
elif [ -n "$last_err" ] && [ -n "$last_msg" ] && [ "$(ts "$last_err")" -gt "$(ts "$last_msg")" ]; then
    echo "⚠️ 收到过消息但之后有报错，可能是处理时崩了。把下面这行发给AI分析："
    grep "\[ERROR\]" "$BOT_LOG" | tail -3
else
    echo "✅ 进程和连接都正常。若仍不回复，可能是消息没到（群端问题）或模型API慢，等1分钟再试一次，仍不行就跑: cd /root/asar-sync && bash asar_sync.sh push 然后让AI看日志。"
fi
echo "================================================"
