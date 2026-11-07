"""
抽签 —— 阿萨尔替人翻一页命书

命令：/抽签  /运势  /翻书  /折卡

设计要点
1. 签筒就是苏丹的卡组：4 类卡 × 4 品级 = 16 种牌面，
   每张牌面配一张签诗，共 16 张签。
2. 签筒 32 支，每种签占 2 支 —— 16 张签概率完全相同，各 6.25%。
3. 品级写在卡名那一行最前面，一眼能看见。
4. 吉凶按"离你多远"分：两头（大吉/吉/小凶）各 3 张，中间（小吉/平/凶）各 2 张，
   "未定" 1 张 —— 吉凶越极端，能说的话越多。
"""
import random
import datetime
import json as _json
import io as _io
import base64 as _b64
from pathlib import Path

from nonebot import on_command
from nonebot.rule import to_me
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent, Message, MessageSegment

fortune = on_command("抽签", aliases={"运势", "翻书", "折卡"}, rule=to_me(), priority=20, block=True)

# ============ 抽签记账：给「你还记得我吗」的小传提供素材 ============
# 同一人当天签是固定的（种子里有日期），所以只在跨天时记一条，绝不刷屏。
FORTUNE_LOG = Path(__file__).resolve().parent.parent / "data" / "fortune_log.json"


def _log_draw(uid: int, sign: dict) -> None:
    try:
        data = {}
        if FORTUNE_LOG.exists():
            data = _json.loads(FORTUNE_LOG.read_text(encoding="utf-8"))
        arr = data.setdefault(str(uid), [])
        today = datetime.date.today().isoformat()
        if not arr or arr[-1].get("d") != today:
            arr.append({"d": today, "key": sign["key"], "name": sign["name"]})
            data[str(uid)] = arr[-200:]  # 防膨胀：只留最近200天
            FORTUNE_LOG.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # 记账失败不影响抽签


# ============ 签卡成像：抽到一张"看得见"的苏丹卡牌 ============
# 本地 Pillow 画，不依赖任何生图接口；字体缺失/画崩时返回 None，退回纯文字签。
_CARD_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]
# 品级配色对齐原作：金=黄金、银=白银、铜=青铜、石=生赭色（SCP-19299："金、银、青铜、生赭四色对应金银铜石"）
_TIER_RGB = {"金": (216, 176, 74), "银": (185, 196, 206), "铜": (172, 114, 60), "石": (148, 88, 52)}
_KEY_RGB = {"大吉": (168, 108, 10), "吉": (128, 100, 22), "小吉": (108, 92, 44),
            "平": (108, 108, 100), "小凶": (64, 96, 140), "凶": (172, 36, 20), "未定": (118, 110, 98)}


