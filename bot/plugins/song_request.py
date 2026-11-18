"""
点歌

群里发 /点歌 晴天  → 机器人找到歌，发语音 / 文件 / 分享卡片

音源说明
---------
网易云的 free 外链早在 2021 年就关了，官方 openapi 又要会员。
这里走第三方聚合接口拿音频流，属于"能响就行"的方案：
- 单曲可能只返回 45 秒试听片段（版权方限制），也可能给整首
- 接口本身是公益性质的，随时可能失效

发送策略（2026-11-11 v4，见手册坑80续）：
1. 默认"语音 + 音乐卡片"双发，卡片双源择优：
   - 语音条：网易云免费源给的完整版（网易云 VIP 的歌早被过滤）
   - 卡片：网易云这首免费 → 发网易云卡片（点开去网易云 App 听完整
     版，后台不断）；网易云 VIP 但 QQ 音乐免费 → 发 QQ 音乐卡片；
     两边都 VIP → 不发假承诺，如实告知
2. 卡片发 QQ 音乐版时，优先挑 QQ 音乐里免费的那条结果
3. 语音失败 → 自动改传群文件；歌超过 600s 或用户加了"文件"后缀 →
   直接传文件
4. 长度红线从 300s 放宽到 600s：voice_probe.log 里 7 次成功都在
   300s 以内纯属"代码没敢试"，11.7MB 都发出去过——先试再说，
   probe 会记下真实上限在哪
"""
import re
import ast
import time
from datetime import datetime
from pathlib import Path

import aiohttp
from nonebot.log import logger
from nonebot import on_command, on_message
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
# 酷我音乐（2026-11-15 实测全链路可用）：r.s 搜索返回单引号 JS 对象
# （ast.literal_eval 解析），antiserver 直接给 mp3 直链，无需任何 key。
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
KW_SEARCH = "http://search.kuwo.cn/r.s"
KW_STREAM = "http://antiserver.kuwo.cn/anti.s"

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

    # 坑114：平台后缀——"点歌 X 酷我/kuwo"、"点歌 X 酷狗"、"点歌 X 咪咕" 去对应平台。
    # 坑124方案（11-18）：酷狗/咪咕从"拒绝"改为"接入"——关键点：官方卡只是跳转链接
    # 不需要本地音频，所以酷狗"音频要签名"、咪咕"搜索接口死"都不再是阻碍。做法：
    # 复用网易云搜索拿歌名歌手（这两家没可用搜索接口），把歌名塞进条目 id，
    # 签名代理 v4 收到 music("kugou"/"migu", 歌名) 后用 OIAPI 出对应标真卡。
    src = "netease"
    m = re.search(r"\s+(酷我|kuwo|qq|qq音乐|QQ音乐|酷狗|kugou|咪咕|migu)$", kw, re.I)
    if m:
        tag = m.group(1).lower()
        kw = kw[: m.start()].strip()
        if tag in ("酷我", "kuwo"):
            src = "kuwo"
        elif tag.startswith("qq"):
            src = "qq"
        elif "酷狗" in tag or tag == "kugou":
            src = "kugou"
        else:
            src = "migu"

    if not kw:
        await song_cmd.finish("想听什么？说个歌名、歌手名都行；后面加个'文件'就发文件版（能后台听）。")

    await song_cmd.send(f"翻柜子找「{kw}」，稍等一下……")

    # 坑114：按平台后缀分发搜索；song 条目统一带 src 字段（netease/qq/kuwo/kugou/migu）
    songs = await search(kw) if src == "netease" else (
        await search_kuwo(kw) if src == "kuwo" else await search_qq(kw))
    # 坑124方案：酷狗/咪咕没有可用搜索接口，复用网易云搜索结果拿"歌名-歌手"，
    # 再把歌名塞进 id（签名代理拿它当 song 喂 OIAPI 出对应平台标），打上 src 标记。
    # 官方卡是纯跳转链接，歌名对了卡就对了，不需要该平台的真实 id/音频。
    if src in ("kugou", "migu"):
        songs = [{
            "id": s["name"], "name": s["name"], "artists": s.get("artists", ""),
            "album": s.get("album", ""), "fee": 0, "src": src,
        } for s in songs if s.get("name")]

    # 坑118外篇五（2026-11-18 用户拍板）：取消"VIP 原曲直接发卡跳过列表"特判。
    # 用户明确"都要先回复列表，再由用户从列表里选歌"——不管是不是 VIP、
    # 有没有带歌手名，一律先出候选列表。VIP 歌放行到列表里（下面 keep_all），
    # 用户选中后由 pick_cmd 按"网易云官方卡优先"发卡；官方卡只是跳转链接，
    # 不需要音频，VIP 歌照样能拿到带"网易云音乐"标的真卡片。
    # 旧特判（4a071bf 及之前）在这里提前 finish，导致带歌手名的 VIP 歌
    # 永远看不到列表——已删除，相关 QQ 找补逻辑仍在 pick_cmd 里兜底。

    # 坑113续（2026-11-15 用户拍板）：候选**不过滤** VIP/现场/伴奏版——
    # 已经是人选歌了，该让人看到全部真实结果；选中放不了会如实相告。
    # 关键词相关性仍保留（防网易云"宽容搜索"塞不相干的歌）。
    songs = [s for s in songs if usable(s, kw, keep_all=True)]
    if not songs:
        await song_cmd.finish(f"「{kw}」这几个字，我在曲子和歌手里都翻不着。换个说法试试？")

    # 用户写了歌手名，就把这位歌手的版本排到前头
    songs = rank_by_artist(songs, kw)

    # 坑111：多选点歌——不再默认点第一首，改成发候选列表让用户回序号挑。
    # 待选表按"群+用户"隔离，互不干扰；60 秒超时作废。
    picks = songs[:PICK_N]
    lines = [
        f"{i + 1}. {s['name']} - {s['artists'] or '佚名'}"
        + ("（VIP）" if s.get("fee") == 1 else "")
        + (f" 《{s['album']}》" if s.get("album") else "")
        + ("" if s.get("src", "netease") == "netease"
           else "〔酷我〕" if s["src"] == "kuwo"
           else "〔酷狗〕" if s["src"] == "kugou"
           else "〔咪咕〕" if s["src"] == "migu" else "〔QQ〕")
        for i, s in enumerate(picks)
    ]
    _PENDING[event.get_session_id()] = (time.time() + PICK_TIMEOUT, picks, force_file, kw)
    await song_cmd.finish(
        f"「{kw}」翻出这几首，回复序号选哪首（{PICK_TIMEOUT}秒内有效）：\n"
        + "\n".join(lines)
        + "\n直接发序号就行；发「取消」或超时作废。"
    )


