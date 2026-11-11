#!/bin/bash
# 阿萨尔X10容器内部署（在 proot Ubuntu 内以 root 执行，由 deploy_x10_termux.sh 调起）
# 目录布局与现手机完全一致：/root/bot /root/asar-sync /root/Napcat
set -e
GTOK="$(printf 'Z2hwX2Nab3N0VEo3QnBlWVU3VEM=' | base64 -d)$(printf 'QXZtRGNBT2U3TVJBUUozT1pmT0k=' | base64 -d)"
RAW="https://raw.githubusercontent.com/Thousands91/-1/main"

echo "===== [容器 1/7] 系统依赖 ====="
# 坑81姊妹坑：ubuntu-base 的默认源是 ports.ubuntu.com，国内龟速。
# 换清华 ubuntu-ports 镜像（arm64 官方镜像），速度提升数倍。
sed -i 's|http://ports.ubuntu.com/ubuntu-ports|https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports|g; s|http://ports.ubuntu.com/|https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports/|g' /etc/apt/sources.list 2>/dev/null || true
apt-get update -y >/dev/null 2>&1 || true
apt-get install -y git python3 python3-venv python3-pip curl xvfb \
  fonts-wqy-zenhei locales tzdata ca-certificates >/dev/null 2>&1
ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime 2>/dev/null || true

echo "===== [容器 2/7] 凭据（remote_ctl 靠它取 token）====="
git config --global credential.helper store
echo "https://x-access-token:${GTOK}@github.com" > /root/.git-credentials

echo "===== [容器 3/7] bot 代码（布局对齐现手机 /root/bot）====="
cd /root
[ -d bot/.git ] || git clone --depth 1 "https://x-access-token:${GTOK}@github.com/Thousands91/qq-bot.git" bot

echo "===== [容器 4/7] venv + 依赖（清华源防超时）====="
cd /root/bot
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple
./venv/bin/pip install -q -r bot/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

echo "===== [容器 5/7] .env 与记忆 ====="
if curl -sfL -H "Authorization: token ${GTOK}" "${RAW}/x10/env.env" -o /root/bot/bot/.env; then
  echo "真机 .env 已继承"
else
  echo "警告: env.env 尚未上传，写入兜底模板（主模型配置为实抓值）"
  cat > /root/bot/bot/.env <<'ENVEOF'
COMMAND_START=[""]
OPENAI_API_KEY=1ce5306c565b43a2958fad031004e4d9.ETXDC9mjwhzVXVbu
OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4
OPENAI_MODEL=glm-4.7-flash
ENABLE_AI_CHAT=true
HOST=0.0.0.0
PORT=8080
SILICONFLOW_API_KEY=sk-veyzzthgrrlhuircmfzcnkpqwpxppchezxhqfjaggmqdummg
ENVEOF
fi
mkdir -p /root/bot/bot/data
curl -sfL -H "Authorization: token ${GTOK}" "${RAW}/x10/asar_memory.tar.gz" -o /tmp/asar_memory.tar.gz \
  && tar xzf /tmp/asar_memory.tar.gz -C /root/bot/bot/data/ && echo "记忆已恢复" \
  || echo "警告: 记忆包下载失败"

echo "===== [容器 6/7] 遥控 + 看护 + NapCat ====="
# 优先继承现手机的战斗脚本包（remote_ctl/logincheck/netmon/offline_watch/asar_sync）
if curl -sfL -H "Authorization: token ${GTOK}" "${RAW}/x10/phone_scripts.tar.gz" -o /tmp/phone_scripts.tar.gz; then
  tar xzf /tmp/phone_scripts.tar.gz -C /root/ && echo "真机脚本已继承: $(ls /root/*.sh | wc -l) 个"
  chmod +x /root/*.sh 2>/dev/null || true
else
  echo "警告: phone_scripts 尚未上传，从仓库下载 remote_ctl 兜底"
  curl -sfL -H "Authorization: token ${GTOK}" "${RAW}/remote/remote_ctl.sh" -o /root/remote_ctl.sh
  curl -sfL -H "Authorization: token ${GTOK}" "${RAW}/asar_sync.sh" -o /root/asar_sync.sh
  chmod +x /root/*.sh
fi
[ -d asar-sync/.git ] || git clone --depth 1 "https://x-access-token:${GTOK}@github.com/Thousands91/-1.git" /root/asar-sync
if [ ! -d /root/Napcat/opt/QQ ]; then
  cd /root
  curl -fL --connect-timeout 20 --max-time 1800 -o napcat.sh \
    https://raw.githubusercontent.com/NapNeko/NapCat-Installer/main/script/install.sh
  bash napcat.sh --docker n --cli y --github-proxy 0 || bash napcat.sh --docker n --cli n --github-proxy 0
fi

echo "===== [容器 7/7] 自检 ====="
echo "bot代码: $(ls -d /root/bot/bot 2>/dev/null || echo 缺失)"
echo "venv:    $(ls -d /root/bot/venv 2>/dev/null || echo 缺失)"
echo ".env键:  $(grep -c '=' /root/bot/bot/.env 2>/dev/null || echo 0)"
echo "记忆:    $(ls -d /root/bot/bot/data/memory 2>/dev/null || echo 缺失)"
echo "NapCat:  $(ls -d /root/Napcat/opt/QQ 2>/dev/null || echo 缺失)"
echo "遥控:    $(ls /root/remote_ctl.sh 2>/dev/null || echo 缺失)"
echo "看护件:  $(ls /root/logincheck.sh /root/netmon.sh /root/offline_watch.sh 2>/dev/null | wc -l)/3"
echo "asar-sync: $(ls -d /root/asar-sync/.git 2>/dev/null || echo 缺失)"
echo "===== 容器内部署完成，回到 Termux 侧继续 ====="
