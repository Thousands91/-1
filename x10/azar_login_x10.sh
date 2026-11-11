#!/bin/bash
# 阿萨尔 NapCat 一键登录助手 —— X10版（路径与现手机一致: opt/QQ）
# 用法: bash azar_login_x10.sh
QR_DST="/sdcard/DCIM/Camera/asar_qr.png"
LOG="/root/napcat.log"
QR_VALID_SEC=110

echo "==> 第 1 步：清理旧进程"
screen -S napcat -X quit 2>/dev/null
pkill -f "opt/QQ/qq" 2>/dev/null
sleep 3

echo "==> 第 2 步：启动 NapCat（后台，日志在 $LOG）"
rm -f "$LOG"
find /root/Napcat/opt/QQ/resources -name qrcode.png -delete 2>/dev/null
nohup xvfb-run -a /root/Napcat/opt/QQ/qq --no-sandbox --disable-gpu --disable-software-rasterizer --disable-dev-shm-usage --js-flags=--max-old-space-size=512 > "$LOG" 2>&1 &

echo "==> 第 3 步：等二维码生成（NapCat 启动约需 20~40 秒）"
GEN_TIME=""
QR_SRC=""
for i in $(seq 1 45); do
    sleep 2
    QR_SRC=$(find /root/Napcat/opt/QQ/resources -name qrcode.png 2>/dev/null | head -1)
    if [ -n "$QR_SRC" ] && [ -f "$QR_SRC" ]; then
        GEN_TIME=$(stat -c %Y "$QR_SRC")
        break
    fi
done
if [ -z "$GEN_TIME" ]; then
    echo "!! 90 秒内没等到二维码。运行 tail -30 $LOG 看看日志"
    exit 1
fi

echo "==> 第 4 步：复制二维码到相册"
cp "$QR_SRC" "$QR_DST" && chmod 644 "$QR_DST"
NOW=$(date +%s)
AGE=$((NOW - GEN_TIME))
echo "   二维码已就位（生成于 ${AGE} 秒前），有效期约 ${QR_VALID_SEC} 秒"

echo ""
echo "############################################################"
echo "#   现在抓紧时间（剩约 $((QR_VALID_SEC - AGE)) 秒）：                        #"
echo "#   打开X10相册 -> DCIM/Camera -> asar_qr.png 全屏显示       #"
echo "#   用另一台手机的QQ扫一扫 -> 确认登录                       #"
echo "############################################################"
echo ""

echo "==> 第 5 步：盯着登录结果"
LAST_GEN="$GEN_TIME"
START=$(date +%s)
while true; do
    sleep 3
    NOW=$(date +%s)

    if grep -aqE "登录成功|扫码登录成功|Bot.*online" "$LOG" 2>/dev/null; then
        echo ""
        echo "############################################"
        echo "#  ✅ 登录成功！阿萨尔在 X10 上活了          #"
        echo "############################################"
        exit 0
    fi

    QR_SRC=$(find /root/Napcat/opt/QQ/resources -name qrcode.png 2>/dev/null | head -1)
    if [ -n "$QR_SRC" ] && [ -f "$QR_SRC" ]; then
        NEW_GEN=$(stat -c %Y "$QR_SRC")
        if [ "$NEW_GEN" != "$LAST_GEN" ]; then
            LAST_GEN="$NEW_GEN"
            cp "$QR_SRC" "$QR_DST" && chmod 644 "$QR_DST"
            echo "[$(date +%H:%M:%S)] 二维码刷新了，已复制新的到相册，请重新扫码"
        fi
    fi

    if [ $((NOW - START)) -gt 600 ]; then
        echo "!! 10 分钟没等到登录成功。重新运行本脚本再试一轮。"
        exit 1
    fi
done