async def try_official_card(matcher, s: dict) -> bool:
    """发官方模板卡（163/qq/kuwo/kugou/migu）。卡片只是一张跳转链接，
    **不需要本地音频**——VIP 歌、文件过大、取不到音频的歌照样能发（坑118外篇二
    用户拍板："能走官方卡就直接走"）。NapCat 侧要过签名服务，上游波动抛异常
    →返回 False 交兜底。
    坑123翻案（11-18）：旧代理4上游不支持kuwo官方卡契约故长期return False；
    接入OIAPI全平台签名源后，酷我官方卡实测能出真·"酷我音乐"标卡（appid
    100243533）——kuwo 分支不再放弃，改发 music("kuwo", id)。
    """
    src = s.get("src", "netease")
    try:
        if src == "qq":
            await matcher.send(MessageSegment.music("qq", s["id"]))
            logger.info(f"[发卡] qq官方卡 OK id={s['id']}")
        elif src == "kuwo":
            await matcher.send(MessageSegment.music("kuwo", str(s["id"])))
            logger.info(f"[发卡] kuwo官方卡 OK id={s['id']} (坑123 OIAPI酷我标)")
        elif src in ("kugou", "migu"):
            # 坑124方案：歌名存在 id 里，代理 v4 拿它当 song 喂 OIAPI 出对应标真卡
            await matcher.send(MessageSegment.music(src, str(s["id"])))
            logger.info(f"[发卡] {src}官方卡 OK 歌名={s['id']} (坑124 OIAPI全平台标)")
        else:
            await matcher.send(MessageSegment.music("163", str(s["id"])))
            logger.info(f"[发卡] 163官方卡 OK id={s['id']}")
        return True
    except Exception as e:
        logger.warning(f"[发卡] 官方卡失败 src={src} id={s['id']}: {type(e).__name__} {e}")
        return False


