#!/data/data/com.termux/files/usr/bin/bash
# 阿萨尔荣耀X10一键部署（在旧手机 Termux 里执行）
# 用法: curl -sL -o d.sh <本脚本raw地址> && bash d.sh
# 全程约 15-25 分钟，中途不要锁屏太久
set -e
GTOK="$(printf 'Z2hwX2Nab3N0VEo3QnBlWVU3VEM=' | base64 -d)$(printf 'QXZtRGNBT2U3TVJBUUozT1pmT0k=' | base64 -d)"
RAW="https://raw.githubusercontent.com/Thousands91/-1/main/x10"

echo "===== [1/6] Termux 基础包 ====="
pkg update -y >/dev/null 2>&1 || true
pkg install -y proot-distro git curl >/dev/null 2>&1
termux-setup-storage >/dev/null 2>&1 || true

echo "===== [2/6] 安装 Ubuntu 容器 ====="
# 坑81：新版 proot-distro 从 docker registry 拉 Ubuntu，国内网络直连
# 不通（Errno 101）。改走清华镜像下载官方 rootfs tarball 再本地安装，
# 阿里镜像兜底。login 实测可用才算装好，别的都不可信。
if proot-distro login ubuntu -- true >/dev/null 2>&1; then
  echo "容器已存在且可用，跳过"
else
  rm -rf "$PREFIX/var/lib/proot-distro/installed-rootfs/ubuntu"
  echo "从清华镜像下载 Ubuntu rootfs（约30MB）..."
  curl -fL --retry 3 -o ~/ubuntu-rootfs.tar.gz \
    "https://mirrors.tuna.tsinghua.edu.cn/ubuntu-cdimage/ubuntu-base/releases/22.04/release/ubuntu-base-22.04.5-base-arm64.tar.gz" \
  || curl -fL --retry 3 -o ~/ubuntu-rootfs.tar.gz \
    "https://mirrors.aliyun.com/ubuntu-cdimage/ubuntu-base/releases/22.04/release/ubuntu-base-22.04.5-base-arm64.tar.gz" \
  || { echo "!! 两个镜像都下载失败——截图发我"; exit 1; }
  proot-distro install ~/ubuntu-rootfs.tar.gz --name ubuntu \
    || { echo "!! 本地安装失败——截图发我"; exit 1; }
  rm -f ~/ubuntu-rootfs.tar.gz
  echo "容器装好，login 自检..."
  proot-distro login ubuntu -- true || { echo "!! 容器装了但进不去——截图发我"; exit 1; }
fi

echo "===== [3/6] 容器内部署（约10-20分钟）====="
proot-distro login ubuntu -- bash -c "
apt-get update -y >/dev/null 2>&1
apt-get install -y curl >/dev/null 2>&1
curl -sfL -H 'Authorization: token ${GTOK}' '${RAW}/setup_ubuntu.sh' -o /root/setup.sh && bash /root/setup.sh
"

echo "===== [4/6] 下载启动器到 Termux ====="
curl -sfL -H "Authorization: token ${GTOK}" "${RAW}/run_asar.sh" -o ~/x10_run_asar.sh
chmod +x ~/x10_run_asar.sh

echo "===== [5/6] 部署完成清单 ====="
proot-distro login ubuntu -- bash -c '
echo "bot代码: $(ls -d /root/bot/bot 2>/dev/null || echo 缺失)"
echo "venv:    $(ls -d /root/bot/venv 2>/dev/null || echo 缺失)"
echo ".env:    $(ls /root/bot/bot/.env 2>/dev/null || echo 缺失)"
echo "记忆:    $(ls -d /root/bot/bot/data/memory 2>/dev/null || echo 缺失)"
echo "NapCat:  $(ls -d /root/Napcat/opt/QQ 2>/dev/null || echo 缺失)"
echo "遥控:    $(ls /root/remote_ctl.sh 2>/dev/null || echo 缺失)"
echo "看护件:  $(ls /root/logincheck.sh /root/netmon.sh /root/offline_watch.sh 2>/dev/null | wc -l)/3"
'

echo "===== [6/6] 下一步 ====="
echo "① 先做荣耀防杀设置（见迁移清单）"
echo "② 启动: bash ~/x10_run_asar.sh  然后按 Home 键切走（别退出Termux会话）"
echo "③ 等2分钟出码后，扫码命令见迁移清单"
