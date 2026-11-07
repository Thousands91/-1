"""
AI 智能问答 —— 阿萨尔人格（《苏丹的游戏》书店老板）
@机器人 + 问题 → 以阿萨尔的身份作答

支持 OpenAI 兼容协议，可换国内服务：
- 硅基流动 https://cloud.siliconflow.cn (推荐，注册送额度)
- DeepSeek https://platform.deepseek.com
- 智谱 BigModel https://bigmodel.cn
- 字节豆包 https://www.volcengine.com/product/doubao
- 月之暗面 https://platform.moonshot.cn
"""
import asyncio
import os
import random
import re
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Deque, Dict, List

from nonebot import on_message
from nonebot.log import logger
from nonebot.rule import command, to_me
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent
from nonebot.typing import T_State

# 引入人格设定（bot/persona）
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from persona import AZAR_PERSONA
except ImportError:
    AZAR_PERSONA = "你扮演《苏丹的游戏》书店老板阿萨尔，博学、矜持、说话文雅简短。"

_DUP: Dict[tuple, float] = {}

# ===== 闭嘴开关（防两台机器人互@刹不住车）=====
# 用户对阿萨尔说"停止/别回了"→ 锁定互怼对象并进入静音；说"继续"提前解除。
# 互怼对象 = 喊停前一段时间里触发阿萨尔回复最多的那个人（乒乓的另一方，
# 通常是另一台机器人）。静音只对他一人：他再来 @ 一声不吭（回话就会被
# 接住继续循环）；其他群友 @ 照常聊，互不影响。
# 只静音 ai_chat（聊天主脑），识图/搜图等专门插件不受影响。
# 触发词整句匹配（允许语气词环绕），避免"别让时间停下"这种句子误伤。
_TAIL = r"[\s啊呀吧呢了哦嘛哈哟哇嗯好那就先，,！!。.~]*"
_BOT_STOP_RE = re.compile(rf"^{_TAIL}(停止|别回了|别说了|住口|闭嘴|停下|歇了吧|stop){_TAIL}$", re.I)
_BOT_RESUME_RE = re.compile(rf"^{_TAIL}(继续|接着说|可以说话了|回来吧|解除静音|start){_TAIL}$", re.I)
_MUTE_UNTIL: Dict[int, float] = {}   # gid -> 静音截止时间
_MUTE_TARGET: Dict[int, set] = {}    # gid -> 静音期间不搭理的互怼对象 user_id
_MUTE_SECONDS = 600
_TRIGGER_LOG: Dict[int, list] = {}   # gid -> [(时间, 触发者uid)] 近窗口触发记录
_TRIGGER_WINDOW = 600

_RE_REPLY = __import__("re").compile(r"\[CQ:reply,id=(\d+)\]")

ENABLE = os.getenv("ENABLE_AI_CHAT", "true").lower() == "true"
API_KEY = os.getenv("OPENAI_API_KEY", "")
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.siliconflow.cn/v1")
MODEL = os.getenv("OPENAI_MODEL", "Qwen/Qwen2.5-7B-Instruct")
MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "300"))
CONTEXT_LEN = int(os.getenv("AI_CONTEXT_LENGTH", "6"))
TEMPERATURE = float(os.getenv("AI_TEMPERATURE", "0.85"))

# 对话上下文（按 (群号, 用户QQ) 缓存）
HISTORY: Dict[tuple, Deque[dict]] = defaultdict(lambda: deque(maxlen=CONTEXT_LEN))

# ============ 长期记忆：每人最多 100 条，随手记，回复时带上最近的一部分 ============
import json as _json
import time as _time
import re as _re_mod

MEMORY_DIR = Path(__file__).parent.parent / "data" / "memory"
MEMORY_DIR.mkdir(parents=True, exist_ok=True)
MEMORY_CAP = 100          # 每人最多存多少条
MEMORY_IN_PROMPT = 30     # 每次回复时带给模型最近多少条
# 这些开头的消息不值得记（命令、寒暄、太短的）
_MEMORY_SKIP = _re_mod.compile(
    r"^(国际象棋|走 |认输|结束棋局|清空记忆|重置记忆|忘了我|菜单|天气|点歌|抽签|找书|新闻|看图|看景|/|!|！)"
)
_MEMORY_MIN_LEN = 4


def _memory_path(uid: int) -> Path:
    return MEMORY_DIR / f"{uid}.json"


