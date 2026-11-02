"""
识图 —— 阿萨尔替你瞧一眼画
=============================

触发条件
--------
- 私聊：消息里带图片（说不说文字都行）
- 群聊：@阿萨尔 且消息里带图片
- 纯图片无文字时也响应；priority=60，早于 ai_chat 的兜底，没图就放行

实现
----
- 图片下载转 base64（不把 QQ 图床时效 URL 甩给视觉 API，自己下载更可控）
- 视觉模型 glm-4.6v-flash（智谱免费档），一次调用直接用阿萨尔口吻描述，
  不做"先识图再转述"的两段调用
- 免费档限流（错误码1305）较常见：重试 2 次，仍失败就以人设口吻坦白没瞧明白
- 5MB 上限，防超大图拖死
"""
import asyncio
import base64
import json
import os

import aiohttp
from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, PrivateMessageEvent
from nonebot.exception import MatcherException
from nonebot.rule import to_me

API_KEY = os.getenv("OPENAI_API_KEY", "")
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/")
# 硅基流动没有 glm 免费视觉，视觉模型默认固定智谱端点；若 BASE_URL 已是智谱则复用
VLM_URL = (
    BASE_URL + "/chat/completions"
    if "bigmodel" in BASE_URL
    else "https://open.bigmodel.cn/api/paas/v4/chat/completions"
)
VLM_KEY = API_KEY if "bigmodel" in BASE_URL else os.getenv("ZHIPU_API_KEY", API_KEY)
VLM_MODEL = os.getenv("VISION_MODEL", "glm-4.6v-flash")

MAX_IMG_BYTES = 5 * 1024 * 1024  # 5MB
RETRY_TIMES = 2                  # 429 重试次数
TIMEOUT = aiohttp.ClientTimeout(total=30)

# 阿萨尔口吻的识图提示（人设简版，独立于 ai_chat 的完整人设）
SEE_SYSTEM = (
    "你是阿萨尔，《苏丹的游戏》里「垂钓者」书店的老板。有人通过一种远程的传影法术给你看一幅画或一张图。"
    "你只写亲眼见过的事，说话克制、带点自嘲的清醒，像个见多识广的书店老板随口点评。"
    "用两三句话说说你从画里瞧见了什么、画得如何；看不清楚或说不上来是什么，就老实说没瞧明白，别瞎猜。"
    "不要自称AI或助手，不要列要点，就像当面聊一句。"
)


def _has_image(event: GroupMessageEvent | PrivateMessageEvent) -> bool:
    return any(seg.type == "image" for seg in event.message)


see = on_message(to_me() & _has_image, priority=60, block=True)


async def _download_image(seg) -> bytes | None:
    """从 Image 段取 URL 下载，超 5MB 或失败返回 None。"""
    url = seg.data.get("url") or ""
    if not url and str(seg.data.get("file", "")).startswith("http"):
        url = seg.data["file"]
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, timeout=TIMEOUT,
                                headers={"User-Agent": "Mozilla/5.0"}) as r:
                if r.status != 200:
                    return None
                data = await r.read()
        if len(data) > MAX_IMG_BYTES or len(data) < 100:
            return None
        return data
    except Exception as e:
        logger.warning(f"[识图] 下载图片失败: {type(e).__name__} {e}")
        return None


async def _vlm_see(img_b64: str, user_text: str) -> str:
    """调视觉模型，返回阿萨尔口吻的描述。限流重试，最终失败抛异常。"""
    content: list = [{"type": "image_url",
                      "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}]
    text = (user_text or "").strip()
    content.append({"type": "text",
                    "text": text or "说说你从这张图里瞧见了什么"})
    payload = {
        "model": VLM_MODEL,
        "max_tokens": 300,
        "temperature": 0.7,
        "thinking": {"type": "disabled"},  # glm-4.6v-flash 默认思考会吃空正文
        "messages": [
            {"role": "system", "content": SEE_SYSTEM},
            {"role": "user", "content": content},
        ],
    }
    last_err = ""
    for attempt in range(1 + RETRY_TIMES):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    VLM_URL,
                    headers={"Authorization": f"Bearer {VLM_KEY}"},
                    json=payload,
                    timeout=TIMEOUT,
                ) as resp:
                    data = await resp.json()
            if data.get("error"):
                last_err = str(data["error"].get("message", ""))[:80]
            else:
                text_out = (data["choices"][0]["message"].get("content") or "").strip()
                if text_out:
                    return text_out
                last_err = "空正文"
        except Exception as e:
            last_err = f"{type(e).__name__} {str(e)[:60]}"
        await asyncio.sleep(4)
    raise RuntimeError(f"视觉模型失败: {last_err}")


@see.handle()
async def _(event: GroupMessageEvent | PrivateMessageEvent):
    if not VLM_KEY:
        return  # 没配 key 就静默放行，别打扰
    seg = next(s for s in event.message if s.type == "image")
    text = event.get_plaintext()
    # 用户故意只发命令关键词(如"色图"带图回复流程)时,别抢答——交给色图逻辑
    if text.strip() in ("色图", "一张色图", "来张色图"):
        see.skip()
    img = await _download_image(seg)
    if img is None:
        await see.finish("这画我怎么都取不下来，怕是传影的法术出了岔子。")
        return
    img_b64 = base64.b64encode(img).decode()
    try:
        answer = await _vlm_see(img_b64, text)
    except RuntimeError as e:
        logger.warning(f"[识图] {e}")
        await see.finish("这画我瞧了半天也没瞧明白，眼花了，改日再看。")
        return
    except MatcherException:
        raise
    except Exception as e:
        logger.warning(f"[识图] 意外: {type(e).__name__} {e}")
        await see.finish("这画我瞧了半天也没瞧明白，眼花了，改日再看。")
        return
    await see.finish(answer)
