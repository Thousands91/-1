"""
店内游戏 —— 说书人的消遣

命令（一律要 @ 阿萨尔，防裸词误触）：
- @我 掷骰       → 阿萨尔掷三把骰子，报点数
- @我 决斗 @某人  → 你和对方各掷三把，点数大者胜
- @我 猜数字      → 开一局：猜 1~100 里阿萨尔心里想的数
- @我 猜 50 / 猜一百 → 出手（本局进行中才认）
- @我 不玩了      → 认输，公布答案
- @我 谜语        → 说书人出一个谜面
- @我 答 xxx      → 抢答（谜面挂着时才认）
- @我 揭晓        → 没人猜中时公布答案

设计说明：
- 猜数字按群分局，同一群同时只有一局，谁开谁出题的就是机器人自己
- 谜语同一时间每群只挂一个，答对即收
- 全部状态存内存，重启清零——输赢是消遣，不值得持久化
"""
import random
import re

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    PrivateMessageEvent,
)
from nonebot.params import CommandArg
from nonebot.rule import to_me

# 所有游戏一律 to_me：群聊必须 @ 阿萨尔才触发（私聊天然算）。
# 否则"谜语""猜数字"这类裸词，群友闲聊提到就会误触发。

# ============================================================
# 掷骰 / 决斗
# ============================================================

dice_cmd = on_command("掷骰", aliases={"骰子", "掷骰子", "决斗"},
                      rule=to_me(), priority=20, block=True)

DICE_FLAVOR_WIN = [
    "点子正，手气也好。",
    "这一把，命运站在你这边。",
    "好点子。拿去，别赌大了。",
]
DICE_FLAVOR_LOSE = [
    "手气背，认了吧。",
    "骰子不听话，跟手艺无关。",
    "这把输了。回头翻翻册子，看看几时转运。",
]


@dice_cmd.handle()
async def _(event: GroupMessageEvent | PrivateMessageEvent):
    # 决斗：消息里有 @ 别人就变成对决
    ats = [seg.data.get("qq") for seg in event.get_message()
           if seg.type == "at" and str(seg.data.get("qq", "")) != "all"]
    me = str(event.self_id)

    def roll3() -> int:
        return random.randint(1, 6) + random.randint(1, 6) + random.randint(1, 6)

    if ats:
        target = str(ats[0])
        if target == me:
            await dice_cmd.finish("跟我决斗？我的骰子只听我自己的。")
        a, b = roll3(), roll3()
        name = "对方"
        if a > b:
            await dice_cmd.finish(
                f"🎲 你 {a} 点，对方 {b} 点——你赢了。\n{random.choice(DICE_FLAVOR_WIN)}"
            )
        if a < b:
            await dice_cmd.finish(
                f"🎲 你 {a} 点，对方 {b} 点——你输了。\n{random.choice(DICE_FLAVOR_LOSE)}"
            )
        await dice_cmd.finish(f"🎲 {a} 对 {b}，平手。再来一把？")

    # 没点 @，就是自己掷一把图个彩头
    a = roll3()
    await dice_cmd.finish(f"🎲 三把骰子，共 {a} 点。\n{random.choice(DICE_FLAVOR_WIN)}")


# ============================================================
# 猜数字
# ============================================================

guess_start = on_command("猜数字", aliases={"猜数"}, rule=to_me(), priority=20, block=True)
# 出手用 on_message 抓，因为"猜 50"不是固定命令词。
# 必须用 block=False：这两个匹配器对每条消息都会触发，
# block=True 会把没命中的消息也拦下来，关键词回复和 AI 就全哑了。
guess_do = on_message(rule=to_me(), priority=30, block=False)

# {group_id: {"answer": int, "count": int}}
GUESS_GAMES: dict[int, dict] = {}


