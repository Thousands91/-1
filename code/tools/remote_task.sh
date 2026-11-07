#!/usr/bin/env bash
# remote_task.sh — 遥控任务下发固化工具（坑44：任务号格式错；坑49：输出不完整没查实例）
# 用法: remote_task.sh <命令文件（内容=要下发的命令，不含任务号）> [--wait 秒]
# 流程强制: 自动取号+校验纯数字 -> PUT cmd.txt -> 回读验证 -> 轮询 out.txt 直到出现本任务回传
set -euo pipefail

CMD_FILE="$1"; WAIT="${2:-180}"
TOKEN="$(cat /workspace/.gh_token)"
REPO="Thousands91/-1"
API="https://api.github.com/repos/$REPO/contents"
STATE="/workspace/qq-bot-project/tools/.last_task_num"

[ -f "$CMD_FILE" ] || { echo "FAIL: 命令文件不存在"; exit 1; }

# 1. 自动取号：读上次号 +1，强制纯数字校验
LAST=$(cat "$STATE" 2>/dev/null || echo 0)
NEXT=$((LAST + 1))
case "$NEXT" in
  ''|*[!0-9]*) echo "FAIL: 任务号非纯数字 ($NEXT)，中止"; exit 1;;
esac
echo "[1/4] 任务号: $NEXT（上次 $LAST）"

# 2. 组装 cmd.txt（第一行任务号 + 命令）
{ echo "$NEXT"; cat "$CMD_FILE"; } > /tmp/remote_cmd.txt

# 3. PUT + 回读验证
SHA=$(curl -s --max-time 30 -H "Authorization: token $TOKEN" "$API/remote/cmd.txt" | python3 -c "
import json,sys
print(json.load(sys.stdin).get('sha',''))")
python3 - "$SHA" <<'PYEOF'
import base64, json, sys
body = {"message": "task cmd", "content": base64.b64encode(open("/tmp/remote_cmd.txt","rb").read()).decode()}
if sys.argv[1]:
    body["sha"] = sys.argv[1]
open("/tmp/remote_cmd_body.json","w").write(json.dumps(body))
PYEOF
curl -s --max-time 60 -X PUT -H "Authorization: token $TOKEN" -H "Content-Type: application/json" -d @/tmp/remote_cmd_body.json "$API/remote/cmd.txt" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('[2/4] PUT:', d['commit']['sha'][:8] if 'commit' in d else 'FAIL '+str(d))
sys.exit(0 if 'commit' in d else 1)"

sleep 5
READ_OK=0
for i in 1 2 3; do
  curl -s --max-time 30 -H "Authorization: token $TOKEN" "$API/remote/cmd.txt" -o /tmp/remote_readback.json && [ -s /tmp/remote_readback.json ] || { echo "[3/4] 回读第${i}次网络空响应，重试..."; sleep 8; continue; }
  if python3 - "$NEXT" <<'PYEOF'
import json,sys,base64
expected = sys.argv[1]
c=base64.b64decode(json.load(open('/tmp/remote_readback.json'))['content']).decode()
first=c.split(chr(10))[0].strip()
print('[3/4] 回读首行:', first)
sys.exit(0 if first == expected else 1)
PYEOF
  then READ_OK=1; break; else echo "[3/4] 回读任务号不符，第${i}次重试..."; sleep 8; fi
done
[ "$READ_OK" = "1" ] || { echo "FAIL: 三次回读均未确认任务号 $NEXT"; exit 1; }

# 4. 更新本地号状态并轮询 out.txt
echo "$NEXT" > "$STATE"
echo "[4/4] 轮询 out.txt 等待 task_$NEXT 回传（最长 ${WAIT}s，走API防缓存）..."
DEADLINE=$(( $(date +%s) + WAIT ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  # 走 contents API + 随机参数（raw.githubusercontent CDN 会无视查询参数缓存旧内容数分钟，11-07 实测踩坑）
  curl -s --max-time 30 -H "Authorization: token $TOKEN" -H "Accept: application/vnd.github.raw" "$API/remote/out.txt?v=$(date +%s)$RANDOM" -o /tmp/remote_out.txt || { sleep 15; continue; }
  if grep -q "task_$NEXT.txt" /tmp/remote_out.txt; then
    echo "=== task_$NEXT 回传 ==="
    cat /tmp/remote_out.txt
    exit 0
  fi
  sleep 15
done
echo "TIMEOUT: ${WAIT}s 内未见 task_$NEXT 回传（可稍后手动重查，或手机侧循环未运行）"
exit 2