def _wrap_cn(draw, text, font, maxw):
    lines, cur = [], ""
    for ch in str(text):
        if cur and draw.textlength(cur + ch, font=font) > maxw:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def _render_card(sign, card, tier, scores, avg, user, date_str):
    """把这一签画成一张卡牌图，返回 png 字节；任何一步失败返回 None。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        font_path = next((p for p in _CARD_FONT_CANDIDATES if Path(p).exists()), None)
        if not font_path:
            return None
        W, H = 720, 1080
        tc = _TIER_RGB[tier["name"]]
        tc_text = tuple(int(c * 0.72) for c in tc)   # 品级色文字用（白底上加深）
        kc = _KEY_RGB.get(sign["key"], (110, 102, 88))
        img = Image.new("RGB", (W, H), (252, 250, 246))
        d = ImageDraw.Draw(img)
        # 双层边框：外粗内细，品级色
        d.rectangle([8, 8, W - 9, H - 9], outline=tc, width=5)
        d.rectangle([20, 20, W - 21, H - 21], outline=tuple(int(c * 0.55) for c in tc), width=2)
        f_small = ImageFont.truetype(font_path, 26)
        f_key = ImageFont.truetype(font_path, 58)
        f_name = ImageFont.truetype(font_path, 96)
        f_body = ImageFont.truetype(font_path, 31)
        f_dim = ImageFont.truetype(font_path, 27)
        # 顶部戳记
        stamp = "阿萨尔书店 · 命书"
        d.text(((W - d.textlength(stamp, font=f_small)) / 2, 52), stamp, font=f_small, fill=(140, 128, 110))
        # 吉凶 + 卡名
        d.text(((W - d.textlength(sign["key"], font=f_key)) / 2, 108), sign["key"], font=f_key, fill=kc)
        name = f"「{sign['name']}」"
        d.text(((W - d.textlength(name, font=f_name)) / 2, 190), name, font=f_name, fill=(50, 43, 36))
        # 品级行
        tierline = f"{tier['name']}品 · {sign['card']}卡 · 应在{card['realm']}　签力 {int(avg)}"
        d.text(((W - d.textlength(tierline, font=f_small)) / 2, 318), tierline, font=f_small, fill=tc_text)
        d.line([70, 372, W - 70, 372], fill=(185, 176, 160), width=2)
        # 签诗（自动折行，居中）
        y = 400
        for ln in _wrap_cn(d, sign["line"], f_body, W - 160):
            d.text(((W - d.textlength(ln, font=f_body)) / 2, y), ln, font=f_body, fill=(62, 54, 46))
            y += 44
        y += 8
        for ln in _wrap_cn(d, sign["note"], f_body, W - 160):
            d.text(((W - d.textlength(ln, font=f_body)) / 2, y), ln, font=f_body, fill=(105, 96, 84))
            y += 44
        # 五维条
        y = max(y + 28, 620)
        d.text((70, y - 6), "五维", font=f_dim, fill=(140, 128, 110))
        y += 44
        bar_x, bar_w = 210, 330
        for dim, v in scores.items():
            d.text((70, y), dim, font=f_dim, fill=(85, 76, 66))
            d.rounded_rectangle([bar_x, y + 4, bar_x + bar_w, y + 26], radius=8, fill=(228, 223, 215))
            fill_w = int(bar_w * (v - 20) / 80)
            d.rounded_rectangle([bar_x, y + 4, bar_x + max(fill_w, 14), y + 26], radius=8, fill=tc)
            d.text((bar_x + bar_w + 18, y), str(v), font=f_dim, fill=(62, 54, 46))
            y += 46
        # 底部：人 + 日期
        foot = f"{user}　{date_str}"
        d.text(((W - d.textlength(foot, font=f_small)) / 2, H - 96), foot, font=f_small, fill=(140, 128, 110))
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None

# ============ 四类卡：决定运势"应在何处" ============
CARD_TYPES = {
    "纵欲": {"mark": "🍷", "realm": "人情", "bias": {"情缘": 14, "体魄": -6, "才学": -4}},
    "奢靡": {"mark": "💰", "realm": "财货", "bias": {"财货": 16, "体魄": -5}},
    "征服": {"mark": "⚔️", "realm": "行止", "bias": {"才学": 10, "体魄": 9, "见识": 6}},
    "杀戮": {"mark": "🗡️", "realm": "取舍", "bias": {"体魄": 5, "情缘": -11, "才学": 4}},
}

# ============ 四品级 ============
# 只有金/银/铜/石四个字，跟原作一致，不加"上等""极贵"这类游戏里没有的修饰
TIERS = {
    "金": {"mark": "🔆", "name": "金", "offset": 7},
    "银": {"mark": "🌙", "name": "银", "offset": 3},
    "铜": {"mark": "🔶", "name": "铜", "offset": 0},
    "石": {"mark": "🪨", "name": "石", "offset": -5},
}

TIER_ORDER = ["金", "银", "铜", "石"]
CARD_ORDER = ["纵欲", "奢靡", "征服", "杀戮"]

# ============ 16 张签 ============
# key/card/tier：这张签对应哪个牌面
# score：这一签在吉凶轴上的位置（55 为中线）
# line：签诗第一句（说命）
# note：签诗第二句（说理）
SIGNS = [
    # ---- 大吉 3 张 ----
    {
        "key": "大吉", "mark": "🌅", "card": "征服", "tier": "金", "name": "破城",
        "tag": "势在你这边，进则得",
        "score": 90,
        "line": "城门已经开了一条缝，推的人只需再使一把力。",
        "note": "这是苏丹亲手折过的牌位——凡今日起意的事，都不必再等。",
    },
    {
        "key": "大吉", "mark": "🌅", "card": "奢靡", "tier": "金", "name": "满库",
        "tag": "有进无出，宜受不宜辞",
        "score": 92,
        "line": "库房的门被风吹开了，里面的东西正往外淌。",
        "note": "它不会一直淌，接住了就是你的，托词推掉的，就归了别人。",
    },
    {
        "key": "大吉", "mark": "🌅", "card": "纵欲", "tier": "金", "name": "灯下人",
        "tag": "所遇皆投契",
        "score": 86,
        "line": "今夜的灯烧得比平日亮，照到的人也比平日多。",
        "note": "你要见的那个人，也在等一个开口的由头。",
    },

    # ---- 吉 3 张 ----
    {
        "key": "吉", "mark": "✨", "card": "征服", "tier": "银", "name": "月升",
        "tag": "顺，中间有枝节",
        "score": 80,
        "line": "月升在庭院上头，路看得见，只是有几处要绕。",
        "note": "绕过去的都算数，硬撞过去的要付代价。",
    },
    {
        "key": "吉", "mark": "✨", "card": "奢靡", "tier": "银", "name": "集市晴",
        "tag": "有进项，宜落袋",
        "score": 82,
        "line": "集市今日开得早，价也好，只是好价不等人。",
        "note": "该收的今日收，收晚了，价就不是这个价了。",
    },
    {
        "key": "吉", "mark": "✨", "card": "杀戮", "tier": "铜", "name": "断绳",
        "tag": "宜断，断了才轻",
        "score": 76,
        "line": "有根绳子挂在脖子上很久了，其实另一头什么也没拴。",
        "note": "今日剪断它不难，难的是先承认另一头一直是空的。",
    },

    # ---- 小吉 2 张 ----
    {
        "key": "小吉", "mark": "🕯️", "card": "纵欲", "tier": "铜", "name": "灯下",
        "tag": "不出错，也未必出彩",
        "score": 66,
        "line": "一盏灯照着一尺地，看得清脚下，看不清远处。",
        "note": "一尺地也够走一步，只是别拿它当整条路。",
    },
    {
        "key": "小吉", "mark": "🕯️", "card": "征服", "tier": "石", "name": "苦尽",
        "tag": "小处得手，宜自喜不宜自满",
        "score": 68,
        "line": "磨了很久的那把刀，今日终于能割开东西。",
        "note": "石磨出来的刃不利，但比没有强——先把手头这件做完。",
    },

    # ---- 平 2 张 ----
    {
        "key": "平", "mark": "⚖️", "card": "奢靡", "tier": "铜", "name": "集市日",
        "tag": "人来人往，与你无涉",
        "score": 53,
        "line": "集市照常开张，热闹是别人的，你只是路过。",
        "note": "路过也有路过的好处——今日不必被谁记住。",
    },
    {
        "key": "平", "mark": "⚖️", "card": "杀戮", "tier": "石", "name": "旧账",
        "tag": "无吉无凶，只宜理清",
        "score": 50,
        "line": "账本上有一行字迹淡了，看不出是欠人还是人欠。",
        "note": "弄不清楚的就先记着，别凭猜想去结它。",
    },

    # ---- 小凶 3 张 ----
    {
        "key": "小凶", "mark": "🌧️", "card": "纵欲", "tier": "石", "name": "黑街雨",
        "tag": "宜守，不宜进",
        "score": 40,
        "line": "黑街下雨的时候，最滑的是自己脚下的那块石头。",
        "note": "不是路难走，是自己站得不稳。",
    },
    {
        "key": "小凶", "mark": "🌧️", "card": "奢靡", "tier": "石", "name": "空匣",
        "tag": "有耗无得，宜闭手",
        "score": 42,
        "line": "匣子打开是空的，可你已经按市价付过钱了。",
        "note": "今日出门的银钱，大多换不回东西，只换回一个教训。",
    },
    {
        "key": "小凶", "mark": "🌧️", "card": "杀戮", "tier": "银", "name": "逆风",
        "tag": "使力不得力",
        "score": 37,
        "line": "风是逆的，帆也是好的，可船就是不动。",
        "note": "不是船不好，是今日不该走这条路——停一日也无妨。",
    },

    # ---- 凶 2 张 ----
    {
        "key": "凶", "mark": "🩸", "card": "杀戮", "tier": "金", "name": "刀刃",
        "tag": "利刃当前，慎之又慎",
        "score": 22,
        "line": "刀已经出鞘了，握着的人不一定是想伤你，也可能是手滑。",
        "note": "金品最利的刃，砍的是最熟的人。今日少去一处，少说一句。",
    },
    {
        "key": "凶", "mark": "🩸", "card": "纵欲", "tier": "银", "name": "折卡之日",
        "tag": "期限压头，宜静不宜动",
        "score": 18,
        "line": "牌已经在手上，再添一张，就是两张。",
        "note": "石卡压不死人，压死人的是慌——先把手里这张了了。",
    },

    # ---- 未定 1 张 ----
    {
        "key": "未定", "mark": "🌫️", "card": "征服", "tier": "铜", "name": "沙上字",
        "tag": "此刻看不出来，看你怎么走",
        "score": 57,
        "line": "沙上写过字，风还没起，谁也不知道会剩下几个。",
        "note": "这一签不作数，它只是问你：今日想让它成什么。",
    },
]

# ============ 这句签在签筒里占 2 支，共 32 支 ============
assert len(SIGNS) == 16, f"签数应为 16，实为 {len(SIGNS)}"

DIMENSIONS = ["才学", "财货", "情缘", "体魄", "见识"]

# (好运时的话, 差运时的话) —— 差运时不能说"宜"，只能说"还剩下什么"
DIM_ADVICE = {
    "才学": ("宜多读少言，今日记下的往后都用得上", "还肯动脑子，就还有回旋的余地"),
    "财货": ("有进项，但别急着花出去", "手上还有点余钱，这就是底气"),
    "情缘": ("有话趁早说，别留到明天", "还有人愿意听你说话，别把人推远"),
    "体魄": ("宜动，出出汗比闷着强", "身子还撑得住，撑不住就停"),
    "见识": ("会遇到点新鲜事，记下来", "看得清处境，就不算全输"),
}

# 差运时的"所短"：与其让人"多做"，不如让人"少错"
DIM_WARN = {
    "才学": "别急着下判断，先弄清楚再说",
    "财货": "看紧钱包，今日出的多入的少",
    "情缘": "今日宜静，多说一句多一分错",
    "体魄": "早些歇息，别硬扛",
    "见识": "今日宜听，少表态",
}

# 期限那句：每张签各有一句，不与签诗重复，也不与同吉凶的其它签重复
DEADLINE = {
    "破城": "七日照旧在走，只是这一次走在前面的是你",
    "满库": "七天的口子开着，收得住才算你的",
    "灯下人": "灯还亮着，人也还在，别把话留到灯灭",
    "月升": "路能走通，绕的那两处不算白绕",
    "集市晴": "好价就在今日，过了今日就换人来谈",
    "断绳": "断得干脆，比断得漂亮要紧",
    "灯下": "一尺的光也是光，先照着眼前",
    "苦尽": "磨出来的钝刀，也能开个头",
    "集市日": "不必被谁记住，也不必记住谁",
    "旧账": "算不清就先放着，别凭猜想去结",
    "黑街雨": "站得稳比走得快重要",
    "空匣": "付过的钱要不回来，那就别再多付一次",
    "逆风": "船不动不是船的事，等风换了再走",
    "刀刃": "少去一处，少说一句",
    "折卡之日": "先了手里这张，再谈下一张",
    "沙上字": "风还没起，你想它剩下几个",
}

YI = {
    "好": ["去书店翻两页旧书", "与旧友说一句真话", "把拖延已久的事做掉",
           "去集市走一趟，看看价", "把心事写下来，写完就烧", "赴一场约，别迟到"],
    "中": ["读数页旧书", "整理旧物，清一清抽屉", "把该问的问清楚",
           "早睡，别听墙根的话", "把欠下的人情还一点"],
    "坏": ["守成，别开新局", "把手里的事做完，不加新的",
           "把话说短", "把账理清，钱的事别含糊"],
}

JI = {
    "好": ["信来路不明的传言", "在朝堂上多嘴", "把好牌一次打完", "口头应承下做不到的事"],
    "中": ["与人争一时之短长", "深夜独行黑街", "把话说到一半便收住", "翻旧账"],
    "坏": ["在期限之前自乱阵脚", "赌一把翻本", "惊动不该惊动的人", "把刀刃朝向自己人"],
}


def tone_group(key: str) -> str:
    if key in ("大吉", "吉", "小吉"):
        return "好"
    if key in ("小凶", "凶"):
        return "坏"
    return "中"


@fortune.handle()
async def _(event: GroupMessageEvent | PrivateMessageEvent):
    today = datetime.date.today()
    # (QQ + 日期) 做种子 —— 同一人当天固定同一签，抽不到第二次
    rand = random.Random(f"azar-{today.isoformat()}-{event.user_id}")

    # 16 张签等概率：先抽签，再由签上的牌面推品级与卡类
    sign = rand.choice(SIGNS)
    card = CARD_TYPES[sign["card"]]
    tier = TIERS[sign["tier"]]
    group = tone_group(sign["key"])

    # ---- 五维：以 55 为中线，签位定偏移，卡面调倾向，品级再微调 ----
    # 签位带来的偏移打六折：直接叠上去的话，大吉签五维会一起顶到天花板，
    # 全都一样就没法分"所长/所短"了。折一半，高分签也能有高有低。
    scores = {}
    for dim in DIMENSIONS:
        v = (55 + (sign["score"] - 55) * 0.6
             + rand.randint(-13, 13)
             + tier["offset"]
             + card["bias"].get(dim, 0))
        scores[dim] = max(26, min(92, round(v)))

    avg = sum(scores.values()) / len(scores)
    # 签力直接取五维均分 —— 不再和 sign["score"] 二次混合，
    # 否则两头会被拉回中线，大吉和凶显示出来差不多

    top_dim = max(scores, key=scores.get)
    low_dim = min(scores, key=scores.get)
    # 保底：五维偶尔会一起顶到上限，导致所长和所短撞到同一维。
    # 这时把所短换成次低的那一维，绝不让一句话自相矛盾。
    if low_dim == top_dim:
        others = [d for d in DIMENSIONS if d != top_dim]
        low_dim = min(others, key=lambda d: scores[d])
    if group == "坏":
        top_label = "可恃"
        top_adv = DIM_ADVICE[top_dim][1]
    else:
        top_label = "所长"
        top_adv = DIM_ADVICE[top_dim][0]
    low_adv = DIM_WARN[low_dim]

    deadline = DEADLINE[sign["name"]]
    yi = rand.choice(YI[group])
    ji = rand.choice(JI[group])

    user = event.sender.card or event.sender.nickname
    score_line = "　".join(f"{d} {scores[d]}" for d in DIMENSIONS[:3])
    score_line2 = "　".join(f"{d} {scores[d]}" for d in DIMENSIONS[3:])

    msg = (
        f"📜 命书 · 已翻\n"
        f"─────────────\n"
        f"{user}　{today.strftime('%m月%d日')}\n\n"
        f"{sign['mark']}{sign['key']}　「{sign['name']}」\n"
        f"{sign['tag']}\n\n"
        f"{tier['mark']}{tier['name']}品 · {card['mark']}{sign['card']}卡"
        f" · 应在{card['realm']}　签力 {int(avg)}\n\n"
        f"{sign['line']}\n"
        f"{sign['note']}\n\n"
        f"五维　{score_line}\n"
        f"　　　{score_line2}\n\n"
        f"{top_label}　{top_dim} — {top_adv}\n"
        f"所短　{low_dim} — {low_adv}\n\n"
        f"{deadline}\n"
        f"宜　{yi}\n"
        f"忌　{ji}"
    )

    _log_draw(event.user_id, sign)
    card_png = _render_card(sign, card, tier, scores, avg, user, today.strftime('%m月%d日'))
    if card_png:
        # 卡牌图在上，文字签全文在下（图收藏，字细读）
        await fortune.finish(Message([
            MessageSegment.image("base64://" + _b64.b64encode(card_png).decode()),
            MessageSegment.text("\n" + msg),
        ]))
    await fortune.finish(Message(msg))
