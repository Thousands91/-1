#!/bin/bash
# 阿萨尔 NapCat 一键登录助手 —— X10 适配版（opt/QQ -> opt/00）
# 用法: bash azar_login_x10.sh
# 做的事: 清理旧进程 -> 亲自启动 NapCat -> 盯着二维码文件等它刷新 ->
#         复制到相册 -> 提醒扫码 -> 码刷新自动换新 -> 盯到登录成功为止

QR_SRC="/root/Napcat/opt/00/resources/app/app_Launcher/napcat/cache/qrcode.png"
QR_DST="/sdcard/DCIM/Camera/asar_qr.png"
LOG="/root/napcat.log"
QR_VALID_SEC=110

echo "==> 第 1 步：清理旧进程"
screen -S napcat -X quit 2>/dev/null
pkill -f "opt/00/qq" 2>/dev/null
sleep 3

echo "==> 第 2 步：启动 NapCat（后台，日志在 $LOG）"
rm -f "$LOG" "$QR_SRC"
nohup xvfb-run -a /root/Napcat/opt/00/qq --no-sandbox --disable-gpu --disable-software-rasterizer --disable-dev-shm-usage --js-flags=--max-old-space-size=512 > "$LOG" 2>&1 &

echo "==> 第 3 步：等二维码生成（NapCat 启动约需 20~40 秒）"
GEN_TIME=""
for i in $(seq 1 30); do
    sleep 2
    if [ -f "$QR_SRC" ]; then
        GEN_TIME=$(stat -c %Y "$QR_SRC")
        break
    fi
done
if [ -z "$GEN_TIME" ]; then
    echo "!! 60 秒内没等到二维码，NapCat 可能没起来。运行 tail -30 $LOG 看看日志"
    exit 1
fi

echo "==> 第 4 步：复制二维码到相册"
cp "$QR_SRC" "$QR_DST" && chmod 644 "$QR_DST"
NOW=$(date +%s)
AGE=$((NOW - GEN_TIME))
echo "   二维码已就位（生成于 ${AGE} 秒前），有效期约 ${QR_VALID_SEC} 秒"

echo ""
echo "############################################################"
echo "#                                                          #"
echo "#   现在去手机上操作（抓紧时间，剩约 $((QR_VALID_SEC - AGE)) 秒）：      #"
echo "#   1. 打开 QQ（机器人小号登录的那个）                       #"
echo "#   2. 随便开个聊天（推荐文件传输助手）                      #"
echo "#   3. 发图片 -> 选 DCIM/Camera 里的 asar_qr.png            #"
echo "#   4. 点开大图 -> 长按 -> 识别二维码 -> 确认登录            #"
echo "#                                                          #"
echo "############################################################"
echo ""

echo "==> 第 5 步：盯着登录结果（你也可以先去扫码，我在这等着）"
LAST_GEN="$GEN_TIME"
START=$(date +%s)
while true; do
    sleep 3
    NOW=$(date +%s)

    if grep -aqE "登录成功|登录成功!|Bot.*online|扫码登录成功" "$LOG" 2>/dev/null; then
        echo ""
        echo "############################################"
        echo "#  ✅ 登录成功！阿萨尔在 X10 上活了          #"
        echo "############################################"
        exit 0
    fi

    # 二维码被刷新了 -> 旧的作废，复制新码并重新提醒
    if [ -f "$QR_SRC" ]; then
        NEW_GEN=$(stat -c %Y "$QR_SRC")
        if [ "$NEW_GEN" != "$LAST_GEN" ]; then
            LAST_GEN="$NEW_GEN"
            cp "$QR_SRC" "$QR_DST" && chmod 644 "$QR_DST"
            echo "[$(date +%H:%M:%S)] 二维码刷新了，已复制新的到相册，请重新识别（asar_qr.png）"
        fi
    fi

    if [ $((NOW - START)) -gt 600 ]; then
        echo "!! 10 分钟没等到登录成功。重新运行本脚本再试一轮。"
        exit 1
    fi
done