@guess_start.handle()
async def _(event: GroupMessageEvent | PrivateMessageEvent):
    gid = getattr(event, "group_id", 0)
    answer = random.randint(1, 100)
    GUESS_GAMES[gid] = {"answer": answer, "count": 0}
    await guess_start.finish(
        "好，我心里想了一个 1 到 100 的数。发「猜 数字」来试——"
        "我说大了小了，你慢慢逼近。认输就发「不玩了」。"
    )


def _parse_guess(text: str) -> int | None:
    """认「猜 50」「猜五十」这类写法；不是出手的写法返回 None。"""
    t = text.strip().lower()
    m = re.match(r"^猜\s*([0-9]+)$", t)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 100 else None
    # 中文数字：只处理常见的，够用
    cn = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
          "八": 8, "九": 9, "十": 10, "百": 100, "零": 0}
    m = re.match(r"^猜\s*([一二两三四五六七八九十百零]+)$", t)
    if not m:
        return None
    s = m.group(1)
    try:
        # 十位结构：X十Y / 十Y / X十 / 整数
        if s in cn and s in ("十", "百"):
            return cn[s]
        m2 = re.match(r"^([一二两三四五六七八九]?)十([一二三四五六七八九]?)$", s)
        if m2:
            tens = cn.get(m2.group(1), 1) if m2.group(1) else 1
            ones = cn.get(m2.group(2), 0) if m2.group(2) else 0
            return tens * 10 + ones
        m3 = re.match(r"^([一二两三四五六七八九])百([零一二三四五六七八九十]*)$", s)
        if m3:
            base = cn[m3.group(1)] * 100
            rest = m3.group(2)
            if not rest:
                return base
            if rest.startswith("零") and len(rest) == 2:
                return base + cn[rest[1]]
            m4 = re.match(r"^零?([一二三四五六七八九])?十?([一二三四五六七八九])?$", rest)
            if m4 and any(m4.groups()):
                tens = cn.get(m4.group(1), 0) if m4.group(1) else 0
                ones = cn.get(m4.group(2), 0) if m4.group(2) else 0
                return base + tens * 10 + ones
            return base
        if len(s) == 1:
            return cn[s]
    except Exception:
        return None
    return None


@guess_do.handle()
async def _(event: GroupMessageEvent | PrivateMessageEvent):
    gid = getattr(event, "group_id", 0)
    game = GUESS_GAMES.get(gid)
    if not game:
        return

    text = event.get_plaintext().strip()
    low = text.lower()

    # 认输
    if low in ("不玩了", "不猜了", "认输", "放弃"):
        del GUESS_GAMES[gid]
        await guess_do.finish(f"行。那个数是 {game['answer']}，下次手别抖。")

    n = _parse_guess(text)
    if n is None:
        return

    game["count"] += 1
    if n < game["answer"]:
        await guess_do.finish(f"{n}？小了。")
    if n > game["answer"]:
        await guess_do.finish(f"{n}？大了。")
    cnt = game["count"]
    del GUESS_GAMES[gid]
    tail = "一下就中，你也太会算了。" if cnt <= 3 else "中了。"
    await guess_do.finish(f"🎲 {n}，就是它！共猜了 {cnt} 次。{tail}")


# ============================================================
# 谜语
# ============================================================

riddle_cmd = on_command("谜语", aliases={"出谜", "猜谜", "说书"}, rule=to_me(), priority=20, block=True)
riddle_answer = on_message(rule=to_me(), priority=30, block=False)

