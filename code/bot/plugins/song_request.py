"""
点歌

群里发 /点歌 晴天  → 机器人找到歌，发语音 / 文件 / 分享卡片

音源说明
---------
网易云的 free 外链早在 2021 年就关了，官方 openapi 又要会员。
这里走第三方聚合接口拿音频流，属于"能响就行"的方案：
- 单曲可能只返回 45 秒试听片段（版权方限制），也可能给整首
- 接口本身是公益性质的，随时可能失效

发送策略（2026-11-11 v3，见手册坑80）：
1. 默认"语音 + QQ音乐卡片"双发：
   - 语音条：群里即点即听，0 秒操作
   - QQ音乐卡片：离开聊天界面后继续听的正解——点卡片跳 QQ 音乐
     App 播放，切界面/锁屏都不断（语音条和 QQ 内置网页都做不到后台，
     物理上只有"QQ音乐App"和"本地文件"两个后台出口，见坑80）
2. 卡片上的歌是 QQ 音乐版权库的：payplay=1 的要 VIP，点进去只能
   试听——如实备注，不装作没看见
3. 语音失败 → 自动改传群文件；歌超过 600s 或用户加了"文件"后缀 →
   直接传文件
4. 长度红线从 300s 放宽到 600s：voice_probe.log 里 7 次成功都在
   300s 以内纯属"代码没敢试"，11.7MB 都发出去过——先试再说，
   probe 会记下真实上限在哪
"""
import re
from datetime import datetime
from pathlib import Path

import aiohttp
from nonebot import on_command
from nonebot.rule import to_me
from nonebot.exception import FinishedException
from nonebot.adapters.onebot.v11 import (
    Bot, GroupMessageEvent, PrivateMessageEvent, Message, MessageSegment,
)
from nonebot.params import CommandArg

song_cmd = on_command("点歌", aliases={"听歌", "来首歌"}, rule=to_me(), priority=20, block=True)

SEARCH = "https://music.163.com/api/search/get"
# 第三方取流：给了 id 就返回 mp3 二进制
STREAM = "https://api.injahow.cn/meting/"
# QQ 音乐搜索（公开接口，无需 key）：拿 songmid 发官方分享卡片用。
# 2026-11-11 实测可用，返回里有 songmid / pay.payplay（1=要VIP）
QQ_SEARCH = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 超过这个大小就不下载了，QQ 传大文件容易超时
MAX_BYTES = 16 * 1024 * 1024
# 搜索回来的候选，最多往下试几个
TRY_N = 6
# 语音时长上限。旧值 300s 是拍脑袋保险，probe 日志里从来没有 >300s 的
# 样本（代码直接拒发），11.7MB 成功过 → 放宽到 600s，先试语音，
# 失败自动降级传文件，probe 继续记，真实上限让数据说话。见坑80。
MAX_VOICE_SEC = 600
# 公开群聊里按这两个开关过滤：不含 VIP 曲目（免费也拿不到音频）、剔除现场/伴奏版
FILTER_VIP = True
BAD_WORDS = ("伴奏", "纯音乐", "ktv", "instrumental", "inst.", "remix", "dj", "慢摇")
# 文件缓存目录（容器内绝对路径，NapCat 同容器可直接读）
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "music_cache"