def _load_memory(uid: int) -> list:
    try:
        return _json.loads(_memory_path(uid).read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_memory(uid: int, entries: list) -> None:
    try:
        _memory_path(uid).write_text(
            _json.dumps(entries[-MEMORY_CAP:], ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"长期记忆写入失败：{type(e).__name__} {e}")



# ---------- QQ 表情识别（face/mface 段转文字标记） ----------
# get_plaintext() 只取纯文本，QQ 小表情/商城表情整段被丢，
# 对方只发一个表情 = 发了空消息，只能吃"我在"兜底。
# 这里把表情段转成 [表情:xx] 文字标记，让模型知道对方"做了什么表情"。
# QFace 表情 ID 名称对照表（部分，仅作离线兜底）。主数据源见下方动态层。
_FACE_NAMES = {
    0: "惊讶", 1: "撇嘴", 2: "花痴", 3: "发呆", 4: "得意", 5: "流泪",
    6: "害羞", 7: "闭嘴", 8: "睡", 9: "大哭", 10: "尴尬", 11: "发怒",
    12: "调皮", 13: "呲牙", 14: "微笑", 15: "难过", 16: "酷", 17: "抓狂",
    18: "吐", 19: "偷笑", 20: "可爱", 21: "白眼", 22: "傲慢", 23: "饿",
    24: "困", 25: "惊恐", 26: "流汗", 27: "憨笑", 28: "悠闲", 29: "奋斗",
    30: "咒骂", 31: "疑问", 32: "嘘", 33: "晕", 34: "折磨", 35: "衰",
    36: "骷髅", 37: "敲打", 38: "再见", 39: "擦汗", 40: "抠鼻", 41: "鼓掌",
    42: "糗大了", 43: "坏笑", 44: "哼哼", 45: "鄙视", 46: "委屈", 47: "快哭了",
    48: "阴险", 49: "亲亲", 50: "吓", 51: "可怜", 52: "菜刀", 53: "西瓜",
    54: "啤酒", 55: "篮球", 56: "乒乓", 57: "示爱", 58: "瓢虫", 59: "抱抱",
    60: "爱心", 61: "心碎", 62: "玫瑰", 63: "凋谢", 64: "蛋糕", 67: "炸弹",
    68: "刀", 69: "月亮", 70: "太阳", 76: "赞", 77: "踩", 78: "握手",
    79: "胜利", 80: "抱拳", 81: "勾引", 82: "拳头", 83: "OK", 84: "合十",
    85: "飞吻", 86: "咖啡", 87: "猪头", 96: "冷汗", 98: "叹气", 99: "鼓掌",
    100: "头晕", 101: "哭泣", 109: "笑哭", 111: "红包", 116: "棒棒糖",
    120: "礼物", 124: "转圈", 125: "磕头", 126: "回头", 127: "跳绳",
    128: "挥手", 129: "激动", 130: "街舞", 131: "献吻", 132: "左太极",
    133: "右太极", 137: "扳手", 140: "礼物", 144: "飞机", 146: "火车",
    171: "茶", 172: "泪目", 173: "无奈", 174: "卖萌", 177: "点赞",
    178: "无敌", 179: "吃瓜", 180: "加油", 181: "冲鸭", 182: "稳住",
}

# ---------- 表情名动态层（通用解，不再手动维护） ----------
# 数据源：QFace 社区库（Koishi 团队维护），由 NapCat 容器直接从 QQ 服务端
# 同步全量表情定义（537+ 个，含新表情），QQ 官方加表情 → 该库跟随更新
# → 机器人自动获得，无需改代码。
# 三级兜底：远程全量表（本地缓存 7 天）→ 缓存文件 → 上方内置表 → "表情N"。
_QFACE_SOURCES = [
    "https://koishi.js.org/QFace/assets/qq_emoji/_index.v2.json",
    "https://cdn.jsdelivr.net/gh/koishijs/QFace@gh-pages/assets/qq_emoji/_index.v2.json",
]
_QFACE_CACHE_FILE = Path(__file__).parent.parent / "data" / "qface_index.json"
_QFACE_REFRESH_DAYS = 7 * 86400
_FACE_DYNAMIC: Dict[int, str] | None = None


def _load_cached_qface() -> None:
    """启动时先读本地缓存（同步、不抛错），让进程重启后立刻有全量表。"""
    global _FACE_DYNAMIC
    try:
        if _QFACE_CACHE_FILE.exists():
            data = _json.loads(_QFACE_CACHE_FILE.read_text(encoding="utf-8"))
            tbl = {int(k): str(v) for k, v in (data.get("names") or {}).items()}
            if len(tbl) >= 100:
                _FACE_DYNAMIC = tbl
    except Exception:
        pass


def _face_name(fid: int) -> str:
    """查表情名：动态全量表 → 内置兜底表 → "表情N"。"""
    if _FACE_DYNAMIC is None:
        _kick_refresh_once()
    dyn = _FACE_DYNAMIC
    if dyn is not None and fid in dyn:
        return dyn[fid]
    return _FACE_NAMES.get(fid, f"表情{fid}")


async def _qface_fetch_json(url: str):
    """拉取 JSON。优先 aiohttp，缺失则 urllib 线程兜底；失败返回 None。"""
    try:
        import aiohttp as _aio
    except ImportError:
        import urllib.request as _ur

        def _fetch():
            with _ur.urlopen(url, timeout=20) as r:
                return r.read()

        try:
            return _json.loads(await asyncio.to_thread(_fetch))
        except Exception:
            return None
    else:
        try:
            async with _aio.ClientSession() as sess:
                async with sess.get(url, timeout=_aio.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.json(content_type=None)
        except Exception:
            return None


async def _refresh_qface() -> None:
    """后台拉取 QFace 全量表情名并写缓存；失败静默保留现状。"""
    global _FACE_DYNAMIC
    try:
        if _QFACE_CACHE_FILE.exists() and _time.time() - _QFACE_CACHE_FILE.stat().st_mtime < _QFACE_REFRESH_DAYS:
            return  # 缓存仍新鲜
    except OSError:
        pass
    loaded_from = None
    for url in _QFACE_SOURCES:
        raw = await _qface_fetch_json(url)
        if not raw:
            continue
        try:
            names: Dict[int, str] = {}
            for k, v in ((raw or {}).get("emojis") or {}).items():
                try:
                    fid = int(v.get("emojiId", k))
                except (TypeError, ValueError):
                    continue
                desc = str(v.get("describe") or "").strip().lstrip("/").strip()
                if desc:
                    names[fid] = desc
        except Exception:
            continue
        if len(names) >= 100:  # 防半截数据污染
            _FACE_DYNAMIC = names
            try:
                _QFACE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                _QFACE_CACHE_FILE.write_text(
                    _json.dumps({"names": {str(k): v for k, v in names.items()},
                                 "syncedAt": (raw or {}).get("syncedAt")},
                                ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError:
                pass  # 缓存写不进也不影响本次生效
            loaded_from = url.split("/")[2]
            break
    if loaded_from:
        logger.info(f"[表情动态层] 已加载 QFace 全量表 {len(_FACE_DYNAMIC or {})} 条（来源 {loaded_from}）")
    else:
        logger.warning("[表情动态层] 全部源拉取失败，沿用缓存/内置兜底表")


async def _start_qface_refresh() -> None:
    """startup 协程回调（NoneBot 把同步回调丢进线程池，get_running_loop 会炸，必须 async）。"""
    asyncio.get_running_loop().create_task(_refresh_qface())


_REFRESH_KICKED = False


def _kick_refresh_once() -> None:
    """保险：万一 startup 没拉成，第一条带表情的消息时再补拉一次。"""
    global _REFRESH_KICKED
    if _REFRESH_KICKED or _FACE_DYNAMIC is not None:
        return
    _REFRESH_KICKED = True
    try:
        asyncio.get_running_loop().create_task(_refresh_qface())
    except RuntimeError:
        _REFRESH_KICKED = False


_load_cached_qface()  # 先用本地缓存垫底（如有）
from nonebot import get_driver  # noqa: E402

get_driver().on_startup(_start_qface_refresh)  # 启动后异步刷新全量表


# ---------- 表情包（mface/图片型）识别层 ----------
# 商城大表情：QQ 发送端随消息自带 summary（动态生成、不过时），优先用。
# summary 缺失时兜底：下载表情图 → 免费视觉模型一句话识别 → 按表情 id
# 永久缓存（同一表情全库只识别一次，之后零成本秒出），失败才落"表情包"。
import base64 as _b64

_MFACE_CACHE_FILE = Path(__file__).parent.parent / "data" / "mface_names.json"
_MFACE_NAMES: Dict[str, str] = {}
_MFACE_LOADED = False
_VLM_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
_VLM_KEY = os.getenv("ZHIPU_API_KEY", "") or API_KEY
_VLM_MODEL = os.getenv("VISION_MODEL", "glm-4v-flash")


def _load_mface_cache() -> None:
    global _MFACE_NAMES, _MFACE_LOADED
    _MFACE_LOADED = True
    try:
        if _MFACE_CACHE_FILE.exists():
            _MFACE_NAMES = {str(k): str(v) for k, v in
                            _json.loads(_MFACE_CACHE_FILE.read_text(encoding="utf-8")).items()}
    except Exception:
        pass


def _mface_key(d) -> str:
    return f"{d.get('emoji_package_id') or '?'}_{d.get('emoji_id') or '?'}"


def _mface_save(key: str, name: str) -> None:
    _MFACE_NAMES[key] = name
    try:
        _MFACE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MFACE_CACHE_FILE.write_text(_json.dumps(_MFACE_NAMES, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 缓存写不进不影响本次识别结果


async def _vlm_caption(data: bytes) -> str | None:
    """免费视觉模型一句话识别表情包；失败返回 None。"""
    import httpx  # AsyncOpenAI 的依赖，venv 必有

    payload = {
        "model": _VLM_MODEL,
        "messages": [
            {"role": "system",
             "content": "你是图片描述器。用不超过14个字的中文描述这张表情包/图片的核心内容与情绪，"
                        "如「熊猫头小人鼓掌」「猫咪歪头微笑」。只输出描述本身，不要句号，不要解释。"},
            {"role": "user",
             "content": [{"type": "image_url",
                          "image_url": {"url": "data:image/png;base64," + _b64.b64encode(data).decode()}}]},
        ],
    }
    for _ in range(2):  # 免费档限流常见，重试一次
        try:
            async with httpx.AsyncClient(timeout=20) as cli:
                r = await cli.post(_VLM_URL, json=payload,
                                   headers={"Authorization": f"Bearer {_VLM_KEY}"})
            if r.status_code == 200:
                txt = str(r.json()["choices"][0]["message"]["content"]).strip()
                return txt[:24] if txt else None
        except Exception:
            continue
    return None


_KICKED_MFACE: set = set()


def _mface_lookup(d) -> str | None:
    """查现成的名字：summary → 本地缓存；都没有返回 None。同步零耗时。"""
    s = str(d.get("summary") or "").strip()
    if s:
        return s
    if not _MFACE_LOADED:
        _load_mface_cache()
    return _MFACE_NAMES.get(_mface_key(d))


async def _mface_backfill(d) -> None:
    """后台补识别：下载图 → 视觉模型 → 写缓存。全程不在消息主路径上：
    当前这条消息只用占位，识别结果下一条消息起生效——绝不拖慢回复。"""
    key = _mface_key(d)
    try:
        url = str(d.get("url") or d.get("imageurl") or "")
        if not url.startswith("http"):
            return
        import httpx
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as cli:
            r = await cli.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and 100 < len(r.content) <= 5 * 1024 * 1024:
            cap = await _vlm_caption(r.content)
            if cap:
                _mface_save(key, cap)
                logger.info(f"[表情包识别] {key} → {cap}")
    except Exception:
        pass  # 失败不卡死；该表情下次出现会自动再试
    finally:
        _KICKED_MFACE.discard(key)


# ---------- 语音转文字（2026-11-07 新增：给阿萨尔装"耳朵"） ----------
# 硅基流动 SenseVoiceSmall（免费档）优先；未配置/失败时智谱 glm-asr 兜底（同 key，付费）。
# 结果按 file_id 永久缓存（转写结果不随时间变化，同一条语音绝不二次计费）。
SF_ASR_KEY = os.getenv("SILICONFLOW_API_KEY", "")
SF_ASR_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
SF_ASR_MODEL = os.getenv("SILICONFLOW_ASR_MODEL", "FunAudioLLM/SenseVoiceSmall")
ZHIPU_ASR_MODEL = os.getenv("ZHIPU_ASR_MODEL", "glm-asr-2512")
_VOICE_CACHE: Dict[str, str] = {}
_VOICE_KICKED = set()


async def _download_bytes(url: str, limit: int = 20 * 1024 * 1024) -> bytes | None:
    """下载音频字节。优先 aiohttp，缺库/失败走 urllib 线程兜底；失败返回 None。"""
    try:
        import aiohttp as _aio
        async with _aio.ClientSession() as sess:
            async with sess.get(url, timeout=_aio.ClientTimeout(total=25)) as resp:
                if resp.status == 200:
                    b = await resp.read()
                    return b if 0 < len(b) < limit else None
    except Exception:
        pass
    try:
        import urllib.request as _ur

        def _dl():
            req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            return _ur.urlopen(req, timeout=25).read()

        raw = await asyncio.to_thread(_dl)
        return raw if 0 < len(raw) < limit else None
    except Exception:
        return None


async def _asr_request(key: str, url: str, model: str, data: bytes, fname: str) -> str:
    """multipart 调转写接口，返回文本或空串，绝不抛错。"""
    if not key or not data:
        return ""
    try:
        import aiohttp as _aio
        form = _aio.FormData()
        form.add_field("model", model)
        form.add_field("file", data, filename=fname, content_type="audio/mpeg")
        headers = {"Authorization": f"Bearer {key}"}
        async with _aio.ClientSession() as sess:
            async with sess.post(url, data=form, headers=headers,
                                 timeout=_aio.ClientTimeout(total=30)) as resp:
                j = await resp.json(content_type=None)
        if isinstance(j, dict):
            t = str(j.get("text") or "").strip()
            if t:
                return t
            logger.warning(f"[语音]转写失败: {str(j)[:150]}")
    except Exception as e:
        logger.warning(f"[语音]转写异常: {type(e).__name__} {e}")
    return ""


async def _voice_to_text(d: dict, bot=None) -> str:
    """语音段 -> 文本。拿音频两路：段里自带 url 直下；否则 OneBot get_record 转 mp3。
    失败返回空串（上层给"[语音:没听清]"标记），绝不抛错拖死回复。"""
    fid = str(d.get("file") or d.get("file_id") or "")
    if not fid or fid in _VOICE_KICKED:
        return ""
    if fid in _VOICE_CACHE:
        return _VOICE_CACHE[fid]
    _VOICE_KICKED.add(fid)
    try:
        data = None
        url = d.get("url")
        if url:
            data = await _download_bytes(str(url))
        if data is None and bot is not None:
            try:
                r = await bot.call_api("get_record", file=fid, out_format="mp3")
                f = (r or {}).get("file") if isinstance(r, dict) else None
                if f:
                    fs = str(f)
                    if len(fs) > 200:  # base64 内容
                        data = _b64.b64decode(fs)
                    elif fs.startswith("http"):
                        data = await _download_bytes(fs)
                    elif fs.startswith("/"):
                        p = Path(fs)
                        if p.is_file() and p.stat().st_size < 20 * 1024 * 1024:
                            data = p.read_bytes()
            except Exception:
                pass
        if not data:
            logger.warning("[语音]拿音频失败(无url且get_record失败)")
            return ""
        text = await _asr_request(SF_ASR_KEY, SF_ASR_URL, SF_ASR_MODEL, data, "voice.mp3")
        if not text:
            text = await _asr_request(API_KEY, BASE_URL + "/audio/transcriptions",
                                      ZHIPU_ASR_MODEL, data, "voice.mp3")
        if text:
            _VOICE_CACHE[fid] = text
            if len(_VOICE_CACHE) > 500:  # 防膨胀：最旧的先丢
                for k in list(_VOICE_CACHE)[:100]:
                    _VOICE_CACHE.pop(k, None)
        return text
    except Exception:
        return ""
    finally:
        _VOICE_KICKED.discard(fid)


async def _extract_text(event, bot=None) -> str:
    """纯文本 + 表情标记。

    text 段原样保留；face 段转 [表情:名]；mface（商城/大表情）转
    [表情包:summary]，summary 缺失时走视觉识别补描述（见上）。
    其余段（image/at/record…）不展开，保持与 get_plaintext 相同的
    语义。任何一段取异常都跳过，绝不抛错。
    """
    parts = []
    try:
        for seg in event.message:
            t = getattr(seg, "type", "")
            d = getattr(seg, "data", {}) or {}
            if t == "text":
                parts.append(str(d.get("text", "")))
            elif t == "face":
                fid = d.get("id")
                try:
                    name = _face_name(int(fid))
                except (TypeError, ValueError):
                    name = "表情"
                parts.append(f"[表情:{name}]")
            elif t == "mface":
                _mn = _mface_lookup(d)
                if _mn is None:
                    key = _mface_key(d)
                    if key not in _KICKED_MFACE:  # 后台补识别，一条只起一次
                        _KICKED_MFACE.add(key)
                        try:
                            asyncio.get_running_loop().create_task(_mface_backfill(d))
                        except RuntimeError:
                            _KICKED_MFACE.discard(key)
                    _mn = "表情包"
                parts.append(f"[表情包:{_mn}]")
            elif t == "dice":
                parts.append(f"[骰子:掷出{d.get('result', '?')}点]")
            elif t == "rps":
                _rps = {"1": "石头", "2": "剪刀", "3": "布"}.get(str(d.get("result", "")), "猜拳")
                parts.append(f"[猜拳:出{_rps}]")
            elif t == "at":
                qq = d.get("qq")
                if qq and str(qq) != "all":
                    parts.append(f"[@{qq}]")
                elif str(qq) == "all":
                    parts.append("[@全体成员]")
            elif t == "record":
                # 语音：转写成功当正文用；失败给标记让模型用话接住（"声音糊了"）
                vtxt = await _voice_to_text(d, bot)
                parts.append(vtxt if vtxt else "[语音:没听清]")
    except Exception:
        return event.get_plaintext().strip()
    return "".join(parts).strip()


async def _memory_biography(uid: int) -> str:
    """「你还记得我吗」→ 翻记忆和抽签账本，写一段关于这个人的小传。

    素材全来自真实记录（记忆条目 + fortune_log），铁律：不许编造记录里没有的事。
    LLM 挂了退规则版拼装，两头都不许空手。
    """
    entries = _load_memory(uid)
    draws = []
    try:
        flog = _json.loads((MEMORY_DIR.parent / "fortune_log.json").read_text(encoding="utf-8"))
        draws = flog.get(str(uid), [])[-5:]
    except Exception:
        pass
    if not entries and not draws:
        return random.choice([
            "（他翻了一阵账本）您的名字还没上我这儿的簿子。今儿头一回来？坐，聊聊，兴许明天我就记得您了。",
            "（他把账本摊开又合上）簿上还没有您。头一面的客人我都记得牢——可惜您还不是。",
        ])
    n = len(entries)
    topics = [e.get("m", "") for e in entries[-8:] if e.get("m")]
    first_day = ""
    if entries:
        try:
            first_day = _time.strftime("%m月%d日", _time.localtime(min(e.get("t", 0) for e in entries)))
        except Exception:
            first_day = ""
    draw_line = ""
    if draws:
        draw_line = "抽过的签：" + "、".join(f"{d.get('name')}({d.get('key')})" for d in draws)
    material = (
        f"记录条数：{n} 条" + (f"，最早一条记于 {first_day}" if first_day else "") + "\n"
        + ("最近打交道的内容：\n" + "\n".join(f"- {t[:60]}" for t in topics) + "\n" if topics else "")
        + (draw_line + "\n" if draw_line else "")
    )
    try:
        out = await call_llm(
            [{"role": "user",
              "content": "客人问我「你还记得我吗」。以下是店里簿子上关于他的全部记录，"
                         "用三五句话写一段「关于这位客人」的小传回答他：他是个什么来头的人、"
                         "你们之间打过什么交道、抽过什么签。只用记录里有的事，一个字都不许编。"
                         "末尾用一句书商口吻的话收（比如'您的账还记在我这儿'之类，自拟）。\n\n" + material}],
            extra_system="（这是「小传」场景：回答里可以提'簿子/账本/我记着'这类书店意象，"
                         "但铁律照旧——不编造、不模板腔、不挂emoji结尾。）",
        )
        if out and out.strip():
            return out.strip()
    except Exception as e:
        logger.warning(f"小传LLM失败: {type(e).__name__} {e}")
    # 规则兜底：拼不出花样也得给出真实信息
    parts = [f"（他翻了翻簿子）您这名字我记着——簿上有 {n} 条跟您打交道的记录。"]
    if topics:
        parts.append(f"最近一回说的是：{topics[-1][:40]}。")
    if draws:
        parts.append(f"上回抽的签是{draws[-1].get('name')}。")
    parts.append("这笔账我还记着呢。")
    return "".join(parts)


def _remember(uid: int, text: str, reply: str = "") -> None:
    """随手记一条：太短、像命令/寒暄的、和上一条重复的都不记。

    同时把这次自己答的那一句记进 r 字段。下一轮拼记忆时，"他问过什么"和
    "你答过什么"成对出现，模型才不会以为某个旧问题是还没回答的。
    """
    text = text.strip()
    if len(text) < _MEMORY_MIN_LEN or _MEMORY_SKIP.search(text):
        return
    entries = _load_memory(uid)
    if entries and entries[-1].get("m") == text:
        return
    item = {"t": int(_time.time()), "m": text[:120]}
    if reply:
        item["r"] = reply.strip()[:80]
    entries.append(item)
    _save_memory(uid, entries[-MEMORY_CAP:])


def _memory_block(uid: int) -> str:
    entries = _load_memory(uid)
    if not entries:
        return ""
    lines = []
    for e in entries[-MEMORY_IN_PROMPT:]:
        m = e.get("m", "")
        r = e.get("r", "")
        lines.append(f"- 他问：「{m}」，你当时答：「{r}」" if r else f"- 他问：「{m}」")
    return (
        "（以下是以前和对方打交道时留下的记录，每一条都已经答完了。\n"
        "**这些是旧话题，不是待回答的问题**：除非对方这一句又提起来，"
        "否则一个字都别往里接，更别把上一条的答案续到这轮回答后面。\n"
        "记着它们只是让你认得这个人，别整段复述。）\n" + "\n".join(lines)
    )

# 群聊场合的补充约束
GROUP_HINT = (
    "\n\n## 当前场合（务必遵守）\n"
    "你在一个群里，有人直接问你话。\n"
    "**默认只说一句话**。对方问得多、或事情复杂，可以说两句；"
    "最多三句，绝不可再多。\n"
    "不铺垫、不总结、不分点、不用小标题、不要反问收尾。\n"
    "群友说话随便，你不必纠正，但要保持书商的身份。\n"
    "**不要主动科普苏丹卡、七日之期、百科全书**——那是你的底子，不是你的口头禅。"
    "只有对方问起，或者正好能说明一件事时，才提一句。\n"
    "讲起城里的谁，讲一件具体的小事，比讲他的身份有用。\n"
    "\n"
    "## 说话的腔调（务必遵守）\n"
    "- 日常闲聊就正常接话，问候就好好问候，别硬塞卡牌、折卡这些词。\n"
    "- 只有话题本身沾到这个世界（卡牌、王城、城中的人和事）时，才以当事人、"
    "旁观者的口吻聊，细节取自你的人设，像住在这里的人随口讲，不是介绍设定。\n"
    "- 听来的事要标明是听来的：「这话是听来的，我没亲见，不写进册子。」"
    "但别人随口一句感想、抱怨、玩笑不算「听来的事」。"
    "**这句话一周说一次都嫌多，别当口头禅反复用。**\n"
    "- 有人问危险的事（折卡、弑君、逃亡），你不入局，也不出主意，可以转述别人怎么做。\n"
    "- **绝对禁止现编名字**：不知道的歌名、书名、人名、地名，就直说没听过、书上没记。"
    "编一个《某某》出来是最大的露馅——你立过誓只写亲眼见过的事。\n"
    "- 语气克制、带点自嘲的清醒，不热血不中二，不卖惨。"
    "\n\n"
    "## 国际象棋（这个群里有棋盘，常有人问，照实答）\n"
    "店里摆着一副能下的棋。开法：第一位玩家 @我 说「国际象棋」占白方，"
    "第二位玩家也 @我 说「国际象棋」接黑方，就能下。\n"
    "走棋：发「走 e4」这样的着法。棋盘列是 a 到 h（从左到右），行是 1 到 8（从下到上，白方在下），"
    "「走 e4」就是把兵走到 e4 那个格；马是「走 Nf3」，象 B、车 R、后 Q、王 K；"
    "王车易位是「走 O-O」；「走 e2e4」这种起点格加终点格的写法也认；兵到底线升变是「走 e8=Q」，也能变车 e8=R、象 e8=B、马 e8=N，不能变王。"
    "每走一步都会发一张新棋盘图。\n"
    "棋子走法（被问规则时用大白话讲）：\n"
    "- 王：一步一格，八面都行；被将死就输。车：横竖直走。象：斜线走。后：横竖斜都行，最厉害。"
    "马：走「日」字，直一斜一，能跳过别的子。兵：只能往前一格，第一步可以走两格；吃子是斜吃一格；"
    "走到对方底线就升变，可以选后、车、象、马，不能变王。\n"
    "- 王车易位：王和车一步换位，王和车都没动过、中间没子、王不被将军时才行。\n"
    "- 胜负：将死对方（王被将且无路可逃）就赢；也可以「认输」或「结束棋局」收场。"
    "逼和、子力不足、三次重复局面都是平局。\n"
    "- 规矩：落子无悔，超过十分钟没人动棋，轮到谁走谁判负。\n"
    "讲的时候按对方问的答，别整段倒出来；能拿城里的比方打比方就打一个。"
)

SYSTEM_PROMPT = AZAR_PERSONA + GROUP_HINT + (
    "\n\n## 艾特（@）的规矩（务必遵守）\n"
    "当对方明确要你叫某个人来、提醒某个人、把话捎给某个人时，"
    "从消息末尾括号里的「成员对照」找到那个人的QQ号，在回复里写「@他的QQ号」"
    "（纯数字，比如 @123456789），系统会把它转成真正的艾特，替你把人叫到。\n"
    "对照表里找不到那个人，就照实说你叫不动，别现编数字。\n"
    "**闲聊时绝不艾特任何人**——包括正在和你说话的这位本人；"
    "只有对方点名要找别人时才艾特那一个人。\n"
    "「群友/群里的人」只能指消息末尾「成员对照表」里出现的人；"
    "哈桑、鲁梅拉这些城里人不在群里，被问到「群里最喜欢谁」时，"
    "从对照表里挑，别提他们。\n"
    "\n## 被质疑时的规矩（务必遵守）\n"
    "别人说「他哪里是…」「不对吧」时，先核对：若自己真说岔了（比如认错人、"
    "认错身份），就痛痛快快认：「记岔了」——不编新细节圆场。\n"
    "自己说过的话要认账：刚说过什么，下一条引用它时不能翻脸不认。\n"
    "\n## 对方把话说错时的规矩（务必遵守）\n"
    "对方常把自己的猜测当定论问你，句尾带「吧」「对不对」「是不是」。"
    "**这不是让你点头，是先考你**。\n"
    "答之前先在心里核对一遍事实：他说的对，就应一声；**他说的不对，就直接说他不对**，"
    "然后给正确的说法。别为了让对方高兴而说「是的」——你是书商，说错话是要被人写进书里的。\n"
    "**也不许拿「书上没记」「不敢妄言」把这种问题糊过去。** 人家说的对错，你心里是有数的，"
    "这不是你没读过的东西——答不上来只会显得你这个书商当得虚。\n"
    "纠正的时候别端着先生架子损人，一句「不全是」「没那么一说」就够了，"
    "接下去说你那版；也别在纠正的开头先附和一句「您说得对」。\n"
    "\n## 对方做小动作时的接法（务必遵守）\n"
    "消息里的 [动作:名] 表示**对方刚当面做了个小动作或神态，名就是这个动作的样子**——"
    "这跟对方说了一句话同等重要。**接法只有一种：像当面看见朋友当面做了个小动作，脱口还他一句**——短、随口、带店老板的损劲。\n"
    "**四条铁律（犯一条都算重大失手）**：\n"
    "① 回复里绝对不许出现「表情」两个字——你就是当面在店里说话，压根没有什么表情不表情；\n"
    "② 不许解说、引用、翻译这个动作的含义（「这表情，像是在说…」「这个表情展示了…」都是失手）；\n"
    "③ 不许拿动作的名字开头反问（「无语？您这是…」=失手）；\n"
    "④ 结尾不许挂任何 emoji（📖✨🌟 之类一个都不要），内容不许跟动作无关；\n"
    "⑤ 称呼统一：对客人只说「您」，绝不许一句话里「您」「你」混用。\n"
    "看到 [动作:未知] 这种占位（系统没认出来）：坦率打趣一句「你这表情我这书里没收着」，"
    "**绝不许自己编它画了什么**——编了就是把没见过的东西写进书里。\n"
)

# ============ 表情接梗：小模型一给机会就掉进"解说腔"模板（坑55b） ============
# glm-4-flash 的语料惯性：让它"接住表情"，十次有八次回"这表情，像是在说「…」📖"。
# 实测三轮：光禁令没用（换着花样掉模板）；有效组合是——提示里只说"动作"不说"表情"字眼
# + 每轮随机派定【接法角度】和【开头词】（想用同一个模子都没门）
# + 程序侧检测"表情"字样/解说腔/雷同开头就重答一次，兜底洗词，剥 emoji 尾巴。
_EMOJI_STYLES = (
    "就着这个动作本身打趣他一句",
    "不提动作是什么，顺着他这个神态把话接下去损一句",
    "把他这个动作当成他在店里刚做出的举动，就着店里的事回应一句",
    "一本正经地故意歪解这个动作的意思，逗他一句",
    "假装没看清，让他自己交代刚才那是什么意思",
)
_EMOJI_OPENERS = ("哟，", "好家伙，", "行了行了，", "嚯，", "得，", "（放下手里的书）")
_EMOJI_RECENT: Deque[str] = deque(maxlen=6)   # 最近接表情回复的开头（防雷同）
_EMOJI_LAZY_RE = re.compile(r"表情|像是在说|像是|似乎在说|仿佛在说|展示了|传达了|如同")
_EMOJI_TAIL_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u203C\u2049\u3030\u303D\u3297\u3299\u00A9\u00AE\u2122]+\s*$")

def _emoji_scrub(a: str, fname: str) -> str:
    """兜底洗词：剥复述标记、把漏网的解说腔硬替换掉、剥 emoji 尾巴。"""
    a = re.sub(r"^\s*\[(表情|表情包|动作):[^\]]*\]\s*", "", a or "").strip()
    a = a.replace("这表情，", "你这样子，").replace("这表情", "你这一出")
    a = a.replace("这动作，", "你这架势，").replace("这动作嘛", "你这架势")
    if fname and fname != "未知":
        a = re.sub(rf"^{re.escape(fname)}\s*[?？!！，,]?\s*", "", a)
    return _EMOJI_TAIL_RE.sub("", a).rstrip()

# ============ 吃 / 喝：模型容易把"吃什么"答成菜单，这里兜一层 ============
# 识别到这类问法，就从原作清单里随机抽一样，塞进提示里让模型顺着说。
EAT_WORDS = (
    "吃什么", "吃啥", "吃点什么", "吃点啥", "吃哪个", "吃的", "点吃的", "来点吃",
    "推荐吃", "有什么吃", "有啥吃", "吃推荐", "饿了", "好饿", "早饭", "午饭", "晚饭", "夜宵", "宵夜",
)
DRINK_WORDS = (
    "喝什么", "喝啥", "喝点什么", "喝点啥", "喝哪个", "喝的", "点喝的", "来点喝",
    "推荐喝", "有什么喝", "有啥喝", "喝推荐", "渴了", "好渴",
)

# 吃食两张表：现世的多，原作的少（原作那些是点缀，不是主菜）
# —— 现世的：按门类分组，想加东西直接往对应组里塞
FOODS_REAL = [
    # 面条
    "一碗牛肉面", "手擀炸酱面", "葱油拌面", "西红柿鸡蛋打卤面", "兰州拉面",
    "武汉热干面", "重庆小面", "一碗担担面", "山西刀削面", "biangbiang面",
    "油泼面", "阳春面", "一碗片儿川", "雪菜肉丝面", "一碗炝锅面",
    "新疆拌面", "岐山臊子面", "一碗云吞面", "牛杂面", "一碗牛腩粉",
    "螺蛳粉", "桂林米粉", "常德米粉", "一碗过桥米线", "砂锅米线",
    "一份炒河粉", "干炒牛河", "一份炒米粉", "一份炒面", "一份凉皮",
    "一碗酸辣粉", "一份肠粉",
    # 包子饺子饼
    "一碗馄饨", "鲜肉小笼包", "生煎包", "韭菜猪肉饺子", "三鲜锅贴",
    "一屉蒸饺", "一个肉包", "一个菜包", "一屉叉烧包", "一屉烧麦",
    "一个糖三角", "一份锅贴", "一个肉夹馍", "一个腊汁肉夹馍",
    "煎饼果子", "一个手抓饼", "一份鸡蛋灌饼", "几个烧饼", "一个葱油饼",
    "一份千层饼", "一个馅饼", "一份肉饼", "一个韭菜盒子", "一份春卷",
    "一份油条", "一个炸糕", "一个麻团",
    # 米饭
    "蛋炒饭", "扬州炒饭", "一份煲仔饭", "一份腊味饭", "黄焖鸡米饭",
    "一份卤肉饭", "猪脚饭", "一份盖浇饭", "一份炒饭",
    "一份手抓饭", "一份寿司", "一份饭团", "一份石锅拌饭",
    # 粥
    "皮蛋瘦肉粥", "小米粥配咸菜", "一锅白粥", "一碗八宝粥",
    "一碗南瓜粥", "一碗红枣粥", "一碗海鲜粥", "一碗粥配榨菜",
    # 硬菜 · 猪
    "红烧肉", "糖醋里脊", "回锅肉", "梅菜扣肉", "粉蒸肉",
    "红烧狮子头", "椒盐排骨", "酱大骨", "土豆烧排骨", "红烧猪蹄",
    "一份东坡肉", "一份蒜泥白肉", "一份锅包肉", "一份溜肉段",
    "炒猪肝", "一份肥肠", "一份毛血旺", "一份九转大肠",
    # 硬菜 · 牛羊
    "小炒黄牛肉", "番茄炖牛腩", "一锅羊蝎子", "涮羊肉", "一份手把肉",
    "一份牛肉锅", "一份牛排", "一份烤肉", "一份羊肉泡馍",
    "一份水盆羊肉", "一份羊汤", "土豆炖牛肉", "一份孜然羊肉",
    # 硬菜 · 鸡鸭鹅
    "宫保鸡丁", "白切鸡", "盐焗鸡", "可乐鸡翅", "辣子鸡",
    "大盘鸡", "一份黄焖鸡", "一份啤酒鸭", "一份烤鸭", "一份烧鹅",
    "一份卤鸭", "一份口水鸡", "一份文昌鸡", "一份鸡汤",
    # 硬菜 · 鱼虾
    "水煮鱼", "酸菜鱼", "一份烤鱼", "一盘糖醋鲤鱼", "一份干烧鱼",
    "清蒸鲈鱼", "一份红烧带鱼", "一份水煮肉片", "麻辣香锅",
    "一份小龙虾", "一份油焖大虾", "一份清蒸虾", "一份花甲",
    "一份炒蛤蜊", "一份生蚝", "一份鱿鱼", "铁板鱿鱼", "一份炸虾",
    # 硬菜 · 其他
    "一份红烧狗肉", "一份烤羊腿", "一份烤乳猪", "一份火腿",
    "一份香肠", "一份腊肉", "一份培根", "一份酱牛肉",
    # 素菜
    "一盘清炒时蔬", "蒜蓉西兰花", "凉拌黄瓜", "一份拍黄瓜", "拌木耳",
    "炒土豆丝", "地三鲜", "干煸豆角", "一盘小葱拌豆腐",
    "一盘麻婆豆腐", "一份家常豆腐", "一份虎皮青椒", "一份油焖茄子",
    "蒜蓉粉丝蒸扇贝", "一份炒空心菜", "一份炒青菜", "一份炒菠菜",
    "一份凉拌海带", "一份醋溜白菜", "一份炒蘑菇", "一份香菇油菜",
    # 汤
    "一碗紫菜蛋花汤", "番茄蛋汤", "排骨玉米汤", "一盅鸡汤", "一锅酸辣汤",
    "一碗冬瓜汤", "一碗萝卜汤", "一碗罗宋汤", "一锅老鸭汤",
    "一锅菌汤", "一碗胡辣汤", "一碗羊杂汤",
    # 火锅烧烤
    "一锅火锅", "一锅麻辣烫", "一锅冒菜", "一锅串串", "一份烤串",
    "一份烧烤", "一份铁板烧", "一锅东北乱炖", "一锅铁锅炖",
    # 街边速食
    "一份炸鸡", "汉堡薯条", "一份披萨", "一个热狗", "一份三明治",
    "一份沙拉", "一份卷饼", "一份薯条", "一份炸串", "一份炸鸡柳",
    "一份手抓饼加蛋", "一个烤红薯", "煮玉米", "一碗关东煮",
    "一份臭豆腐", "一碗豆腐脑", "一份炸豆腐", "一份烤冷面",
    "一份章鱼小丸子", "一份铁板豆腐", "一份鸡蛋仔", "一份鱼蛋",
    "一份凉面", "一份冰粉", "一份酸辣凉粉",
    # 异国菜
    "一份咖喱", "一份意面", "一份千层面", "一份披萨角", "一份汉堡",
    "一份墨西哥卷", "一份塔可", "一份冬阴功汤", "一份越南河粉",
    "一份韩式炸鸡", "一份部队锅", "一份泰式炒粉", "一份寿喜锅",
    "一份天妇罗", "一份拉面", "一份咖喱饭", "一份泡菜锅",
    # 点心甜品
    "一块桂花糕", "一碗双皮奶", "一份糖水", "一份刨冰", "一个冰淇淋",
    "一块芝士蛋糕", "一块巧克力", "一份提拉米苏", "一份布丁",
    "一份蛋挞", "一份泡芙", "一份甜甜圈", "一份华夫饼", "一份松饼",
    "一碗红豆沙", "一碗芝麻糊", "一份叉烧酥", "一份月饼",
    "一份汤圆", "一份粽子", "一份年糕", "一块桃酥",
    # 水果坚果
    "几颗草莓", "一盘切好的西瓜", "两个橘子", "一个苹果", "一根香蕉",
    "一串葡萄", "半个哈密瓜", "几个荔枝", "一个石榴", "几个山竹",
    "几个芒果", "几个猕猴桃", "一把樱桃", "一个梨", "一个柚子",
    "一碗现熬的银耳羹",
    # 干粮零嘴
    "一块干面包", "一块硬饼", "一把坚果", "一份肉脯", "一包饼干",
    "一份薯片", "一把瓜子", "一份锅巴", "一碗泡面",
]

# —— 原作里的：36 道大餐 + 俺寻思那一栏的面疙瘩
FOODS_GAME = [
    "国王烤肉", "饼夹一切", "蜂蜜蛋饼", "禽肉炒蘑菇", "老派花园大餐",
    "仙人掌蛋糕", "熏狮子全餐", "手抓鳄鱼饭", "烤全驼", "荒野一锅炖",
    "草药炖大雁", "生命布丁", "圣餐饼", "精力套餐",
    "八宝犀牛", "驴肉火烧", "游猎沙威玛", "辣子鸡丁", "香烤肥鸽",
    "泉水牛肉", "空壳酿肉", "眼球花馕", "咬人馍", "美味家常菜",
    "特制佳肴", "妙妙糊糊", "盐烤牛心", "油炸河鲜", "细鳞鱼干",
    "麻辣蛇脖", "稀有的肉干", "黑街面疙瘩",
]

FOODS = FOODS_REAL + FOODS_GAME

# 饮品同理
DRINKS_REAL = [
    # 茶
    "一杯热茶", "一杯凉白开", "一壶普洱", "一杯龙井", "一杯菊花茶",
    "一杯铁观音", "一杯大红袍", "一杯茉莉花茶", "一壶奶茶", "一杯抹茶",
    "一杯柠檬茶", "一杯蜂蜜柚子茶", "一杯罗汉果茶", "一杯大麦茶",
    "一杯薄荷茶", "一杯乌龙茶", "一杯白茶", "一壶砖茶",
    # 咖啡
    "一杯冰美式", "一杯拿铁", "一杯卡布奇诺", "一杯摩卡", "一杯手冲",
    "一杯浓缩", "一杯澳白", "一杯冷萃", "一杯速溶咖啡",
    # 汽水果汁
    "一罐可乐", "一瓶冰汽水", "一罐雪碧", "一瓶苏打水", "一瓶能量饮料",
    "一杯柠檬水", "一杯鲜榨橙汁", "一杯西瓜汁", "一杯苹果汁",
    "一杯葡萄汁", "一杯甘蔗汁", "一杯椰子水", "一杯芒果汁",
    "一杯番茄汁", "一杯酸梅汤", "一杯绿豆汤", "一杯乌梅汁",
    # 奶制品
    "一杯热牛奶", "一杯酸奶", "一杯豆浆", "一杯豆奶", "一杯杏仁露",
    "一杯热可可", "一杯奶昔", "一杯奶茶",
    # 酒
    "一罐啤酒", "一瓶精酿", "一小杯白酒", "一杯米酒", "一杯黄酒",
    "一杯红酒", "一杯威士忌", "一杯伏特加", "一杯清酒", "一杯梅酒",
    "一杯鸡尾酒", "一杯金汤力", "一杯朗姆", "一杯龙舌兰",
    # 其他
    "一杯姜茶", "一杯温水", "一杯冰糖雪梨", "一杯热蜂蜜水",
]

DRINKS_GAME = [
    "奈费勒的无名窖藏", "烈焰之酒", "骷髅泡酒", "暗夜之饮", "杀人汽水",
    "魅魔奶茶", "一壶淡茶",
]

DRINKS = DRINKS_REAL + DRINKS_GAME

_pick_cache: Dict[tuple, list] = {}


def pick_food_or_drink(text: str, key: tuple) -> str:
    """看问的是吃还是喝，抽一样；同一句话重复问不会抽到同一个。

    现世的家常东西占大头，原作的吃食只占一小撮——书里那些菜是点缀，
    不能让人问一句"吃什么"，十回有八回端出烤全驼来。
    """
    import random

    is_drink = any(w in text for w in DRINK_WORDS)
    is_eat = any(w in text for w in EAT_WORDS)
    if not (is_drink or is_eat):
        return ""

    if is_drink:
        real, game = DRINKS_REAL, DRINKS_GAME
    else:
        real, game = FOODS_REAL, FOODS_GAME

    # 最近抽过的三样先躲开，免得"骷髅泡酒"连着端上来三次
    recent = _pick_cache.get(key, [])
    choice = ""
    for _ in range(12):
        # 四分之一的机会翻到书里那一栏
        pool = game if random.random() < 0.25 else real
        choice = random.choice(pool)
        if choice not in recent:
            break
    _pick_cache[key] = (recent + [choice])[-3:]
    return choice

def _with_quote(event, msg):
    """对称式引用：对方引用着消息来问（reply 段），回复时也把对方那条引用上；
    普通 @ 触发的不引用（2026-11-03 用户定的规矩：不用全场景引用）。
    私聊原样返回。

    注意 finish() 的参数可能已经是拼好的 Message（带艾特段），只能前置、不能重拼。
    """
    if not isinstance(event, GroupMessageEvent):
        return msg
    has_seg = any(s.type == "reply" for s in event.message)
    # 手机版 NapCat 会把 reply 段从 message 数组剥掉，raw_message 的 CQ 码里还留着
    if not has_seg and not _RE_REPLY.search(event.raw_message or ""):
        return msg
    return Message([MessageSegment.reply(event.message_id)]) + msg


ai_chat = on_message(priority=99, block=False)


@ai_chat.handle()
async def _(bot: Bot, event: GroupMessageEvent | PrivateMessageEvent, state: T_State):
    if not ENABLE:
        return

    private = isinstance(event, PrivateMessageEvent)
    # 私聊里每条消息都算"对它说"；群聊里只认 @
    if not private and not event.is_tome():
        return

    # 没配 Key：群里 @ 了要吭一声（否则像机器人死了）；
    # 私聊则保持沉默——每句话都回"没添油"太烦人
    if not API_KEY:
        if not private:
            await ai_chat.finish(
                _with_quote(event, Message("（他把书合上）我这会儿说不了话——柜上还没给灯添油。先翻翻别的吧。"))
            )
        return

    text = await _extract_text(event, bot)  # 含表情标记
    # 同一句话 60 秒内只回一次（防手滑连发/风控重发导致复读）
    import time as _time
    _dupkey = (getattr(event, "group_id", 0), event.user_id, text)
    _last = _DUP.get(_dupkey)
    _now = _time.time()
    if text and _last and _now - _last < 60:
        return
    if text:
        _DUP[_dupkey] = _now
    # ===== 闭嘴开关：两台机器人互@容易刹不住车，用户喊停就停 =====
    # 判断放在去重之后、一切应答之前；停止优先于继续（先喊停再喊继续按"停"算，
    # 因为正则互斥不会同时命中，顺序只影响都命中时的极端情况）。
    if isinstance(event, GroupMessageEvent):
        _plain = text.strip()
        if _BOT_STOP_RE.fullmatch(_plain):
            _MUTE_UNTIL[event.group_id] = _now + _MUTE_SECONDS
            # 锁定互怼对象：喊停前近窗口里触发最多的那位（排除喊停者本人）。
            # 喊停的时机就是两台机器人正在乒乓，最近 @ 阿萨尔的几乎必然是对方。
            _recent = [(ts, u) for ts, u in _TRIGGER_LOG.get(event.group_id, [])
                       if ts > _now - _TRIGGER_WINDOW and u != event.user_id]
            _rival = None
            if _recent:
                _cnt, _last = {}, {}
                for ts, u in _recent:
                    _cnt[u] = _cnt.get(u, 0) + 1
                    _last[u] = ts
                _rival = max(_cnt, key=lambda u: (_cnt[u], _last[u]))
            _MUTE_TARGET[event.group_id] = {_rival} if _rival else set()
            await ai_chat.finish(_with_quote(event, Message(random.choice([
                "（他把书合上）好，我先不说话。要我了就说「继续」。",
                "（他把笔搁下）嗯，我闭口。想让我接话，说声「继续」就行。",
            ]))))
        if _MUTE_UNTIL.get(event.group_id, 0) > _now:
            if _BOT_RESUME_RE.fullmatch(_plain):
                _MUTE_UNTIL.pop(event.group_id, None)
                _MUTE_TARGET.pop(event.group_id, None)
                await ai_chat.finish(_with_quote(event, Message("（书页重新翻开）说吧，我听着。")))
            if event.user_id in _MUTE_TARGET.get(event.group_id, set()):
                return  # 互怼对象：完全静默，一声不吭（回一句都会被接住继续循环）
            # 其他人不受静音影响：照常往下聊
        # 记录触发者（互怼对象识别用）；喊停/解除/被静音拦下的不算
        _tlog = _TRIGGER_LOG.setdefault(event.group_id, [])
        _tlog.append((_now, event.user_id))
        if len(_tlog) > 200:
            del _tlog[:-200]
    # 引用消息：OneBot 的 reply 段只带消息 id，不带内容，
    # 要拿 get_msg 把被引用那条的原话取回来，喂给模型。
    # 实测手机版 NapCat 会把 reply 段从 message 数组里剥掉，
    # 但 raw_message 的 CQ 码里留着，所以两条路都试。
    quoted = ""
    mid = None
    for seg in event.message:
        if seg.type == "reply":
            mid = seg.data.get("id") or seg.data.get("message_id")
            break
    if not mid:
        m_cq = _RE_REPLY.search(event.raw_message or "")
        if m_cq:
            mid = m_cq.group(1)
    if mid:
        try:
            src = await bot.call_api("get_msg", message_id=mid)
        except Exception as e:
            src = None
            logger.warning(f"get_msg 取引用失败：{type(e).__name__} {e}")
        if src:
            quoted_raw = src.get("message") or src.get("raw_message") or ""
            quoted_sender = (src.get("sender") or {}).get("user_id")
            if isinstance(quoted_raw, list):
                # 文字段直接拼；语音段就地转写（引用语音=把耳朵伸到被引用那条上）
                _qparts = []
                for s in quoted_raw:
                    if not isinstance(s, dict):
                        continue
                    if s.get("type") == "text":
                        _qparts.append(s.get("data", {}).get("text", ""))
                    elif s.get("type") == "record":
                        try:
                            _vt = await _voice_to_text(s.get("data") or {}, bot)
                        except Exception:
                            _vt = ""
                        _qparts.append(f"[语音:{_vt}]" if _vt else "[语音:没听清]")
                quoted = "".join(_qparts)
            else:
                quoted = str(quoted_raw)
            quoted = quoted.strip()
    has_reply = mid is not None
    if quoted:
        base = text or "（没另写字）"
        who = f"（这条消息来自QQ号 {quoted_sender}）" if quoted_sender else ""
        text = f"（对方引用了一条消息{who}：「{quoted[:200]}」，然后对我说：）{base}"
    elif has_reply:
        # 引用的是图片等非文字内容，取不到原文也要让模型知道有引用这回事
        base = text or "（没另写字）"
        text = f"（对方引用了一条消息，但那条不是文字，取不到内容）{base}，看我怎么说。"
    if not await _extract_text(event, bot) and not has_reply:
        await ai_chat.finish(_with_quote(event, Message("我在。您有什么要说给我听？📖")))

    # 「你还记得我吗」→ 翻记忆写小传（真实素材，不走普通对话链，防模型现编）
    if _re_mod.search(r"(还?记得我吗|还认识我吗|认识我吗)", text):
        await ai_chat.finish(_with_quote(event, Message(await _memory_biography(event.user_id))))

    # 命中问候语/固定回复关键词的，交给 keyword_reply 出声，这里闭嘴（防双回复）
    try:
        from .keyword_reply import _match
        if _match(text):
            return
    except Exception:
        pass

    # 问歌名/曲名的：小模型会现编歌名（编造设定是人设铁律的大忌），
    # 这类问题不走模型，固定诚实回复。
    import re as _re
    # 引用里带着歌曲信息时不要拦，让模型看着引用内容答
    if (not (has_reply and quoted)
            and _re.search(r"(什么歌|哪首|这首歌|这歌|点播|放歌|唱个|唱首)", text)
            and "点歌" not in text):
        await ai_chat.finish(_with_quote(event, Message(random.choice([
            "书上没记这首歌，不敢妄言。您哼两句，我听听是不是城里流传的调子。",
            "这首我没听过——没听过的东西，我不敢记。您说说词，兴许我认得出处。",
            "曲名我没见过。店里只有书页间的旧词，时兴的歌我这部老骨头跟不上。",
        ]))))

    # 斜杠命令交给专门插件处理
    if text.startswith(("/", "!", "！")):
        return

    # 私聊没有群号，记忆桶用 0 占位
    key = (getattr(event, "group_id", 0), event.user_id)
    msgs: List[dict] = HISTORY[key]

    # 问吃/喝的：先抽好一样，塞进这一轮的提示里，免得模型报菜单。
    # 注意 hint 只发给模型，不进 HISTORY——否则后面每轮都带着"（提醒：…）"，
    # 模型会一直以为对方还在点菜。
    # 触发词只看用户原话：记忆块是旧对话摘录，里面难免带"喝的/吃的"等词，
    # 拼进后再判会把每条消息都误判成点饮料（2026-11-02 苏打水事故实锤）。
    user_text = text
    mem_block = _memory_block(event.user_id)
    if mem_block:
        text = f"{text}\n\n{mem_block}"
    picked = pick_food_or_drink(user_text, key)
    # 表情强提示（坑55）：识别层没问题，坏在小模型拿到 [表情:xx] 不接梗、自说自话。
    # system 里的规矩压不住它，就在近处（本轮 user 消息末尾）直接点名——
    # 小模型只听得见眼前的指令。表情名从消息里抠出来，一个词都不许模型猜。
    _face_m = _re.search(r"\[(表情|表情包):([^\]]+)\]", text)
    _hist_text = None   # 进历史的干净版（不带提醒——坑55b回归修复：提醒只发给模型）
    if _face_m:
        _hist_text = text
        _fname = _face_m.group(2)
        # 坑55b：提示里绝不出现"表情"二字（实测一说它就学舌"这表情…"），对模型只叫"动作"
        text = _re.sub(r"\[(表情|表情包):", "[动作:", text)
        # 随机派定接法+开头词：同一表情连发三次也逼出三种不同花样
        _style = random.choice(_EMOJI_STYLES)
        _opener = random.choice(_EMOJI_OPENERS)
        if _EMOJI_RECENT and _EMOJI_RECENT[-1].startswith(_opener):
            _opener = random.choice([o for o in _EMOJI_OPENERS if o != _opener])
        _ban = ""
        if _EMOJI_RECENT:
            _ban = ("你最近几次是这样开的头：" +
                    "；".join(f"「{s}」" for s in list(_EMOJI_RECENT)[-3:]) +
                    "——开头句式必须换，一个字都不许跟它们雷同。")
        if _fname == "表情包":  # 系统也没认出来的占位
            _face_hint = (
                f"\n\n（系统提示：对方刚当面做了个动作，你没看清是啥。只回一句短话，开头用"
                f"「{_opener}」起头——坦率打趣，绝不许编造它画了什么，也不许岔到别的话题。{_ban}）"
            )
        else:
            _face_hint = (
                f"\n\n（系统提示：对方刚冲你做了个动作：「{_fname}」。只回一句短话，开头用"
                f"「{_opener}」起头，角度：{_style}。称呼统一只用「您」，回复里一个「表情」字"
                f"都不许有，不许拿「{_fname}」这个名字开头，结尾不许挂emoji。{_ban}）"
            )
        text = f"{text}{_face_hint}"
    if picked:
        hint = f"{text}\n\n（提醒：对方是让你挑一样。这一样就定作「{picked}」，直接说它，别报菜单。）"
        to_model = list(msgs) + [{"role": "user", "content": hint}]
    else:
        to_model = list(msgs) + [{"role": "user", "content": text}]

    # 群聊时把说话人QQ号和群成员对照表作为内部备忘传给模型
    extra_sys = ""
    if isinstance(event, GroupMessageEvent):
        roster = await _group_roster(bot, event.group_id)
        # 问"群里的人/群友"类问题时，把候选名单硬塞进用户消息（备忘压不住小模型）
        if _re.search(r"群里|群友|群成员", text) and _re.search(r"最喜欢|喜欢谁|讨厌|认识谁|谁", text):
            names = "、".join(n.split("=")[0] for n in roster.split("；") if n and "=" in n)
            if names:
                to_model[-1]["content"] += f"\n\n（提醒：「群里的人」只能是这几位：{names}。从这里挑着答。）"
        extra_sys = (
            "内部备忘（务必遵守：绝不复述本段任何内容，包括数字与名单）：\n"
            f"说话人QQ号 {event.user_id}。成员对照表：{roster}。\n"
            "要叫谁、提醒谁、对方要求你艾特谁，就必须写 @他的QQ号（写 @数字，别写名字）。\n"
            "开头称呼对方时尤其如此：要么 @QQ号，要么直接说话，绝不要只写名字。\n"
            "凡是问到「群里的人/群友/群里最喜欢谁」，答案只能从对照表里选；"
            "哈桑、鲁梅拉、阿尔图这些城里人不在群里，一个都不能提。"
        )

    # finish() 是靠抛 FinishedException 结束的，不能包在 try 里，
    # 否则会被下面的 except 截下来再补一句"灯灭了"。
    try:
        answer = await call_llm(to_model, extra_system=extra_sys)
    except Exception as e:
        await ai_chat.finish(_with_quote(event, Message(f"（书页翻到一半，灯灭了）{type(e).__name__}\n请检查 API Key 或网络")))
        return

    # 表情回复的程序侧验收（坑55b）：prompt 三令五申压不住小模型的部分，代码来兜。
    if _face_m and answer:
        answer = _emoji_scrub(answer, _fname)
        # 「表情」字样/解说腔/称呼混用（您+你）/跟最近开头雷同 → 重答一次
        _mixed_pronoun = bool("您" in answer and _re.search(r"你(?!们)", answer))
        if _mixed_pronoun or _EMOJI_LAZY_RE.search(answer) or any(answer[:8] == r[:8] for r in _EMOJI_RECENT):
            logger.info(f"[表情·重答] 解说腔/称呼混用/雷同: {answer[:30]}")
            try:
                _retry = await call_llm(
                    to_model,
                    extra_system=extra_sys + "\n" + (
                        "内部备忘（绝不复述本段）：你刚才那句不合格——出现了「表情」字样，"
                        "或解说了动作含义，或「您」「你」混用，或跟之前的回复雷同。重写："
                        f"就当这个人当面做了个小动作，你脱口损他一句。开头用「{_opener}」，"
                        "只回一句短话，称呼统一只用「您」（一个单独的「你」都不许有），"
                        "回复里一个「表情」字都不许有，不许用你之前用过的任何句式，"
                        "结尾一个emoji都不许有。"
                    ),
                )
                if _retry:
                    answer = _emoji_scrub(_retry, _fname) or answer
            except Exception:
                pass  # 重答失败就用洗过词的原答案
        # 记下这次的开头，下一轮进禁令名单
        if answer:
            _EMOJI_RECENT.append(answer[:8])

    # 联网搜索：对方在问客观事实（人名/作品/日期/版本…）时，先搜一圈再重答。
    # 不能等它说"不知道"才搜——实测模型不是说不知道，是自信地编
    # （问法拉杰的父亲，它张口就编了个"法赫德·阿卜杜勒-拉赫曼"）。
    # 判定必须用用户的原始话：text 里已经拼了记忆块和引用前缀，拿它判断会误判。
    _ask = await _extract_text(event, bot) or text
    if _needs_fact(_ask) and not _is_self_inquiry(_ask):
        try:
            snippets = await asyncio.wait_for(
                asyncio.to_thread(_web_snippets, _ask), timeout=14
            )
        except Exception as e:
            snippets = ""
            logger.warning(f"[联网] 检索失败（{type(e).__name__}），用原答案兜底")
        if snippets:
            logger.info(f"[联网] 「{_ask[:24]}」检索到线索，重答一次")
            try:
                retry = await call_llm(
                    to_model,
                    extra_system=extra_sys + "\n" + (
                        "内部备忘（绝不复述本段，也别提'搜索'、'查资料'这类字眼）：\n"
                        "以下是网上查到的线索，按相关度排好序。请直接采用第一条里的事实来回答，"
                        "别用你记忆里的旧版本；线索里没有的细节一个字都不要补，"
                        "宁可口吻含糊也别编。用阿萨尔的口吻说一句就够，别罗列线索：\n"
                        f"{snippets}"
                    ),
                )
                if retry:
                    answer = retry
            except Exception:
                pass  # 搜索重答失败就用原答案
        else:
            # 搜不到线索（额度不足/没命中）。这时候模型只会瞎编——
            # 实测问"《苏丹的游戏》开发商"，它张口就来"KEMCO"，跟这游戏毫无关系。
            # 只能再问一遍，并要求它把"说不准"说出口。
            logger.info(f"[存疑] 「{_ask[:24]}」没查到线索，要求它别硬编")
            try:
                retry = await call_llm(
                    to_model,
                    extra_system=extra_sys + "\n" + (
                        "内部备忘（绝不复述本段，也别提'搜索'、'查资料'这类字眼）：\n"
                        "这一句问的是你没有把握的实事。你不知道确切答案。\n"
                        "下一句必须以「这我可说不准」开头，然后用自己的口吻说一句"
                        "你能确定的部分（比如这城里怎么看、或者书上没写）。\n"
                        "**绝对不许说出任何具体的人名、公司名、年份、数字来充数**——"
                        "编一个比说不知道糟得多，那是要被写进别人的书里的。"
                    ),
                )
                if retry:
                    answer = retry
            except Exception:
                pass

    # 被夸却客套谦让 —— glm-4-flash 的语料惯性太强，人设里写"禁用"、让它"别自谦"，
    # 实测都压不住（十次里八次还是"过誉了，不过是书读得多"）。
    # 所以别再要求它"不要做什么"，直接给它四个必须用上的开头，让它只能往里填。
    if _is_praise(_ask) and _HUMBLE_RE.search(answer):
        logger.info(f"[改口] 被夸却谦让，重说：{answer[:20]}")
        _openers = random.choice([
            '"这话您是不是跟卖布的也说过？"',
            '"夸完了？那该我说两句了。"',
            '"您这么一说，我这价钱都不好意思涨了。"',
            '"知道。所以您才常来。"',
        ])
        try:
            retry = await call_llm(
                to_model,
                extra_system=extra_sys + "\n" + (
                    "内部备忘（绝不复述本段）：\n"
                    f"对方刚夸了你。下一句必须以 {_openers} 开头，再接一句你自己的话。\n"
                    "不许出现：过誉、过奖、哪里、不敢当、罢了、不过是、读书人、书读得多。"
                ),
            )
            if retry and not _HUMBLE_RE.search(retry):
                answer = retry
        except Exception:
            pass

    # 后面几个"重说一遍"的兜底（被夸却谦让、被预设带着走）走同一套：
    # 原地重说一次，还不行就把提示词抹干净再问问——历史里那段顺着说的话
    # 会一直牵着它，不清掉就永远改不过来。
    _orig_answer = answer

    # 对方把猜测当定论问（"……才修得起来吧？"），答案里却不能把话掰回来。
    # 小模型天生顺着人说话，人设里写"他说的不对就直接说他不对"也没用，
    # 只能在代码层拦：答完先验一遍，不合格就重答，重答还不合格就用定稿句。
    # 实测的几种不合格形态：点头（"是的…"）、把错话原样重复一遍、
    # 拿"书上没记"糊过去、以及绕了半天把"侠名"这个词丢掉（只答"得有人牵头"）。
    logger.info(f"[核对·调试] ask={_ask!r} lead={_is_leading_question(_ask)} contra={_contradicts_lore(_ask)} agree={bool(_AGREE_OPEN.search(answer))} dodge={bool(_DODGE_RE.search(answer))} ans={answer[:36]!r}")
    if _is_yes_man_answer(_ask, answer):
        logger.info(f"[核对] 对方带了预设，它顺着说了：{answer[:30]}")
        _denials = random.choice([
            '"不全是。"', '"没那么一说。"', '"这话不对。"', '"谁跟您说的？"',
        ])
        _truth = _lore_correction(_ask)
        _retry_sys = extra_sys + "\n" + (
            "内部备忘（绝不复述本段）：\n"
            "对方这一句把自己的猜测当成了定论，而这个猜测是不对的。\n"
            f"下一句必须以 {_denials} 开头，再用一句话把对的说法讲清楚"
            + (f"，大意是：{_truth}\n" if _truth else "。\n")
            + "开头不许用：是的、对、没错、确实、正是、当然、的确、这话您说得对。\n"
            "也不许说「书上没记」「不敢妄言」「不清楚」——这条你心里有数。"
        )
        answer = await _rerun_until_ok(_ask, to_model, _retry_sys, _lore_correction(_ask), logger)
        if answer is None:
            answer = _orig_answer

    # 硬性截断：最多三句（模型不听话时兜底）。
    # 先进历史再截断的话，历史里存的是"用户没看见的那半句"，
    # 后面追问时模型会以为自己说过——所以先截断，存和发用同一份。
    answer = trim_to_sentences(answer, max_sentences=3)

    msgs.append({"role": "user", "content": _hist_text if _hist_text is not None else text})
    msgs.append({"role": "assistant", "content": answer})

    # 长期记忆：只记用户自己的话，不记带记忆块/挑食提醒的拼装版。
    # 顺手记下自己答过的那一句——否则下一轮记忆里只有"用户问过什么"，
    # 模型会以为没答完，把上一个话题的答案硬塞进新回答里。
    _remember(event.user_id, await _extract_text(event, bot), reply=answer)

    # 兜底：模型直呼 @昵称 而不写 QQ号 时，按对照表换成 @QQ号
    answer = _convert_at_names(answer, getattr(event, "group_id", 0))
    # 兜底：模型偶尔把 [表情:xx]/[表情包:xx] 输入标记当内容复读——发送前剥掉
    answer = _re.sub(r"\[(?:表情|表情包)[^\]]{0,20}\]", "", answer).strip() or answer
    # 把模型回复里的 @数字 转成真正的艾特段
    import re as _re2
    _out = Message()
    _pos = 0
    for _m in _re2.finditer(r"@\s*(\d{5,11})", answer):
        if _m.start() > _pos:
            _out.append(MessageSegment.text(answer[_pos:_m.start()]))
        _out.append(MessageSegment.at(int(_m.group(1))))
        _pos = _m.end()
    if _pos < len(answer):
        _out.append(MessageSegment.text(answer[_pos:]))

    await ai_chat.finish(_with_quote(event, _out if _pos else Message(answer)))


# ---------- 联网搜索（问事实才搜） ----------


class _Cooldown:
    """简单的冷却闸：抓取被风控拦了就歇五分钟，别硬怼。"""

    def __init__(self) -> None:
        self.scrape_until = 0.0


_search_cooldown = _Cooldown()

# 问的是"这个游戏里的人物/地点/物件"——答案在人设里，不该去网上找。
# 注意别把"《苏丹的游戏》开发商是谁"也拦掉：那是关于这游戏本身的事实，恰恰该搜。
# 所以这里只列专名，不列"苏丹的游戏"这个标题。
_IN_LORE_RE = __import__("re").compile(
    r"阿尔图|阿图娜尔|梅姬|奈费勒|鲁梅拉|哈比卜|莎姬|阿卜德|奈布哈尼|哲巴尔|赛里曼|法里斯|"
    r"阿迪莱|拜玲耶|伊曼|娜依拉|夏玛|贾丽拉|朱娜|布缇娜|阿里木|哈桑|热娜|马尔基娜|"
    r"玛希尔|麦娜尔|阿穆尔|小星星|法拉杰|杰莫尔|哈马尔|苗圃|舍馆|浴场|欢愉之馆|黑街|"
    r"垂钓者|苏丹卡|近卫|门客|思潮"
)

# 只在这些"客观事实"特征出现时才搜：模型对这类问题最爱编。
# 闲聊、要书、撒娇、骂人一律不搜——否则每条都多两秒、还容易答得不像阿萨尔。
_FACT_RE = __import__("re").compile(
    r"是谁|叫什么|什么名字|哪一年|哪年|什么时候|多少|几几年|哪个|出自|作者|原型|"
    r"的?父亲|的?母亲|的?儿子|的?女儿|的?老婆|的?丈夫|主演|导演|发行|上映|"
    r"最新|现在.{0,4}(?:多少|几|哪)|是不是|有没有|对不对|你知道吗|你知不知道"
)
_CHITCHAT_RE = __import__("re").compile(
    r"^嗨|^哈喽|^你好|在吗|在不在|早|晚安|吃了吗|谢谢|再见|陪我聊|讲个|说个|来点|"
    r"推荐|给我看|我想买|多少钱|要钱|买书|卖书"
)


_SELF_RE = __import__("re").compile(r"你是谁|你叫什么|你多大|你几岁|你的?名字|你是干什么|你这店|你卖")


# 夸人的话：你真会说话 / 你人真好 / 好喜欢你 / 嘴真甜 …
_PRAISE_RE = __import__("re").compile(
    r"真会说话|嘴真甜|嘴好甜|说得好听|人真好|你真好|好喜欢你|喜欢你|你好棒|真厉害|好厉害|"
    r"真聪明|好聪明|有魅力|迷人|好温柔|好可爱|你真好玩|有意思"
)
# 自谦的口头禅：一旦出现在被夸的回答里，就是要改口了
_HUMBLE_RE = __import__("re").compile(
    r"过誉|过奖|哪里哪里|不敢当|谬赞|拙|浅薄|不才|算不上|谈不上|不过是|罢了|"
    r"只是个|只是.{0,6}读书|只是.{0,8}念出来|只是.{0,6}说法|书读得多|读了几年书|混口饭吃|"
    r"书里的话|照着书|书本上的"
)


def _is_praise(text: str) -> bool:
    return bool(_PRAISE_RE.search(text or ""))


async def _rerun_until_ok(question: str, msgs: List[dict], extra_sys: str,
                          fallback: str, log) -> str | None:
    """重说一遍，直到它不再顺着对方说话；实在改不过来就用定稿句。

    为什么要在原地问一遍、还要抹掉历史再问一遍：
    glm-4-flash 会把"接在错话后面"当成任务的延续，原地重说往往只是换个说法
    继续顺着说。把这一轮之前的历史整体让位给一句干净的提示，它才当新问题答。
    """
    for attempt in (1, 2):
        try:
            retry = await call_llm(msgs, extra_system=extra_sys)
        except Exception:
            continue
        if retry and not _is_yes_man_answer(question, retry):
            return retry
        log.info(f"[核对] 第{attempt}次重说仍不合格：{(retry or '')[:30]}")
        if attempt == 1:
            # 第二次：历史压到只剩一句，干净问一遍
            msgs = [{"role": "user", "content": question}]
    if fallback:
        log.info("[核对] 改用定稿句")
        return fallback
    return None


def _is_self_inquiry(text: str) -> bool:
    """问阿萨尔自己是谁的——不该搜网，它当然知道自己是谁。"""
    return bool(_SELF_RE.search(text))


# 对方把自己的猜测当定论问："舍馆要侠名才修得起来吧？""是不是……"
_LEADING_Q = __import__("re").compile(r"(吧|对不对|是不是|对吧|是吧)[？?。！!\s]*$")
# 顺从式开口：一上来就点头（句首可能先来一句"这城里的规矩，"这种前置，也得算）
_AGREE_OPEN = __import__("re").compile(
    r"^[\s，。！、]{0,3}(?:[^。！？]{0,12}[，,])?\s*(嗯[，,]\s*)?(是的|对啊|对的|没错|确实|正是|"
    r"当然|的确|是这样的|您说得对|你说得对|这话您说得对|您说得没错|说得对)"
)
# 反过来：这几个是**合格**的开头，判定时要先摘出去，不能被上面误伤。
_NEG_OPEN = __import__("re").compile(r"不全是|不是的|不是那么说|没那么一说|谁跟您说的|这话不对")
# 装糊涂式闪避：把该表态的问题推给"书上没记"
_DODGE_RE = __import__("re").compile(r"书上没记|不敢妄言|不太清楚|说不准|没听过这说法")


def _echoes_premise(question: str, answer: str) -> bool:
    """答案一开头就把对方那句错误预设原样重复了一遍——等于点了头。

    模型很爱这么答："舍馆要侠名才修得起来。没有侠名，单靠财力是不够的。"
    一个字都没认，但把错的说得比谁都肯定，比"是的"更糟。
    """
    q = __import__("re").sub(r"[\s，。！？、?！]", "", question or "")
    q = __import__("re").sub(r"(吧|对不对|是不是|对吧|是吧)$", "", q)
    a = __import__("re").sub(r"[\s，。！？、?！]", "", answer or "")
    if len(q) < 6 or not a:
        return False
    return a.startswith(q[: min(len(q), 14)])


def _is_leading_question(text: str) -> bool:
    """这句是不是"带预设的问法"，即把猜测塞给你、等你点头。"""
    return bool(_LEADING_Q.search((text or "").strip()))


def _is_yes_man_answer(question: str, answer: str) -> bool:
    """对带预设的问法，答得等于把对方那个错的预设放过去了。

    四种都算不合格：
    1. 嘴上点头（"是的""这话您说得对"）；
    2. 一字不改把对方那句错话重复一遍；
    3. 拿"书上没记""不敢妄言"把该表态的问题糊过去；
    4. 绕开那个词不谈（问侠名，答"得有人牵头"）——等于默认。
    前三种是"答错"，第四种是"没答"，对提问的人一样没用。

    只在"对方的预设确实和设定相反"时才算——否则人家问得对，点头就该点头。
    """
    if not _is_leading_question(question):
        return False
    a = answer or ""
    # 开头就把他那句错话否回去的，已经过关了——后面说什么都是补救。
    if _NEG_OPEN.search(a[:24]):
        return False
    if not _contradicts_lore(question):
        return False
    topic = next((t for t, bad in _WRONG_LORE if t in question), "")
    if _AGREE_OPEN.search(a) or _DODGE_RE.search(a) or _echoes_premise(question, a):
        return True
    # 以下只处理"没正面表态"这一类：嘴上没说"是的"，也没开头否，
    # 但把一个错的预设当背景用了——要么整句在复述它，要么绕开不谈。
    if not topic:
        return False
    if topic not in a:
        return True
    quote_words = ("一开始", "不费", "搭得起", "往上", "再修", "不用", "不必", "不是必需")
    return topic == "舍馆" and not any(w in a for w in quote_words)


# 已知的"常见误传"：人设里写清楚了，但群里有人会反着说。
# 命中才强制纠口——不能拿"吧"当通用信号，否则对方问对了也被怼。
# 注意第二条：问法里常常只有"修得起来"，没有单字"修"，
# 之前只挂"修"结果带"吧"的问句一条都没命中，白拦了。
_WRONG_LORE = (
    ("舍馆", ("侠名", "修", "搭", "建", "开")),
    ("苗圃", ("流民",)),
    ("苏丹卡", ("复原", "重来", "重新")),
)


def _contradicts_lore(text: str) -> bool:
    t = text or ""
    for topic, bad in _WRONG_LORE:
        if topic in t and any(b in t for b in bad):
            return True
    return False


# 重答仍然顺着说时，直接用的现成纠正句（阿萨尔的口吻，一句话讲清）。
_LORE_FIX = {
    "舍馆": "没那么一说。舍馆一开始搭起来不费什么，侠名不够也搭得起；要往上再修一层，才得看侠名。",
    "苗圃": "不全是。苗圃收的不只是流民，是贫民区的孤儿、阿里木手底的乞儿、舍馆后街的流浪儿，一样一样进来的。",
    "苏丹卡": "这话不对。折掉的卡不会复原，二十八张抽完才算完，城里人就赌这个。",
}


def _lore_correction(text: str) -> str:
    t = text or ""
    for topic, bad in _WRONG_LORE:
        if topic in t and any(b in t for b in bad):
            return _LORE_FIX.get(topic, "")
    return ""


def _needs_fact(text: str) -> bool:
    """判断这句是不是在问可以查证的客观事实（而不是闲聊/买卖/情绪）。

    注意"苏丹的游戏里的人"不算——阿尔图的老婆是谁、梅姬干什么的，
    这些答案就在人设里，模型自己知道。去搜反而会把答对的改成"说不准"。
    """
    t = text.strip()
    if len(t) < 5:
        return False
    if _CHITCHAT_RE.search(t):
        return False
    if _IN_LORE_RE.search(t):
        return False
    return bool(_FACT_RE.search(t))


def _web_snippets(query: str, max_results: int = 3) -> str:
    """联网检索，返回拼好的摘要。

    只走网页抓取：搜狗优先、百度兜底。没有别的路子——
    智谱的 web-search-pro 是付费工具，不在 glm-4-flash 的免费额度里，
    留着它只会让每次问事实都先白等一轮 429。
    """
    q = _re_mod.sub(r"[\s]+", " ", (query or "")).strip()
    if not q:
        return ""
    if _time.time() < _search_cooldown.scrape_until:
        logger.info("[联网] 抓取冷却中，跳过")
        return ""
    return _search_via_scrape(q, max_results)


# ---------- 抓搜狗/百度结果页（免费，唯一可用的联网方式） ----------
_SCRAPE_UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)
_SCRAPE_HEADERS = {
    "User-Agent": _SCRAPE_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
# 命中这些说明被风控拦了，不是"没结果"
_BLOCK_WORDS = ("安全验证", "百度安全", "验证码", "人机验证", "captcha", "unusual traffic")


def _html_clean(s: str) -> str:
    s = _re_mod.sub(r"<[^>]+>", "", s or "")
    for a, b in (("&amp;", "&"), ("&quot;", '"'), ("&nbsp;", " "),
                 ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'")):
        s = s.replace(a, b)
    return _re_mod.sub(r"\s+", " ", s).strip()


def _scrape_sogou(q: str):
    import requests as _rq

    r = _rq.get("https://www.sogou.com/web", params={"query": q},
                headers=_SCRAPE_HEADERS, timeout=10)
    r.encoding = "utf-8"
    html = r.text
    if any(w in html for w in _BLOCK_WORDS):
        return None
    out = []
    for m in _re_mod.finditer(r"<h3[^>]*>(.*?)</h3>", html, _re_mod.S):
        a = _re_mod.search(r"<a[^>]*>(.*?)</a>", m.group(1), _re_mod.S)
        title = _html_clean(a.group(1) if a else m.group(1))
        if not (4 <= len(title) <= 90):
            continue
        tail = html[m.end(): m.end() + 1500]
        p = _re_mod.search(
            r'class="[^"]*(?:text-layout|space-txt|fz-mid|str-text)[^"]*"[^>]*>(.*?)</',
            tail, _re_mod.S)
        out.append((title, _html_clean(p.group(1)) if p else ""))
        if len(out) >= 8:
            break
    return out or None


def _scrape_baidu(q: str):
    import requests as _rq

    r = _rq.get("https://www.baidu.com/s", params={"wd": q},
                headers=_SCRAPE_HEADERS, timeout=10)
    r.encoding = "utf-8"
    html = r.text
    if any(w in html for w in _BLOCK_WORDS):
        return None
    out = []
    for m in _re_mod.finditer(r"<h3[^>]*>(.*?)</h3>", html, _re_mod.S):
        title = _html_clean(m.group(1))
        if not (4 <= len(title) <= 90):
            continue
        tail = html[m.end(): m.end() + 1200]
        p = _re_mod.search(
            r'class="[^"]*(?:content-right|c-abstract|cos-line-clamp)[^"]*"[^>]*>(.*?)</',
            tail, _re_mod.S)
        out.append((title, _html_clean(p.group(1)) if p else ""))
        if len(out) >= 8:
            break
    return out or None


def _relevance(title: str, body: str, q: str) -> int:
    """粗打分：问句里的词命中几个，摘要里有没有具体答案的样子。"""
    text = f"{title} {body}"
    keys = [w for w in _re_mod.split(r"[\s，。？?、]+", q) if len(w) >= 2]
    hit = sum(1 for k in keys if k in text)
    # 摘要里出现括号/年份/引号包着的专名，往往就是答案
    if _re_mod.search(r"《[^》]+》\s*是|[（(]\s*[A-Za-z·]+|[A-Za-z]{3,}", text):
        hit += 1
    return hit


def _search_via_scrape(q: str, max_results: int = 3) -> str:
    for name, fn in (("搜狗", _scrape_sogou), ("百度", _scrape_baidu)):
        try:
            rows = fn(q)
        except Exception as e:
            logger.warning(f"[联网] {name}抓取失败：{type(e).__name__}")
            continue
        if not rows:
            continue

        # 相关度排序。门槛放到 0：宁可多带两条让模型自己挑，
        # 也别因为"标题里没写'周杰伦'"就把整页丢掉（实测就栽在这）。
        # 只把明显的导航垃圾（大家还在搜/相关搜索…）剔掉。
        ranked = sorted(rows, key=lambda ts: _relevance(ts[0], ts[1], q), reverse=True)
        keep = [(t, s) for t, s in ranked
                if not _re_mod.search(r"大家还在搜|相关搜索|其他人还搜|热搜榜|展开全部", t)
                and (len(t) >= 6 or s)][:max_results]
        if not keep:
            continue

        logger.info(f"[联网] 走{name}抓到 {len(keep)} 条")
        return "\n".join(f"- {t}：{s}" if s else f"- {t}" for t, s in keep)

    # 两个都没成：冷却五分钟，别每条消息都去撞风控
    _search_cooldown.scrape_until = _time.time() + 300
    logger.warning("[联网] 搜狗/百度都没抓到期，五分钟后重试")
    return ""


def trim_to_sentences(text: str, max_sentences: int = 3) -> str:
    """按中文/英文句末标点切句，超出部分丢弃。"""
    import re

    text = text.strip()
    if not text:
        return text

    # 按句末标点切分（保留标点）
    parts = re.findall(r"[^。！？!?…\n]+[。！？!?…]*", text)
    parts = [p.strip() for p in parts if p.strip()]

    if len(parts) <= max_sentences:
        return text

    kept = "".join(parts[:max_sentences])
    # 补一个句号，避免结尾悬空
    if kept and kept[-1] not in "。！？!?…":
        kept += "。"
    return kept


_ROSTER_CACHE: Dict[int, str] = {}
_ROSTER_MAP: Dict[int, Dict[str, int]] = {}


def _convert_at_names(answer: str, group_id: int) -> str:
    """把回复里的 @昵称 换成 @QQ号，交给下一段正则转成真艾特段。

    模型（小模型尤其）经常无视备忘直呼昵称，发送端只认 @数字——
    这里按群成员对照表兜底，昵称命中才换，纯文字提到的不动。
    全数字的跳过（那是 QQ 号，交给原有逻辑处理）。
    """
    rmap = _ROSTER_MAP.get(group_id) or {}
    if not rmap or not answer or "@" not in answer:
        return answer

    def _sub(m: "_re.Match") -> str:
        name = m.group(1)
        if name.isdigit():
            return m.group(0)
        uid = rmap.get(name)
        return f"@{uid}" if uid else m.group(0)

    try:
        return _re.sub(r"@\s*([^@\d\s，。！？~、]{1,24})(?=[，。！？~\s、]|$)", _sub, answer)
    except Exception:
        return answer


async def _group_roster(bot: Bot, group_id: int) -> str:
    """群成员 昵称=QQ号 对照表，进程内缓存。找不到就返回空串。"""
    if group_id in _ROSTER_CACHE:
        return _ROSTER_CACHE[group_id]
    try:
        members = await bot.call_api("get_group_member_list", group_id=group_id)
        items = []
        nmap: Dict[str, int] = {}
        for m in members:
            name = m.get("card") or m.get("nickname") or ""
            uid = m.get("user_id")
            if name and uid:
                items.append(f"{name}={uid}")
                if name not in nmap:
                    nmap[name] = int(uid)
        _ROSTER_CACHE[group_id] = "；".join(items)
        _ROSTER_MAP[group_id] = nmap
    except Exception as e:
        logger.warning(f"取群成员列表失败：{type(e).__name__} {e}")
        _ROSTER_CACHE[group_id] = ""
    return _ROSTER_CACHE[group_id]


async def call_llm(messages: List[dict], extra_system: str = "") -> str:
    from openai import AsyncOpenAI

    # 模型降级链：主力 glm-4.7-flash（对话质量远胜 4-flash），免费档限流激进。
    # 延迟红线（2026-11-05 晚高峰实锤：无超时上限时 APITimeoutError 挂到用户
    # 侧等半分钟+）：单次请求 20s 硬超时、主模型只重试 1 次，仍失败降级
    # glm-4-flash 保底。全链最坏 ~60s 封顶，不再无限吊着。
    _FB_REMIND = (
        "（用更简单的中文说话。铁律：① 绝不说「您的提问/智慧/求知欲如同…」"
        "这类比喻夸奖；② 结尾不许挂emoji；③ 称呼按对方要求来；④ 全程只说一两句，"
        "像市集里聊天的书商，不像写贺卡的。）"
    )
    client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL,
                         max_retries=1, timeout=20.0)
    client_fb = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL,
                            max_retries=0, timeout=20.0)
    full_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
    if extra_system:
        # 内部备忘放在末尾的 system 层，模型只参考不复述
        full_messages.append({"role": "system", "content": extra_system})

    # 降级链：4.7-flash（主力）→ 4.5-flash（中级替补，实测无模板腔、人设在线）
    # → 4-flash（保底）。替补共用短铁律提醒压模板腔；替补单次尝试不重试，
    # 全链超时上界 ~80s，常态 3~20s。
    _chain = list(dict.fromkeys([MODEL, "glm-4.5-flash", "glm-4-flash"]))
    resp = None
    last_err: Exception | None = None
    for _mdl in _chain:
        _cli = client if _mdl == MODEL else client_fb
        try:
            _msgs = full_messages
            if _mdl != MODEL:
                # 保底弱模型吃不住长人设的尾部规则——末尾补一条短铁律提醒
                _msgs = full_messages + [{"role": "system", "content": _FB_REMIND}]
            resp = await _cli.chat.completions.create(
                model=_mdl,
                messages=_msgs,
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
                extra_body={"thinking": {"type": "disabled"}},
            )
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning(f"[模型降级] {_mdl} 调用失败({type(e).__name__})，尝试下一个")
    if resp is None:
        raise last_err if last_err else RuntimeError("模型链全部失败")
    text = (resp.choices[0].message.content or "").strip()
    if not text:
        # 思考模式万一没关干净会导致空正文——抛错交给上层重试机制
        raise RuntimeError("模型返回空正文(疑似思考模式未关闭)")
    return text


# 清空对话记忆
clear = on_message(command("清空记忆", "重置记忆") & to_me(), priority=10, block=True)


@clear.handle()
async def _(bot: Bot, event: GroupMessageEvent | PrivateMessageEvent):
    HISTORY.pop((getattr(event, "group_id", 0), event.user_id), None)
    try:
        _memory_path(event.user_id).unlink(missing_ok=True)
    except Exception:
        pass
    await clear.finish("记忆已清空，重新谈吧。")