async def deliver(matcher, bot: Bot, event: GroupMessageEvent | PrivateMessageEvent,
                  kw: str, songs: list, force_file: bool,
                  audio: bytes | None = None, sec: int | None = None,
                  orig: dict | None = None) -> None:
    """从 songs[0] 起按顺序试，取第一个真能拿到音频的发出去。

    「点歌」直发（旧入口）与「序号选择」（坑111）两个入口共用；
    matcher 参数化：send/finish 走调用方自己的 matcher，长音频的
    _timeout 余量、finish 结束语义都保持原样。
    audio/sec：调用方已取好的音频（坑114跨平台找补场景），传了就
    不再按条目取，直接发送 songs[0]。
    orig：跨平台找补时的原曲条目（坑118外篇：原曲网易云免费就优先
    发官方163卡——官方卡只是个链接，不需要音频；文件过大只挡语音
    条，不挡卡。用户拍板"能走官方卡就直接走"）。
    """
    # 同名歌很多，按顺序试，取第一个真能拿到音频的（仅直发点歌入口用；
    # 多选入口已在处理器里单选单曲、放不了如实报告——坑113改版）
    card = songs[0]
    for s in songs[:TRY_N]:
        if audio is None:
            # 坑114：按条目平台分发取音频（netease/qq/kuwo）
            audio, sec, err = await fetch_audio_any(s)
            if not audio:
                continue
        card = s

        # ---- 文件模式：显式要求，或歌太长不赌语音 ----
        if force_file or (sec and sec > MAX_VOICE_SEC):
            note = (f"这首 {sec} 秒，直接给你传文件——下载后用什么播放器都行，"
                    f"切出聊天界面照样放，还能拖进度条。")
            if await send_file(bot, event, s, audio):
                await matcher.finish(Message(f"{build_card(s)}\n\n{note}"))
            await matcher.finish(
                Message(f"{build_card(s)}\n\n这首 {sec} 秒，文件没传上去。点上面的链接听吧。")
            )

        # ---- 语音模式：先发卡片，再发语音条 ----
        # 坑114按平台分支：qq 条目直接发官方 qq 卡；netease 免费 → 网易云卡、
        # VIP → 试 QQ 音乐找补卡；kuwo 无官方模板 → custom 卡 → 文字卡
        card_note = ""
        card_done = False
        src = s.get("src", "netease")
        if src == "qq":
            try:
                await matcher.send(MessageSegment.music("qq", s["id"]))
                card_done = True
                logger.info(f"[发卡] qq官方卡 OK id={s['id']}")
                card_note = ("点这张卡片去 QQ 音乐听——切界面、锁屏都不会停。")
            except Exception as e:
                logger.warning(f"[发卡] qq官方卡失败: {type(e).__name__} {e}")
                card_note = ""
        elif src == "netease":
            # 坑118外篇四：去掉 fee!=1 限制——VIP 歌也先试官方163卡（卡片只是
            # 跳转链接不需要音频；用户拍板"能走官方卡就直接走"）。发不出去再
            # 由下面的 QQ找补/custom卡/文字卡兜底，不会漏卡。
            try:
                await matcher.send(MessageSegment.music("163", str(s["id"])))
                card_done = True
                logger.info(f"[发卡] 163官方卡 OK id={s['id']}")
            except Exception as e:
                logger.warning(f"[发卡] 163官方卡失败: {type(e).__name__} {e}")
                card_note = ""
        elif src == "kuwo":
            # 坑123翻案（11-18）：接入OIAPI全平台签名源后，酷我官方卡能出真
            # "酷我音乐"标卡（实测 appid=100243533）——不再只靠无封面的custom
            # 卡（QQ音乐标）。先发官方酷我卡，失败再落下面的custom/文字卡兜底。
            try:
                await matcher.send(MessageSegment.music("kuwo", str(s["id"])))
                card_done = True
                logger.info(f"[发卡] kuwo官方卡 OK id={s['id']} (坑123 OIAPI酷我标)")
                card_note = "点这张卡片去酷我音乐听——切界面、锁屏都不会停。"
            except Exception as e:
                logger.warning(f"[发卡] kuwo官方卡失败: {type(e).__name__} {e}")
                card_note = ""
        # 坑118外篇（2026-11-17 用户拍板）：跨平台找补场景，原曲网易云
        # 免费 → 优先发原曲的163官方卡（卡片只是链接，不需要音频；
        # "文件过大"是我们给语音条设的下载上限，不挡官方卡），酷我
        # 音频继续当语音条。官方卡失败才落 custom 卡兜底。
        if not card_done and not card_note and orig \
                and orig.get("src", "netease") == "netease" \
                and orig.get("fee") != 1:
            try:
                await matcher.send(MessageSegment.music("163", str(orig["id"])))
                card_done = True
                logger.info(f"[发卡] 163官方卡(找补原曲) OK id={orig['id']}")
            except Exception as e:
                logger.warning(f"[发卡] 163官方卡(找补原曲)失败: {type(e).__name__} {e}")
        if not card_done and src == "netease":
            # 网易云 VIP/官方卡发不出去 → QQ 音乐找补（坑80续：哪边免费用哪边）
            mid, vip = await qq_card_mid(kw)
            if mid:
                try:
                    await matcher.send(MessageSegment.music("qq", mid))
                    card_done = True
                    logger.info(f"[发卡] qq找补官方卡 OK mid={mid}")
                    if vip:
                        card_note = ("上面这张卡片是 QQ 音乐的同一首歌，点它也能后台听；"
                                     "不过这首在 QQ 音乐要 VIP，点进去可能只能试听几十秒，"
                                     "听整首还是趁语音条。")
                    elif s.get("fee") == 1:
                        card_note = ("这首在网易云要 VIP，但 QQ 音乐有免费的——点这张卡片"
                                     "去 QQ 音乐听完整版，切界面、锁屏都不会停。")
                    else:
                        card_note = ("点这张卡片去 QQ 音乐听——切界面、锁屏都不会停。")
                except Exception:
                    card_done = False
                    card_note = ""
        # 坑107终局：官方模板卡要过外部签名服务，公益签名服务全灭时
        # 发卡失败——这里接 custom 卡兜底，再不行文字卡，点歌体验不断；
        # 上游复活时真卡自动回来，无需人工干预
        # 坑118仪表化：每个分支发没发出去都落日志，实测无卡时日志必给实锤
        if not card_done and not card_note:
            _ok = await send_custom_card(s)
            if _ok:
                logger.info(f"[发卡] custom卡 OK src={s.get('src','netease')} id={s['id']}")
            else:
                logger.warning(f"[发卡] custom卡失败(封面缺或发送异常) src={s.get('src','netease')} id={s['id']} → 落文字卡")
                await matcher.send(Message(build_card(s)))

        try:
            # 长音频发过去要等 NapCat 转 silk 再上传，默认 30 秒的 api_timeout
            # 根本不够，按体积给个宽裕的余量。
            _t = 60 + len(audio) // (200 * 1024)
            await matcher.send(MessageSegment.record(audio), _timeout=_t)
            probe(sec, len(audio), "ok", "")
            if card_note:
                await matcher.send(card_note)
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
                await matcher.finish(Message(f"{build_card(s)}\n\n{note}，改成传文件了——下载后切界面也能听。"))
            await matcher.finish(
                Message(f"{build_card(s)}\n\n{note}，文件也没传上去。点上面的链接听吧。")
            )

    # 全都拿不到音频，仍先递官方卡（卡片只是链接不需要音频），再退回文字卡
    if await try_official_card(matcher, card):
        await matcher.finish("这首柜上翻不出音频，点上面这张卡片去平台听吧。")
    await matcher.finish(
        Message(f"{build_card(card)}\n\n曲子我调不出来，只能给您递个名目。")
    )