# (谜面, 谜底, 提示)
RIDDLES = [
    ("千条线，万条线，掉进水里看不见。", "雨", "天上来的"),
    ("五个兄弟，住在一起，名字不同，高矮不齐。", "手指", "你身上就有"),
    ("有面没有口，有脚没有手，虽有四只脚，自己不会走。", "桌子", "店里就有"),
    ("小小诸葛亮，独坐军中帐，摆下八卦阵，专捉飞来将。", "蜘蛛", "结网的"),
    ("白天草里住，晚上空中游，金光闪闪动，小尾灯在头。", "萤火虫", "夜里发光"),
    ("一个黑孩，从不开口，要是开口，掉出舌头。", "瓜子", "零嘴"),
    ("人脱衣服，它穿衣服，人脱帽子，它戴帽子。", "衣架", "挂在墙边"),
    ("两姊妹，一样长，同滋味，共作坊，酸盆里泡，盐水里尝。", "蒜", "剥了才辣"),
    ("弟兄七八个，围着柱子坐，只要一分开，衣服就扯破。", "蒜", "上面那个近亲"),
    ("麻屋子，红帐子，里面住个白胖子。", "花生", "地里长的"),
    ("远看山有色，近听水无声，春去花还在，人来鸟不惊。", "画", "挂在墙上"),
    ("小时四条腿，长大两条腿，老了三条腿。", "人", "说的是你"),
    ("什么东西越洗越脏？", "水", "它替你脏"),
    ("什么门永远关不上？", "球门", "球场上的"),
    ("什么书在店里买不到？", "秘书", "是个称呼"),
    ("什么车寸步难行？", "风车", "靠风吹"),
    ("二三四五六七八九（打一成语）", "缺衣少食", "少了什么数什么"),
    ("一年四季都是春（打一城市）", "长春", "东北地区"),
    ("一口咬掉牛尾巴（打一字）", "告", "牛没尾巴再添一口"),
    ("半个月亮（打一字）", "胖", "月伴一半"),
    ("半真半假（打一字）", "值", "真假各取一半"),
    ("人不在其位（打一字）", "立", "位字去人"),
]

# {group_id: {"riddle": (面, 底, 提示), "hinted": bool}}
RIDDLE_GAMES: dict[int, dict] = {}


@riddle_cmd.handle()
async def _(event: GroupMessageEvent | PrivateMessageEvent):
    gid = getattr(event, "group_id", 0)
    if gid in RIDDLE_GAMES:
        face = RIDDLE_GAMES[gid]["riddle"][0]
        await riddle_cmd.finish(f"上一个还没人猜中呢：\n{face}\n\n发「答 xxx」抢答，「揭晓」看答案。")
    r = random.choice(RIDDLES)
    RIDDLE_GAMES[gid] = {"riddle": r, "hinted": False}
    await riddle_cmd.finish(f"📜 说书人出一题：\n{r[0]}\n\n发「答 xxx」抢答；卡住了发「提示」。")


@riddle_answer.handle()
async def _(event: GroupMessageEvent | PrivateMessageEvent):
    gid = getattr(event, "group_id", 0)
    game = RIDDLE_GAMES.get(gid)
    if not game:
        return

    text = event.get_plaintext().strip()

    # 提示
    if text in ("提示", "给点提示", "提示一下"):
        face, ans, hint = game["riddle"]
        if not hint:
            await riddle_answer.finish("这题没有提示，全凭本事。")
            return
        if game["hinted"]:
            await riddle_answer.finish(f"提示给过了：{hint}\n再想想，不行就发「揭晓」。")
            return
        game["hinted"] = True
        await riddle_answer.finish(f"提示：{hint}")

    # 揭晓
    elif text == "揭晓":
        face, ans, _ = game["riddle"]
        del RIDDLE_GAMES[gid]
        await riddle_answer.finish(f"谜底是「{ans}」。这一题记在你账上——下回接好。")

    # 抢答
    elif text.startswith(("答", "猜")):
        raw = text[1:].strip().strip("，。！？、,.!? ")
        _, ans, _ = game["riddle"]
        if raw and (raw in ans or ans in raw):
            del RIDDLE_GAMES[gid]
            await riddle_answer.finish(f"答对了，是「{ans}」。有点意思，您常来。")
        elif raw:
            await riddle_answer.finish(f"「{raw}」？不对。再想想——不行就发「提示」。")