@song_cmd.handle()
async def _(bot: Bot, event: GroupMessageEvent | PrivateMessageEvent, arg: Message = CommandArg()):
    # 命令前缀可能是空串（裸词"点歌"就能触发），这时 get_plaintext() 里
    # 没有"/点歌"可替换，整句会被当成歌名——所以必须走 CommandArg
    kw = arg.extract_plain_text().strip()

    # "点歌 晴天 文件" → 不发语音，直接传文件（后台随便听，见坑80）
    force_file = bool(re.search(r"\s*(文件|file)$", kw, re.I))
    kw = re.sub(r"\s*(文件|file)$", "", kw, flags=re.I).strip()

    if not kw:
        await song_cmd.finish("想听什么？说个歌名、歌手名都行；后面加个'文件'就发文件版（能后台听）。")

    await song_cmd.send(f"翻柜子找「{kw}」，稍等一下……")

    songs = await search(kw)

    # 原曲是 VIP 发不出声时，宁可说实话也不拿翻唱充数。
    # 判定：关键词里带了歌手名，搜到了该歌手的同名原曲，但它只有 VIP 版。
    kws = re.split(r"[\s,，、/]+", kw.strip())
    title = _norm(kws[0]) if kws else ""
    artist_words = [_norm(x) for x in kws[1:]]
    if title and artist_words:
        vip_original = None
        free_original = False
        for s in songs:
            if title in _norm(s["name"]) and any(a in _norm(s["artists"]) for a in artist_words):
                if s.get("fee") == 1:
                    vip_original = s
                else:
                    free_original = True
        if vip_original and not free_original:
            s = vip_original
            await song_cmd.finish(
                Message(f"{build_card(s)}\n\n这首是「{s['artists']}」的原曲，但它是 VIP 曲目，"
                        f"柜上拿不来整首的音频，翻唱的又不肯糊弄您——点上面的链接去听吧。")
            )

    songs = [s for s in songs if usable(s, kw)]
    if not songs:
        await song_cmd.finish(f"「{kw}」这几个字，我在曲子和歌手里都翻不着。换个说法试试？")

    # 用户写了歌手名，就把这位歌手的版本排到前头
    songs = rank_by_artist(songs, kw)

    # 同名歌很多，按顺序试，取第一个真能拿到音频的
    card = songs[0]
    for s in songs[:TRY_N]:
        audio, sec, err = await fetch_audio(s["id"])
        if not audio:
            continue
        card = s

        # ---- 文件模式：显式要求，或歌太长不赌语音 ----
        if force_file or (sec and sec > MAX_VOICE_SEC):
            note = (f"这首 {sec} 秒，直接给你传文件——下载后用什么播放器都行，"
                    f"切出聊天界面照样放，还能拖进度条。")
            if await send_file(bot, event, s, audio):
                await song_cmd.finish(Message(f"{build_card(s)}\n\n{note}"))
            await song_cmd.finish(
                Message(f"{build_card(s)}\n\n这首 {sec} 秒，文件没传上去。点上面的链接听吧。")
            )

        # ---- 语音模式：先附 QQ 音乐卡片（后台听的正解），再发语音条 ----
        card_note = ""
        mid, vip = await qq_card_mid(kw)
        if mid:
            try:
                await song_cmd.send(MessageSegment.music("qq", mid))
                card_note = (
                    "上面这张卡片是 QQ 音乐的同一首歌——离开聊天界面还想继续听就点它，"
                    "用 QQ 音乐放，切界面、锁屏都不会停。"
                    if not vip else
                    "上面这张卡片是 QQ 音乐的同一首歌，点它也能后台听；"
                    "不过这首在 QQ 音乐要 VIP，点进去可能只能试听几十秒，听整首还是趁语音条。"
                )
            except Exception:
                # 卡片发不出去不影响主线，静默跳过
                card_note = ""

        try:
            # 长音频发过去要等 NapCat 转 silk 再上传，默认 30 秒的 api_timeout
            # 根本不够，按体积给个宽裕的余量。
            _t = 60 + len(audio) // (200 * 1024)
            await song_cmd.send(MessageSegment.record(audio), _timeout=_t)
            probe(sec, len(audio), "ok", "")
            if card_note:
                await song_cmd.send(card_note)
            return
        except FinishedException:
            # finish() 是靠抛这个异常结束的，别当成"发送失败"记进日志
            raise
        except BaseException as e:
            # BaseException 不是手滑：音频往 WS 里塞是同步的，长音频能卡住
            # 整个事件循环，卡太久会被 CancelledError 打断。这种也得兜住，
            # 否则异常漏出去就是一个红色堆栈，用户那边什么也收不到。
            probe(sec, len(audio), "fail", f"{type(e).__name__}|{_t if '_t' in dir() else '?'}")
            note = f"这首 {sec} 秒，语音条递不过去"
            if card_note:
                note += "；卡片照发，去 QQ 音乐点着听也行"
            if await send_file(bot, event, s, audio):
                await song_cmd.finish(Message(f"{build_card(s)}\n\n{note}，改成传文件了——下载后切界面也能听。"))
            await song_cmd.finish(
                Message(f"{build_card(s)}\n\n{note}，文件也没传上去。点上面的链接听吧。")
            )

    # 全都拿不到音频，退回卡片
    await song_cmd.finish(
        Message(f"{build_card(card)}\n\n曲子我调不出来，只能给您递个名目。")
    )


