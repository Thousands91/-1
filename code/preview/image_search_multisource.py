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

    item = None
    if private and not tags:
        # 私聊纯随机：三源轮庄——jitsu(题材杂) / tbib(大库) / lolicon
        _roll = random.random()
        if _roll < 0.34:
            _img = await fetch_jitsu_bytes(r18=True)
            if _img:
                await image_search.finish(MessageSegment.image(_img))
                return
        elif _roll >= 0.67:
            _img = await fetch_tbib_bytes(None)
            if _img:
                await image_search.finish(MessageSegment.image(_img))
                return
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
            if private:
                # Lolicon 搜不到 → tbib 大库再搜一轮（同为色图库，不货不对板）
                # 中文高频词先直译；其余交给 AI 翻译成 booru 英文标签
                tbib_tags = _ZH_TAG_MAP.get(query)
                if tbib_tags is None:
                    bt = await translate_tags(query)
                    if bt:
                        tbib_tags = [t.strip() for t in bt.split(",") if t.strip()] or None
                if tbib_tags:
                    _img = await fetch_tbib_bytes(tbib_tags)
                    if _img:
                        await image_search.finish(MessageSegment.image(_img))
                        return
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


# —— 多图源：tbib.org（Danbooru 系大库，可标签搜索，R18，国内可达） ——
# API 与图床（images/samples/thumbnails）实测全通。sample 采样图约
# 1200px，与 Lolicon regular 同级。tags 用 booru 英文标签体系。
TBIB_API = "https://tbib.org/index.php"
_TBIB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"

# 中文高频词 → booru 标签直译（不走 AI 翻译也能用）
_ZH_TAG_MAP = {
    "男": ["1boy", "solo"], "男性": ["1boy", "solo"], "男人": ["1boy", "solo"],
    "帅哥": ["1boy", "solo"], "女": ["1girl"], "女性": ["1girl"], "美女": ["1girl"],
}


async def _tbib_query_bytes(tag_str: str) -> bytes | None:
    """单个标签组合查 tbib。失败/无图返回 None。"""
    url = (f"{TBIB_API}?page=dapi&s=post&q=index&json=1&limit=25"
           f"&tags={tag_str}&pid={random.randint(0, 3)}")
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                url,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": _TBIB_UA},
            ) as resp:
                if resp.status != 200:
                    return None
                arr = await resp.json(content_type=None)
                if not arr:
                    return None
                random.shuffle(arr)
                for post in arr[:4]:
                    image = post.get("image", "")
                    directory = post.get("directory", "")
                    if not image or not directory:
                        continue
                    # sample(约1200px) 优先，原图兜底——两者实测都可达
                    urls = [f"https://tbib.org/samples/{directory}/sample_{image}",
                            f"https://tbib.org/images/{directory}/{image}"]
                    for u in urls:
                        try:
                            async with sess.get(
                                u, timeout=aiohttp.ClientTimeout(total=25),
                                headers={"User-Agent": _TBIB_UA},
                            ) as r2:
                                if r2.status == 200:
                                    data = await r2.read()
                                    # QQ 发图 10MB 会拖死：超大图直接换下一张
                                    if 2048 < len(data) <= 5 * 1024 * 1024:
                                        return data
                        except Exception:
                            continue
                return None
    except Exception:
        return None


async def fetch_tbib_bytes(tags: list[str] | None = None) -> bytes | None:
    """tbib 搜图。tags: booru 英文标签（空 = 纯随机）。失败返回 None，绝不抛错。

    先按全部标签取交集搜；搜不到时逐个标签单独搜兜底——防止 AI 翻译出
    不存在的复合标签（如 silver_hair_girl）导致整链空手。
    """
    if tags:
        norm = []
        for t in tags:
            t = t.strip().replace(" ", "_")
            if t and t not in norm:
                norm.append(t)
        tags = norm or None
    # 冷门标签总图数可能很少，pid 不能翻太深；小页码取一批候选随机挑。
    # 注意：booru API 的标签分隔符必须用裸 "+"——走 aiohttp params 会被
    # 编码成 %2B 导致永远搜不到，所以这里手动拼 query string。
    if tags:
        _q = "+".join(["rating:explicit"] + tags)
        data = await _tbib_query_bytes(_q)
        if data:
            return data
        # 交集空手 → 逐标签单独搜（保留"色图"属性 rating:explicit）
        for t in tags:
            if t.startswith("rating:"):
                continue
            data = await _tbib_query_bytes(f"rating:explicit+{t}")
            if data:
                return data
        return None
    return await _tbib_query_bytes("rating:explicit")


# —— 多图源：寂寞随机图 API（国内可达，图库杂、题材类型更广） ——
# Lolicon 图库以女性向插画为绝对主体、无性别筛选参数；jitsu 是混合抓取
# 图库，题材构成更多样。仅私聊纯随机抽图时使用，带关键词搜索仍走
# Lolicon（只有它有标签检索）。群聊路径绝不触碰本图源。
JITSU_API = "https://moe.jitsu.top/img/"


async def fetch_jitsu_bytes(r18: bool = False) -> bytes | None:
    """直出图字节。失败返回 None，由调用方回落 Lolicon，绝不抛错。"""
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                JITSU_API,
                params={"r18": "1" if r18 else "false"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                # 太小的"图"多半是错误页，当失败处理
                if len(data) < 1024:
                    return None
                return data
    except Exception:
        return None


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
    "重要——必须使用图库真实存在的标签，禁止自造复合标签：\n"
    "1. 人物属性要拆开：外貌用 silver_hair/blonde/twintails 等独立标签，"
    "人数用 1girl/2girls/1boy/2boys，服饰用 maid/kimono/bikini 等\n"
    "2. 例：银发少女 → silver_hair, 1girl（不是 silver_hair_girl）\n"
    "3. 例：两个女仆 → maid, 2girls（不是 two_maids）\n"
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
