#!/usr/bin/env bash
# gh_push.sh — GitHub API 文件推送固化工具（坑：任务号错/PUT不回读/覆盖不备份）
# 用法: gh_push.sh <本地文件> <仓库路径> [提交信息]
# 流程强制: 备份线上原文 -> GET sha -> PUT -> 回读 -> md5 比对，任何一步失败即退出非0
set -euo pipefail

LOCAL="$1"; REPO_PATH="$2"; MSG="${3:-update $REPO_PATH}"
TOKEN="$(cat /workspace/.gh_token)"
REPO="Thousands91/-1"
API="https://api.github.com/repos/$REPO/contents"
ENC_PATH="$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$REPO_PATH")"

[ -f "$LOCAL" ] || { echo "FAIL: 本地文件不存在 $LOCAL"; exit 1; }

# 1. 备份线上原文（新文件则跳过）
HTTP_CODE=$(curl -s -o /tmp/ghpush_remote.json -w "%{http_code}" --max-time 30 -H "Authorization: token $TOKEN" "$API/$ENC_PATH")
if [ "$HTTP_CODE" = "200" ]; then
  python3 -c "
import json,base64
d=json.load(open('/tmp/ghpush_remote.json'))
raw=base64.b64decode(d['content'])
open('/tmp/ghpush_remote.md','wb').write(raw)
print('备份原文', len(raw), '字节 -> /tmp/ghpush_remote.md')"
elif [ "$HTTP_CODE" = "404" ]; then
  echo "[1/4] 新文件，无需备份"
else
  echo "FAIL: GET 现状失败 http=$HTTP_CODE，中止"; exit 1
fi

# 2. GET sha
SHA=$(curl -s --max-time 30 -H "Authorization: token $TOKEN" "$API/$ENC_PATH" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d.get('sha',''))")
echo "[2/4] sha: ${SHA:-(新文件)}"

# 3. PUT
python3 - "$SHA" "$MSG" "$LOCAL" <<'PYEOF'
import base64, json, sys
body = {"message": sys.argv[2], "content": base64.b64encode(open(sys.argv[3],'rb').read()).decode(), "branch":"main"}
if sys.argv[1]:
    body["sha"] = sys.argv[1]
open("/tmp/ghpush_body.json","w").write(json.dumps(body))
PYEOF
PUT_RESULT=$(curl -s --max-time 60 -X PUT -H "Authorization: token $TOKEN" -H "Content-Type: application/json" -d @/tmp/ghpush_body.json "$API/$ENC_PATH")
echo "$PUT_RESULT" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('[3/4] PUT:', d['commit']['sha'][:8] if 'commit' in d else 'FAIL '+str(d))" 
echo "$PUT_RESULT" | python3 -c "
import json,sys
d=json.load(sys.stdin)
sys.exit(0 if 'commit' in d else 1)" || exit 1

# 4. 回读 + md5 比对
curl -s --max-time 30 -H "Authorization: token $TOKEN" "$API/$ENC_PATH" | python3 -c "
import json,sys,base64,hashlib
d=json.load(sys.stdin)
remote=base64.b64decode(d['content'])
open('/tmp/ghpush_readback','wb').write(remote)
print('[4/4] 回读', len(remote), '字节')"
LOCAL_MD5=$(md5sum "$LOCAL" | cut -d' ' -f1)
REMOTE_MD5=$(md5sum /tmp/ghpush_readback | cut -d' ' -f1)
if [ "$LOCAL_MD5" = "$REMOTE_MD5" ]; then
  echo "OK: md5 一致 ($LOCAL_MD5)，推送闭环"
else
  echo "FAIL: md5 不一致! 本地=$LOCAL_MD5 远程=$REMOTE_MD5 线上原文备份在 /tmp/ghpush_remote.md"; exit 1
fi