# ---- 坑111：序号选择 ----
# 待选表：session_id → (过期时间戳, 候选列表)
_PENDING: dict = {}
PICK_TIMEOUT = 60
PICK_N = 6


async def _pick_rule(event: GroupMessageEvent | PrivateMessageEvent) -> bool:
    """只有"该用户点过歌"时，纯数字/取消消息才被这里接住——其余一律放行。
    坑115：过期列表也接住——用户超时后发序号，与其让 AI 复读一个光秃秃
    的"4"（像坏了），不如明确告诉他列表作废了。接住即清，只拦一次。"""
    txt = event.get_plaintext().strip()
    if not re.fullmatch(r"[0-9]{1,2}|取消", txt):
        return False
    return event.get_session_id() in _PENDING


pick_cmd = on_message(rule=_pick_rule, priority=1, block=True)
# priority=1（坑113）：必须排在一切对话类处理器之前。原来 priority=15 排在
# ai_chat 后面，用户发"1"会先被 AI 当聊天接走抢答一句，选择器再执行——
# 双重回复。选择规则本身极窄（60秒内点过歌的纯数字），提到最前不会误伤。


@pick_cmd.handle()
async def _(bot: Bot, event: GroupMessageEvent | PrivateMessageEvent):
    sid = event.get_session_id()
    expiry, picks, force_file, kw = _PENDING.pop(sid)
    txt = event.get_plaintext().strip()
    # 坑115：选择期过了就明说作废，不让消息漏给 AI 复读
    if expiry < time.time():
        await pick_cmd.finish(
            "刚才那份列表过了60秒，作废了——想听重新点一次，序号马上给你重排。")
    if txt == "取消":
        await pick_cmd.finish("好，柜子关上了。想听再点。")
    try:
        n = int(txt)
    except ValueError:
        n = 0
    if not 1 <= n <= len(picks):
        await pick_cmd.finish(f"序号 1~{len(picks)} 才算数；要重新翻柜子就再说一次「点歌」。")
    s = picks[n - 1]
    # 坑113改版（2026-11-15 用户拍板）：选哪首就是哪首——拿不到音频就如实
    # 说，绝不顺延换歌（顺延到的那首不是点歌人想要的，等于答非所问）。
    # 坑114：本平台放不了 → 先找**同一首**的跨平台音频（能直接听），
    # 再试 QQ 卡，最后如实报告。待选表在剩余时间内保留，可改选。
    audio, sec, err = await fetch_audio_any(s)
    if not audio:
        if expiry >= time.time():
            _PENDING[sid] = (expiry, picks, force_file, kw)
        # 坑124方案：酷狗/咪咕本来就不取音频（官方卡=纯跳转链接，签名代理出
        # 对应标真卡），fetch_audio_any 对它们必然返回 None 是预期内，不算故障。
        # 直接在这里发官方卡收尾，别掉进下面的"网络错误/跨平台找补/如实告知"。
        if s.get("src") in ("kugou", "migu"):
            if await try_official_card(pick_cmd, s):
                label = "酷狗音乐" if s["src"] == "kugou" else "咪咕音乐"
                await pick_cmd.finish(
                    f"点上面这张「{label}」卡片，去{label}听「{s['name']}」——"
                    f"卡片是跳转链接，用对应 App 打开就能放。")
        # 网络类错误先如实说：不是歌的问题，重试可能就好，不浪费找补
        if "HTTP" in err or "Error" in err or "Timeout" in err:
            await pick_cmd.finish(
                f"取「{s['name']}」的音频时网络出了岔子（{err}），不是曲子的问题——"
                f"您再回一次这个序号试试。")
        # ① 同曲跨平台音频找补（能直接听的优先——不用跳别的平台）
        alt, a2, s2 = await same_song_elsewhere(s)
        if alt and a2:
            # 坑118续（2026-11-17 用户实测"列表选歌无卡片"）：酷我条目天生
            # 不带封面字段，custom 卡无图会被 NapCat 拒 → 每次都落文字卡。
            # 把原曲封面带过去：搜索条目自带的 pic 优先，网易云条目再退
            # meting 的 song 接口（稳定带 pic）。QQ 找补分支自带封面兜底。
            if not alt.get("pic"):
                alt["pic"] = s.get("pic") or ""
                if s.get("src", "netease") == "netease" and not alt["pic"]:
                    try:
                        alt["pic"] = await _song_pic(s["id"]) or ""
                    except Exception:
                        pass
            await pick_cmd.send(
                f"您选的这首在{_why_no_audio(s, err)}——"
                f"同一首歌我从{'酷我' if alt['src'] == 'kuwo' else 'QQ 音乐'}的柜子里"
                f"找到了，给您递过来：")
            await deliver(pick_cmd, bot, event, kw, [alt], force_file, audio=a2, sec=s2, orig=s)
            return
        # 坑118外篇五（2026-11-18 用户拍板）：网易云 VIP 歌优先发"网易云官方卡"——
        # 官方卡天然带"网易云音乐"标，卡片只是跳转链接不需要音频。用户截图实测
        # 已能收到该标（02:19:40 [发卡]163官方卡OK id=284799），之前"做不了"是
        # 把官方卡和 custom 卡搞混的误判。QQ 找补卡显示"QQ音乐"标，降为兜底。
        if s.get("src", "netease") == "netease" and s.get("fee") == 1:
            if await try_official_card(pick_cmd, s):
                await pick_cmd.finish(
                    f"这首是「{s['artists']}」的原曲，两边平台都要 VIP，柜上拿不来整首的音频"
                    f"——点上面这张卡片去网易云听（有会员就能听整首）。")
        # ② QQ 音乐卡片找补（跳转听；条目本身就是 QQ 的就不用再找）
        if s.get("src", "netease") != "qq" and (s.get("fee") == 1 or err == "不是音频"):
            mid, qvip = await qq_card_mid(s["name"])
            if mid:
                try:
                    await pick_cmd.send(MessageSegment.music("qq", mid))
                    if qvip:
                        await pick_cmd.finish(
                            f"您选的这首{_why_no_audio(s, err)}；QQ 音乐那边同样要 VIP，"
                            "点上面卡片大概只能试听几十秒——您回别的序号，或重新点歌。")
                    await pick_cmd.finish(
                        f"您选的这首{_why_no_audio(s, err)}——不过 QQ 音乐有免费的，"
                        "点上面这张卡片去听完整版，切界面、锁屏都不会停。")
                except FinishedException:
                    raise
                except Exception:
                    pass  # 卡片没递出去就走下面的诚实告知
            if s.get("fee") == 1:
                if await send_custom_card(s):
                    await pick_cmd.finish(
                        f"这首是「{s['artists']}」的原曲，两边平台都要 VIP——点上面这张卡片听吧。")
                await pick_cmd.finish(
                    Message(f"{build_card(s)}\n\n这首是「{s['artists']}」的原曲，"
                            f"两边平台都要 VIP，柜上拿不来整首的音频"
                            f"——点上面的链接去听吧。"))
        # ③ 最后如实告知，绝不含糊
        await pick_cmd.finish(
            f"您选的「{s['name']}」这首，{_why_no_audio(s, err)}，"
            f"柜上真放不了。想听别首就再回个序号，或重新点歌。")
    await deliver(pick_cmd, bot, event, kw, [s], force_file)


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


