"""
随机图片

- 色图：二次元插画（Lolicon），仅私聊开放（含 R18）
- 看景：真人摄影图（picsum.photos，风景/静物，全年龄）

这个区分不靠参数自觉，靠 isinstance 判定会话类型。
"""
import asyncio
import os
import random

import aiohttp
from ddgs import DDGS
from nonebot import on_command
from nonebot.params import CommandArg
from nonebot.rule import to_me
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)

TOKEN = os.getenv("LOLICON_TOKEN", "")

# 全年龄备用图源：dmoe.cc 还活着，sevin.cn 已 404
RANDOM_IMG_APIS = [
    "https://www.dmoe.cc/random.php?return=json",
]

def _private_only(event) -> bool:
    # 色图只开私聊：群聊一律不触发，防止封号风险
    return isinstance(event, PrivateMessageEvent)


image_search = on_command(
    "色图",
    aliases={"来点色图", "整点涩图"},
    rule=to_me() & _private_only,
    priority=20,
    block=True,
)


@image_search.handle()
async def _(event: GroupMessageEvent | PrivateMessageEvent, arg: Message = CommandArg()):
    private = isinstance(event, PrivateMessageEvent)

    # 命令后面跟的词当搜索标签：「色图 原神」→ 搜原神；多词用空格隔开
    tags = [t for t in arg.extract_plain_text().split() if t]
    query = " ".join(tags)

    item = await fetch_lolicon(r18=1 if private else 0, tags=tags)

    if item:
        urls, is_r18 = item
        # 群聊里绝不能出 R18：拦掉重取一次，重取到的若还带 R18 标记就一条都不发
        if is_r18 and not private:
            item = await fetch_lolicon(r18=0, tags=tags)
            urls = {}
            if item and not item[1]:
                urls = item[0]
    else:
        urls = {}

    img = await fetch_image_bytes(urls) if urls else None

    # pixiv 图库查无此词 → AI 翻译成英文标签 → 查 Safebooru 二次元图库
    # 群聊：Safebooru 本就是全年龄图库，内容符合群里预期，可用
    # 私聊：用户带着「要色图」的预期，Safebooru 只有全年龄图，宁可不发也不能货不对板
    if img is None and tags and not private:
        booru_tags = await translate_tags(query)
        # 先整串查（多标签取交集）；查不到再逐个标签试（避免一个冷门标签拖垮全部）
        candidates = [booru_tags] if booru_tags else []
        if "," in booru_tags:
            candidates += [t.strip() for t in booru_tags.split(",") if t.strip()]
        for ct in candidates:
            booru_url = await fetch_safebooru(ct)
            if booru_url:
                img = await download_image(booru_url)
                if img:
                    break

    if img is None:
        # 带关键词搜不到就不随机兜底，如实说；不带关键词才走备用图源
        if tags:
            if private and img is None:
                await image_search.finish(
                    f"「{query}」的风月册页，图库里未曾收录。冷门的作品，画师们不轻易落笔——换位热门角色，兴许有货。"
                )
                return
            await image_search.finish(f"「{query}」么，架上没有这一册。换个说法，我再替您找找。")
            return
        alt = await fetch_random_img()
        if alt:
            img = await download_image(alt)
    if img is None:
        await image_search.finish("画送来的路上破损了，今日只得作罢，改日再取。")
        return
    await image_search.finish(MessageSegment.image(img))


# —— 看景：真人摄影图，全年龄 ——
scenery = on_command(
    "看景",
    aliases={"风景", "来点风景", "看风景", "摄影"},
    rule=to_me(),
    priority=20,
    block=True,
)


@scenery.handle()
async def _():
    # picsum：真实摄影图库。用 seed URL：同一链接永远返回同一张图。
    # 不做预检——图能不能拉成功由 QQ 的服务器说了算，机器人这边探一遍只是白等
    seed = random.randint(0, 999999)
    url = f"https://picsum.photos/seed/{seed}/1200/800.jpg"
    await scenery.finish(Message(f"[CQ:image,file={url},cache=0]"))


async def fetch_lolicon(r18: int = 0, tags: list[str] | None = None):
    """返回 ({size: 图片直链}, 是否R18)，失败返回 None。tags 为 pixiv 标签搜索词。"""
    # 请求 regular（约1200px），再按 pixiv 图床规则构造出 small（540px）做竞速
    params = {"num": 1, "r18": r18, "size": "regular"}
    if tags:
        params["tag"] = "|".join(tags)
    if TOKEN:
        params["token"] = TOKEN
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                "https://api.lolicon.app/setu/v2",
                params=params,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                data = await resp.json()
                if data.get("error"):
                    return None
                arr = data.get("data") or []
                if not arr:
                    return None
                d = arr[0]
                urls = d.get("urls") or {}
                url = urls.get("regular") or urls.get("original") or ""
                if not url:
                    return None
                picked = {"regular": url}
                if "i.pixiv.re/" in url:
                    small = url.replace("i.pixiv.re/", "i.pixiv.re/c/540x540_70/")
                    picked["small"] = small
                is_r18 = bool(d.get("r18"))
                # 上游字段偶尔漏填，用标签再兜一次
                if not is_r18:
                    tags = " ".join(d.get("tags") or []).lower()
                    is_r18 = ("r-18" in tags) or ("r18" in tags)
                return (picked, is_r18)
    except Exception:
        return None


