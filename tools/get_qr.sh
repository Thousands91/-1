#!/usr/bin/env bash
# get_qr.sh — 一键取阿萨尔登录二维码（坑53固化的工具，2026-11-07）
# 背景：手册遗留事项写了"从 napcat-run.log 提取二维码解码URL用qrcode秒出图片，
#       不必再让用户跑命令"，但 11-07 我仍让用户去 Termux 粘 cp 命令——记住的方案不执行等于没记。
# 用法: bash tools/get_qr.sh   （沙箱直接跑，产出 /workspace/阿萨尔登录二维码.png）
set -euo pipefail

CMD_FILE=$(mktemp)
cat > "$CMD_FILE" << 'EOF'
grep -oE "https://txz.qq.com/p\?k=[^ \"]*" /root/napcat-run.log | tail -1
EOF

URL=$(bash "$(dirname "$0")/remote_task.sh" "$CMD_FILE" 120 2>/dev/null \
      | grep -oE "https://txz.qq.com/p\?k=[^ \"]*" | tail -1) || true
[ -n "${URL:-}" ] || { echo "FAIL: 没取到二维码URL，看手机端 NapCat 是否活着"; exit 1; }

echo "URL: $URL"
python3 - "$URL" << 'PYEOF'
import sys, qrcode
img = qrcode.make(sys.argv[1], box_size=12, border=2)
img.save("/workspace/阿萨尔登录二维码.png")
print("OK: /workspace/阿萨尔登录二维码.png")
PYEOF

# 附：同时推一份进手机相册（双保险，监控脚本也在推）
cat > "$CMD_FILE" << 'EOF'
cp /root/Napcat/opt/QQ/resources/app/app_launcher/napcat/cache/qrcode.png /sdcard/DCIM/Camera/asar_qr.png 2>/dev/null && chmod 644 /sdcard/DCIM/Camera/asar_qr.png && echo 相册已更新
EOF
bash "$(dirname "$0")/remote_task.sh" "$CMD_FILE" 120 2>/dev/null | grep -E "相册已更新" || true
rm -f "$CMD_FILE"
echo "提示: 二维码几分钟过期，过期重跑本脚本即可拿新码"
