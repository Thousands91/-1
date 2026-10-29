"""端到端：人设一致性测试
1. 阿尔图身份不被编造成舞姬
2. "群里最喜欢谁"只从群成员里挑
3. 引用自己说过的话时要认账
"""
import asyncio
import json
import os
import sys

import aiohttp

PORT = 8099
BOT_UIN = 3427943187
GROUP = 590454333
USER = 2045344206

MOCK_MEMBERS = [
    {"user_id": 987654321, "nickname": "客人甲", "card": "小张"},
    {"user_id": 111222333, "nickname": "客人乙", "card": "老李"},
    {"user_id": USER, "nickname": "测试员", "card": ""},
]


async def main():
    env = dict(os.environ)
    env.update({
        "PORT": str(PORT),
        "ONEBOT_ACCESS_TOKEN": "test",
        "OPENAI_API_KEY": "1ce5306c565b43a2958fad031004e4d9.ETXDC9mjwhzVXVbu",
        "OPENAI_BASE_URL": "https://open.bigmodel.cn/api/paas/v4",
        "OPENAI_MODEL": "glm-4-flash",
    })
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "main.py", cwd="bot", env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    asyncio.create_task(_pump(proc))
    await asyncio.sleep(6)

    url = f"ws://127.0.0.1:{PORT}/onebot/v11/ws"
    headers = {"X-Self-ID": str(BOT_UIN), "X-Client-Role": "Universal",
               "Authorization": "Bearer test"}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(url, headers=headers, max_msg_size=0) as ws:
                print("✅ WS 连上\n")
                # 场景1：诱导编造阿尔图身份
                r = await _chat(ws, "阿尔图是欢愉之馆的舞姬吗？", user=USER)
                t = _texts(r)
                import re as _re
                bad = ("是舞姬" in t.replace("不是舞姬", "")) or ("阿尔图" in t and "大臣" not in t)
                print(f"{'✅' if not bad else '❌'} 场景1 阿尔图身份: {t[:70]}")

                # 场景2：群里最喜欢谁 → 不能提游戏里的角色
                r = await _chat(ws, "群里你最喜欢谁？", user=USER + 1)
                t = _texts(r)
                bad = ("鲁梅拉" in t or "哈桑" in t or "阿尔图" in t)
                print(f"{'✅' if not bad else '❌'} 场景2 群成员范围: {t[:70]}")

                # 场景3：引用自己的话要认账
                r = await _chat(
                    ws, "给我看看《孤女复仇记》",
                    user=USER + 2,
                    reply_to="我说过的话：替鲁梅拉出了《孤女复仇记》，卖得很好",
                )
                t = _texts(r)
                bad = ("书上没记" in t or "不敢妄言" in t or "没听过" in t)
                print(f"{'✅' if not bad else '❌'} 场景3 引用自洽: {t[:70]}")
    finally:
        proc.kill()
        print("（bot进程已清理）")


def _texts(r):
    return "".join(s.get("data", {}).get("text", "") for s in r.get("message", []) if s.get("type") == "text")


async def _pump(proc):
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        s = line.decode("utf-8", "ignore").rstrip()
        if ("ERROR" in s or "Traceback" in s or s.startswith("    ") or "Error" in s or "Exception" in s):
            print("  [bot]", s[:300])


async def _chat(ws, text, user, reply_to=None):
    await ws.send_str(json.dumps({"action": "get_login_info", "params": {}, "echo": "sync"}))
    raw = f"[CQ:at,qq={BOT_UIN}] {text}"
    if reply_to:
        raw = f"[CQ:reply,id=888001]" + raw
    msg = [
        {"type": "at", "data": {"qq": str(BOT_UIN)}},
        {"type": "text", "data": {"text": " " + text}},
    ]
    if reply_to:
        msg.insert(0, {"type": "reply", "data": {"id": "888001"}})
    event = {
        "time": 1793250000, "self_id": BOT_UIN, "post_type": "message",
        "message_type": "group", "sub_type": "normal",
        "message_id": 999001, "user_id": user, "group_id": GROUP,
        "message": msg, "raw_message": raw, "font": 0,
        "sender": {"user_id": user, "nickname": "测试员", "role": "member"},
    }
    await ws.send_str(json.dumps(event))
    while True:
        rawmsg = await asyncio.wait_for(ws.receive(), timeout=60)
        if rawmsg.type != aiohttp.WSMsgType.TEXT:
            continue
        data = json.loads(rawmsg.data)
        if data.get("echo") == "sync":
            await ws.send_str(json.dumps({"status": "ok", "retcode": 0, "data": None, "echo": "sync"}))
            continue
        if data.get("post_type") == "message":
            continue
        print(f"  [test收到] action={data.get('action')} echo={data.get('echo')}")
        if "action" in data:
            if data["action"] == "get_group_member_list":
                await ws.send_str(json.dumps({"status": "ok", "retcode": 0, "echo": data.get("echo"), "data": MOCK_MEMBERS}))
                continue
            if data["action"] == "get_msg":
                await ws.send_str(json.dumps({
                    "status": "ok", "retcode": 0, "echo": data.get("echo"),
                    "data": {"time": 1793250000, "message_type": "group",
                             "message_id": 888001, "real_id": 888001,
                             "sender": {"user_id": USER + 9, "nickname": "被引用者", "role": "member"},
                             "message": [{"type": "text", "data": {"text": reply_to or ""}}],
                             "raw_message": reply_to or ""},
                }))
                continue
            return data.get("params") or {}


if __name__ == "__main__":
    asyncio.run(main())