async def fetch_image_bytes(urls: dict) -> bytes | None:
    """regular 和 small 同时下载：大图先到就发大图，否则发先到的小图。"""
    tasks = {
        asyncio.create_task(download_image(u)): k for k, u in urls.items() if u
    }
    if not tasks:
        return None
    done, pending = await asyncio.wait(
        tasks, timeout=8, return_when=asyncio.FIRST_COMPLETED
    )
    if not done:
        for t in pending:
            t.cancel()
        return None
    # 有小图先落地的话，最多再给大图 4 秒，到点就用小图
    got_small = None
    for t in done:
        img = t.result()
        if img and tasks[t] == "small":
            got_small = img
        elif img and tasks[t] == "regular":
            for p in pending:
                p.cancel()
            return img
    if got_small and pending:
        try:
            r_img = await asyncio.wait_for(
                asyncio.shield(list(pending)[0]), timeout=4
            )
            if r_img:
                return r_img
        except Exception:
            pass
    return got_small


# ============ Safebooru 扩展图库（AI 翻译标签） ============
# 二次元图库只有 pixiv 一个时，冷门词查不到；Safebooru 标签库更大，
# 但认英文标签。中文关键词先用 AI 翻译成图库标签再去查。

AI_KEY = os.getenv("OPENAI_API_KEY", "")
AI_URL = os.getenv("OPENAI_BASE_URL", "https://api.siliconflow.cn/v1")
AI_MODEL = os.getenv("OPENAI_MODEL", "Qwen/Qwen2.5-7B-Instruct")
_TAG_CACHE: dict[str, str] = {}

_TRANSLATE_PROMPT = (
    "你把中文关键词翻译成 Danbooru/Safebooru 二次元图库的英文标签。"
    "规则：标签全小写、单词间用下划线，多个标签之间用逗号分隔，"
    "角色名用 名字_(作品名) 格式。只输出标签，不要解释。\n"
    "常见对照：原神→genshin_impact；雷电将军→raiden_shogun；"
    "甘雨→ganyu；胡桃→hu_tao；初音未来→hatsune_miku；"
    "芙宁娜→furina；纳西妲→nahida；钟离→zhongli\n"
    "示例：苏丹的游戏 阿萨尔 → sultan's_game, azar_(sultan's_game)"
)


async def translate_tags(query: str) -> str:
    """中文关键词 → 图库英文标签，结果缓存。失败返回空串。"""
    if query in _TAG_CACHE:
        return _TAG_CACHE[query]
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                f"{AI_URL}/chat/completions",
                headers={"Authorization": f"Bearer {AI_KEY}"},
                json={
                    "model": AI_MODEL,
                    "max_tokens": 60,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": _TRANSLATE_PROMPT},
                        {"role": "user", "content": query},
                    ],
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                tags = (
                    data["choices"][0]["message"]["content"].strip().splitlines()[0]
                )
                tags = tags.strip().strip('"').lower()
                if tags:
                    _TAG_CACHE[query] = tags
                return tags
    except Exception:
        return ""


async def fetch_safebooru(tags: str) -> str:
    """查 Safebooru（全年龄二次元图库），随机返回一张图的直链。失败返回空串。"""
    if not tags:
        return ""
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                "https://safebooru.org/index.php",
                params={
                    "page": "dapi",
                    "s": "post",
                    "q": "index",
                    "tags": tags,
                    "limit": 100,
                    "json": 1,
                },
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                arr = await resp.json(content_type=None)
                if not arr:
                    return ""
                import random as _r

                post = _r.choice(arr)
                return post.get("file_url") or ""
    except Exception:
        return ""


async def download_image(url: str) -> bytes | None:
    """把图拉回来：pixiv 图床带 Referer，其余站点带通用 UA，失败自动重试。"""
    headers = {
        "Referer": "https://www.pixiv.net/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
    }
    for _ in range(3):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=12)
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.read()
                    if data and len(data) > 1024:
                        return data
        except Exception:
            pass
        await asyncio.sleep(0.3)
    return None


async def fetch_random_img() -> str:
    api = random.choice(RANDOM_IMG_APIS)
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(api, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                data = await resp.json()
                return (
                    data.get("imgurl")
                    or data.get("img_url")
                    or data.get("image_url")
                    or ""
                )
    except Exception:
        return ""