async def _song_pic(song_id: int) -> str:
    """封面链路（坑118终版）：网易云官方 song/detail 接口的专辑 picUrl
    是**直链**（实测 HTTP 200，一步到位）；meting 的 pic 是 302 跳转链
    且带 param=90y90 缩略参数——QQ 卡片抓二次跳转很容易失败，抓到也是
    缩略小图（用户实测 04:20/04:22 两张 custom 卡均无封面）。
    官方直链优先，meting 跳转链降为兜底。
    """
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as sess:
            async with sess.get(
                f"https://music.163.com/api/song/detail?id={song_id}&ids=%5B{song_id}%5D",
                headers={"Referer": "https://music.163.com/"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                d = await r.json(content_type=None)
        s = (d.get("songs") or [{}])[0]
        pic = (s.get("album") or {}).get("picUrl") or ""
        if pic:
            return f"{pic}?param=300y300"
    except Exception:
        pass
    return await _meting_pic("netease", song_id)


async def _meting_pic(server: str, item_id) -> str:
    """meting song 接口取封面（netease/qq 两家都走这个）。"""
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as sess:
            async with sess.get(
                f"{STREAM}?type=song&server={server}&id={item_id}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                d = await r.json(content_type=None)
        if isinstance(d, list):
            d = d[0] if d else {}
        return (d or {}).get("pic") or ""
    except Exception:
        return ""


async def send_custom_card(s: dict) -> bool:
    """坑107兜底：官方模板卡（163/qq）要过外部签名服务，公益签名服务一挂
    卡片就全军覆没（实测 2026-11-14 四个已知签名地址全不通）。自定义卡片
    用自己拼的 ark 结构，NapCat 直接发、不走签名服务，材料全是现成的：
    网易云页面链接 + meting 音频外链 + 封面 + 歌名。
    用户已拍板不发附带说明句（坑98文案定版），所以成功即静默。
    """
    pic = s.get("pic") or ""
    src = s.get("src", "netease")
    if src == "qq":
        page = f"https://y.qq.com/n/ryqq/songDetail/{s['id']}"
        audio_url = f"{STREAM}?type=url&server=tencent&id={s['id']}"
        if not pic:
            pic = await _meting_pic("tencent", s["id"])
    elif src == "kuwo":
        page = f"https://www.kuwo.cn/play_detail/{s['id']}"
        audio_url = (f"{KW_STREAM}?type=convert_url&format=mp3"
                     f"&response=url&rid={s['id']}")
        # 酷我拿不到稳定封面，缺图时 NapCat 会拒卡 → 走文字卡兜底
    else:
        page = f"https://music.163.com/song?id={s['id']}"
        audio_url = f"{STREAM}?type=url&server=netease&id={s['id']}"
        if not pic:
            pic = await _song_pic(s["id"])
    if not pic:
        logger.warning(f"[发卡] custom卡放弃: 无封面 src={src} id={s['id']}")
        return False  # NapCat 硬性要求 image，拿不到封面就别硬发
    try:
        await song_cmd.send(MessageSegment.music_custom(
            url=page,
            audio=audio_url,
            title=f"{s['name']} - {s['artists'] or '佚名'}",
            content=s.get("album") or None,
            img_url=pic,
        ))
        logger.info(f"[发卡] custom卡发送成功 src={src} id={s['id']}")
        return True
    except Exception as e:
        logger.warning(f"[发卡] custom卡发送异常: {type(e).__name__} {e}")
        return False


async def qq_card_mid(keyword: str):
    """查 QQ 音乐，返回 (songmid, 是否VIP) 或 (None, False)。

    卡片是发给 QQ 音乐版权库的，和网易云搜索结果不是一家——按完整
    关键词（歌名+歌手）在 QQ 音乐再搜一次。**优先挑免费的那条**
    （坑80续：哪边免费用哪边），全是 VIP 才退回第一条。

    ⚠️ QQ 音乐搜索同样"宽容"：乱码也能返回不相干的歌（实测"9zq"
    返回了《珍惜对你例外的人》），所以必须过相关性检查才准发。
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
        relevant = [s for s in lst if _qq_relevant(s, keyword)]
        if not relevant:
            return None, False
        for s in relevant:
            if not (s.get("pay") or {}).get("payplay"):
                return s.get("songmid"), False
        s = relevant[0]
        return s.get("songmid"), True
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
                "pic": (s.get("album") or {}).get("picUrl", ""),
                "sec": int((s.get("duration") or 0) // 1000),
                "fee": s.get("fee", 0),
            })
        return out
    except Exception as e:
        logger.warning(f"[点歌] 搜索「{keyword}」异常: {e!r}")
        return []


def _norm(t: str) -> str:
    """只留汉字和字母数字，用来比对名字。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", (t or "").lower())


def usable(s: dict, keyword: str, keep_all: bool = False) -> bool:
    """过滤掉注定发不出声的条目。

    网易云的搜索很"宽容"，输一串乱码也能返 10 条不相干的歌，所以：
    1. 关键词得跟 歌名/歌手/专辑 其中之一真的沾边（去符号后比对）
       —— 坑80：旧版只比歌名，导致"只搜歌手名"全军覆没
    2. 剔除伴奏 / 纯音乐 / DJ 版（这些是给翻唱伴奏用的，不是给人听的）

    keep_all=True（坑113续，用户拍板）：多选候选不过滤 VIP/现场/伴奏——
    人选歌不预筛，只留关键词相关性这一条。
    """
    if not keep_all:
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
    # 2026-11-12：上游 meting 接口行为变更——不带 server 参数返回 200 空体，
    # 必须显式 server=netease（歌源全是网易云搜索，恒定 netease）。缺它=语音全灭。
    url = f"{STREAM}?type=url&server=netease&id={song_id}"
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


# ---- 坑114：多平台（2026-11-15 用户要求"全部都做上"）----
# 实测平台矩阵（2026-11-15）：
#   网易云 搜索✅ 音频✅（部分VIP/版权拿不到）——主平台
#   QQ音乐 搜索✅（自家接口） 音频✅（meting代理直链）——全链路
#   酷我   搜索✅（r.s老接口） 音频✅（antiserver直链）——全链路
#   酷狗/咪咕 原判定"音频要签名/搜索死"→不接（那是为语音条写的，已过时）
#   坑124方案（11-18）：官方卡=纯跳转链接不需要本地音频，复用网易云搜索拿
#   歌名歌手 → 塞进 id → 代理v4 用 OIAPI 出对应标真卡（酷狗音乐/咪咕音乐）。
#   搜索用网易云的（这两家自身搜索接口死的死、要签名的要签名），只发卡不取音频。


async def search_qq(keyword: str) -> list:
    """QQ 音乐搜索，条目转统一结构（src='qq'，id=songmid）。"""
    try:
        async with aiohttp.ClientSession(
            headers={"User-Agent": UA, "Referer": "https://y.qq.com/"},
        ) as sess:
            async with sess.get(
                QQ_SEARCH,
                params={"w": keyword, "format": "json", "n": 12},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                d = await r.json(content_type=None)
        out = []
        for s in ((d.get("data") or {}).get("song") or {}).get("list") or []:
            out.append({
                "id": s.get("songmid") or "",
                "name": s.get("songname") or "",
                "artists": "/".join(x.get("name", "") for x in s.get("singer") or []),
                "album": s.get("albumname") or "",
                "fee": 1 if (s.get("pay") or {}).get("payplay") else 0,
                "src": "qq",
            })
        return out
    except Exception as e:
        logger.warning(f"[点歌] QQ音乐搜索「{keyword}」异常: {e!r}")
        return []


async def search_kuwo(keyword: str) -> list:
    """酷我搜索。r.s 老接口返回的是单引号 JS 对象，ast.literal_eval 解析；
    &nbsp; 和 \\u0026 是它家的转义怪癖，先还原再当正文。"""
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as sess:
            async with sess.get(
                KW_SEARCH,
                params={"all": keyword, "ft": "music", "itemset": "web_2013",
                        "client": "mp3", "p2p": "1", "rformat": "json",
                        "encoding": "utf8", "pn": "0", "rn": "12"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                txt = await r.text()
        d = ast.literal_eval(txt.replace("&nbsp;", " "))
        out = []
        for m in d.get("abslist", []):
            out.append({
                "id": (m.get("MUSICRID") or "").replace("MUSIC_", ""),
                "name": (m.get("SONGNAME") or "").replace("\\u0026", "&"),
                "artists": (m.get("ARTIST") or "").replace("\\u0026", "&"),
                "album": m.get("ALBUM") or "",
                "fee": 1 if m.get("PAY") == "1" else 0,
                "src": "kuwo",
            })
        return out
    except Exception as e:
        logger.warning(f"[点歌] 酷我搜索「{keyword}」异常: {e!r}")
        return []


async def fetch_audio_qq(songmid: str):
    """QQ 音乐音频：meting 的 song 接口给代理直链，再下载二进制。"""
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as sess:
            async with sess.get(
                f"{STREAM}?type=song&server=tencent&id={songmid}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                d = await r.json(content_type=None)
            if isinstance(d, list):
                d = d[0] if d else {}
            url = (d or {}).get("url") or ""
            if not url.startswith("http"):
                return None, 0, "不是音频"
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200:
                    return None, 0, f"HTTP {r.status}"
                data = await r.read()
        if len(data) < 20 * 1024:
            return None, 0, "文件过小"
        return data, mp3_seconds(data), ""
    except Exception as e:
        return None, 0, type(e).__name__


async def fetch_audio_kuwo(rid: str):
    """酷我音频：antiserver 直接返回 mp3 直链文本，再下载二进制。"""
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as sess:
            async with sess.get(
                KW_STREAM,
                params={"type": "convert_url", "format": "mp3",
                        "response": "url", "rid": rid},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as r:
                url = (await r.text()).strip()
            if not url.startswith("http"):
                return None, 0, "不是音频"
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200:
                    return None, 0, f"HTTP {r.status}"
                data = await r.read()
        if len(data) < 20 * 1024:
            return None, 0, "文件过小"
        return data, mp3_seconds(data), ""
    except Exception as e:
        return None, 0, type(e).__name__


async def fetch_audio_any(s: dict):
    """按条目 src 分发取音频 → (bytes, 秒, err)。"""
    src = s.get("src", "netease")
    if src == "kuwo":
        return await fetch_audio_kuwo(s["id"])
    if src == "qq":
        return await fetch_audio_qq(s["id"])
    if src in ("kugou", "migu"):
        # 坑124：这两家只发官方卡（跳转链接），不取音频；id 里存的是歌名
        return None, 0, "官方卡平台"
    return await fetch_audio(s["id"])


async def kuwo_find_same(s: dict):
    """同曲酷我找补：在酷我按"歌名+歌手"搜，只认歌名和歌手都沾边的——
    找补的是"这一首"，不是近似品（坑113原则：绝不换歌）。"""
    target = f"{s['name']} {s['artists']}".strip()
    for c in await search_kuwo(target):
        if not c["id"]:
            continue
        if _norm(s["name"]) in _norm(c["name"]) and (
                not s.get("artists") or _norm(s["artists"]) in _norm(c["artists"])):
            return c
    return None


async def qq_find_same(s: dict):
    """同曲 QQ 音乐找补（返回条目，卡片/音频都从它出）。"""
    for c in await search_qq(f"{s['name']} {s['artists']}".strip()):
        if not c["id"]:
            continue
        if _norm(s["name"]) in _norm(c["name"]) and (
                not s.get("artists") or _norm(s["artists"]) in _norm(c["artists"])):
            return c
    return None


async def same_song_elsewhere(s: dict):
    """同曲跨平台音频找补：本平台放不了 → 去另一家找**同一首**，
    音频拿得到才返回（坑113原则：找补的是这一首，不是近似品）。
    返回 (替代条目, 音频bytes, 秒) 或 (None, None, None)。"""
    if s.get("src", "netease") != "kuwo":
        alt = await kuwo_find_same(s)
        if alt:
            a, sec, _ = await fetch_audio_kuwo(alt["id"])
            if a:
                return alt, a, sec
    else:
        alt = await qq_find_same(s)
        if alt:
            a, sec, _ = await fetch_audio_qq(alt["id"])
            if a:
                return alt, a, sec
    return None, None, None


def _why_no_audio(s: dict, err: str) -> str:
    """如实播报本平台为什么放不了——什么平台什么原因就说什么（坑113续）。"""
    pname = {"netease": "网易云", "qq": "QQ音乐", "kuwo": "酷我"}[s.get("src", "netease")]
    if s.get("fee") == 1:
        return f"{pname}这首歌要 VIP，柜上取不出免费的音频"
    if err == "不是音频":
        return f"{pname}的歌源没递出这首的音频（这类一般是版权受限的曲子）"
    return f"{pname}这首歌放不了（{err}）"


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
    src = s.get("src", "netease")
    # 坑114：文字卡链接必须指向歌所在的真实平台（旧版写死网易云，
    # QQ/酷我的歌点开就是错页）
    if src == "qq":
        link = f"https://y.qq.com/n/ryqq/songDetail/{s['id']}"
    elif src == "kuwo":
        link = f"https://www.kuwo.cn/play_detail/{s['id']}"
    else:
        link = f"https://music.163.com/song?id={s['id']}"
    return (
        f"🎵 {s['name']}\n"
        f"　　{artist}"
        f"{'　《' + s['album'] + '》' if s.get('album') else ''}\n"
        f"　　{link}"
    )