LOG = Path(__file__).resolve().parent.parent / "data" / "voice_probe.log"


def probe(sec: int, size: int, result: str, err: str) -> None:
    """记一条：这次发了多少秒，成没成。

    QQ 语音条到底能有多长，官方没说，各家说法不一。与其猜，不如让它自己
    记：跑一段时间翻日志，就能看出失败是从第几秒开始出现的。
    """
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{sec}s\t"
                    f"{size // 1024}KB\t{result}\t{err}\n")
    except Exception:
        pass


async def qq_card_mid(keyword: str):
    """查 QQ 音乐，返回 (songmid, 是否VIP) 或 (None, False)。

    卡片是发给 QQ 音乐版权库的，和网易云搜索结果不是一家——按完整
    关键词（歌名+歌手）在 QQ 音乐再搜一次，取第一条。搜不到就放弃
    卡片，退回纯语音，不让用户看见半残体验。

    ⚠️ QQ 音乐搜索同样"宽容"：乱码也能返回不相干的歌（实测"9zq"
    返回了《珍惜对你例外的人》），所以第一条必须过相关性检查才准发。
    """
    try:
        async with aiohttp.ClientSession(
            headers={"User-Agent": UA, "Referer": "https://y.qq.com/"},
        ) as sess:
            async with sess.get(
                QQ_SEARCH,
                params={"w": keyword, "format": "json", "n": 5},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                d = await r.json(content_type=None)
        lst = ((d.get("data") or {}).get("song") or {}).get("list") or []
        for s in lst:
            if _qq_relevant(s, keyword):
                return s.get("songmid"), bool((s.get("pay") or {}).get("payplay"))
        return None, False
    except Exception:
        return None, False


def _qq_relevant(s: dict, keyword: str) -> bool:
    """QQ 音乐结果和关键词沾边才发卡片，比对逻辑和 usable() 同款。"""
    name = _norm(s.get("songname", ""))
    singer = _norm("".join(x.get("name", "") for x in s.get("singer") or []))
    album = _norm(s.get("albumname", ""))
    fields = (name, singer, album)
    words = [_norm(w) for w in re.split(r"[\s,，、/]+", keyword.strip()) if w]
    full = _norm(keyword)
    if full and (full in name or (name and name in full)):
        return True
    return any(w and any(w in f for f in fields) for w in words)


async def search(keyword: str) -> list:
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": UA, "Referer": "https://music.163.com/"}) as sess:
            async with sess.get(
                SEARCH,
                params={"s": keyword, "type": 1, "limit": 10, "offset": 0},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as r:
                d = await r.json(content_type=None)
        raw = ((d.get("result") or {}).get("songs")) or []
        out = []
        for s in raw:
            out.append({
                "id": s["id"],
                "name": s.get("name", ""),
                "artists": "/".join(a.get("name", "") for a in (s.get("artists") or [])),
                "album": (s.get("album") or {}).get("name", ""),
                "sec": int((s.get("duration") or 0) // 1000),
                "fee": s.get("fee", 0),
            })
        return out
    except Exception:
        return []


def _norm(t: str) -> str:
    """只留汉字和字母数字，用来比对名字。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", (t or "").lower())


def usable(s: dict, keyword: str) -> bool:
    """过滤掉注定发不出声的条目。

    网易云的搜索很"宽容"，输一串乱码也能返 10 条不相干的歌，所以：
    1. 关键词得跟 歌名/歌手/专辑 其中之一真的沾边（去符号后比对）
       —— 坑80：旧版只比歌名，导致"只搜歌手名"全军覆没
    2. 剔除伴奏 / 纯音乐 / DJ 版（这些是给翻唱伴奏用的，不是给人听的）
    """
    if FILTER_VIP and s.get("fee") == 1:
        return False
    # 坏词过滤必须放在所有提前 return 之前：旧版"整串包含"的提前
    # return 会绕过它，"晴天DJ版"这种就漏出去了（单测抓到的潜伏 bug）
    if any(w in s["name"].lower() for w in BAD_WORDS):
        return False
    name = _norm(s["name"])
    artist = _norm(s["artists"])
    album = _norm(s.get("album", ""))
    kws = [k for k in re.split(r"[\s,，、/]+", keyword.strip()) if k]
    if not kws:
        return True
    full = _norm(keyword)
    # 整串对歌名的互相包含（老行为，保住"歌名优先"的精确性）
    if full and (full in name or (name and name in full)):
        return True
    # 否则每个词在 歌名/歌手/专辑 任一字段命中即算沾边（救活纯歌手搜索）
    if not any(_norm(k) and (_norm(k) in name or _norm(k) in artist or _norm(k) in album)
               for k in kws):
        return False
    return True


async def fetch_audio(song_id: int):
    """返回 (音频 bytes 或 None, 时长秒, 错误说明)"""
    url = f"{STREAM}?type=url&id={song_id}"
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as sess:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200:
                    return None, 0, f"HTTP {r.status}"
                ctype = (r.headers.get("Content-Type") or "").lower()
                # 拿不到音频时接口会回一个空 text/html，正好挡住
                if "audio" not in ctype and "octet-stream" not in ctype:
                    return None, 0, "不是音频"
                size = int(r.headers.get("Content-Length") or 0)
                # 完整版动辄十几兆，超了就不下了，别白等一分钟
                if size > MAX_BYTES:
                    return None, 0, "文件过大"
                # 分块读，超过上限就停，避免把内存吃光
                chunks, got = [], 0
                async for chunk in r.content.iter_chunked(64 * 1024):
                    got += len(chunk)
                    if got > MAX_BYTES:
                        return None, 0, "文件过大"
                    chunks.append(chunk)
        data = b"".join(chunks)
        if len(data) < 20 * 1024:
            return None, 0, "文件过小"
        return data, mp3_seconds(data), ""
    except Exception as e:
        return None, 0, type(e).__name__


def rank_by_artist(songs: list, keyword: str) -> list:
    """关键字里带了歌手，就把该歌手的版本提前。

    网易云大量原唱因版权下架，搜出来多是翻唱。这里只保证"同名翻唱里
    优先挑歌手对得上的"，挑不到也不假装它是原曲。
    坑80：旧版用子串包含（artists in keyword），歌手名带 feat. 之类的
    就漏匹配；改成分词归一化后的互相包含。
    """
    kws = [_norm(k) for k in re.split(r"[\s,，、/]+", keyword.strip()) if k]

    def hit(s: dict) -> bool:
        a = _norm(s["artists"])
        if not a:
            return False
        return any(k and (k in a or a in k) for k in kws)

    hits = [s for s in songs if hit(s)]
    rest = [s for s in songs if not hit(s)]
    return hits + rest


def mp3_seconds(data: bytes) -> int:
    """不依赖第三方库，从 MP3 帧头估算时长。"""
    if len(data) < 4096:
        return 0
    i = 0
    if data[:3] == b"ID3":
        sz = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | \
             ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
        i = 10 + sz
    limit = min(len(data) - 4, 65536)
    while i < limit and not (data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0):
        i += 1
    if i >= limit:
        return 0
    br_idx = (data[i + 2] >> 4) & 0xF
    bitrates = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
    br = bitrates[br_idx] * 1000
    if not br:
        return 0
    return int(len(data) * 8 / br)


async def send_file(bot: Bot, event: GroupMessageEvent | PrivateMessageEvent,
                    s: dict, data: bytes) -> bool:
    """把整首歌作为群/私聊文件传上去（坑80：离开聊天界面也能听的正解）。

    NapCat 的 upload_group_file / upload_private_file 需要一个它自己
    读得到的绝对路径——bot 和 NapCat 同容器，data/ 下的路径直接可用。
    """
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        base = re.sub(r'[\\/:*?"<>|\s]+', "_", f"{s['name']}-{s['artists']}").strip("_")
        fname = f"{base}.mp3"
        fpath = CACHE_DIR / fname
        fpath.write_bytes(data)
        if isinstance(event, GroupMessageEvent):
            await bot.call_api("upload_group_file", group_id=event.group_id,
                               file=str(fpath), name=fname)
        else:
            await bot.call_api("upload_private_file", user_id=event.user_id,
                               file=str(fpath), name=fname)
        return True
    except Exception:
        return False


def build_card(s: dict) -> str:
    artist = s["artists"] or "佚名"
    return (
        f"🎵 {s['name']}\n"
        f"　　{artist}"
        f"{'　《' + s['album'] + '》' if s.get('album') else ''}\n"
        f"　　https://music.163.com/song?id={s['id']}"
    )
