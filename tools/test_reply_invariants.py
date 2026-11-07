#!/usr/bin/env python3
"""回复链不变量验收（部署 ai_chat.py 前必跑，不绿不部署）。

背景（坑55~58，2026-11-07 两次被用户抓包后固化）：
改动回复链（prompt/后处理/洗词）时，只测"能不能修坏输出"不够——
必须同时测"会不会把好输出改坏"。本脚本对洗词与检测逻辑跑不变量断言，
任何一条挂了就以非零码退出，阻断部署。

不变量：
  I1 洗词后绝不产生称呼混用（您+单独的你同句）
  I2 洗词后绝无 emoji 尾巴
  I3 洗词后绝无复述的 [表情:/[表情包: 标记
  I4 洗词后绝无"表情"字样（对模型的铁律，代码自己先不许违反）
  I5 检测表对已知坏句型（解说腔各变体）全命中
"""
import re, sys
from pathlib import Path

src = Path(__file__).resolve().parent.parent / "bot" / "plugins" / "ai_chat.py"
code = src.read_text(encoding="utf-8")

# 抽出需要验收的定义（不依赖 nonebot）
ns = {"re": re}
for name in ["_EMOJI_TAIL_RE", "_EMOJI_LAZY_RE"]:
    m = re.search(rf"^{name} = .*$", code, re.M)
    assert m, f"找不到 {name}——ai_chat.py 结构变了，本工具需同步更新"
    exec(m.group(0), ns)
m = re.search(r"def _emoji_scrub.*?(?=\n# |\n\S)", code, re.S)
assert m, "找不到 _emoji_scrub"
exec(m.group(0), ns)
scrub = ns["_emoji_scrub"]

def mixed(a):
    return bool("您" in a and re.search(r"你(?!们)", a))

FAILS = []
def check(inv, cond, detail):
    print(("PASS" if cond else "FAIL"), inv, "—", detail)
    if not cond:
        FAILS.append(inv)

fixtures = [
    # (模型典型输出, 说明)
    ("嚯，您这动作，像是闻到了什么稀罕事似的。", "您开头+像是"),
    ("您这表情，似乎在说「故事先放一放」。📖🤔", "您开头+解说+emoji尾"),
    ("得，这动作，真是绝了。", "无称呼+这动作"),
    ("你这表情真逗", "你开头（应归一到您）"),
    ("[表情:略] 这表情，似乎在说「略」。📚😕", "复述标记"),
    ("哟，您这一出，可比昨天的戏还热闹。", "好句不该被改坏"),
    ("行了行了，您怕是要把书都笑掉了。", "好句不该被改坏"),
]
for text, note in fixtures:
    out = scrub(text, "略")
    check("I1", not mixed(out), f"{note} → {out[:24]}")
    check("I2", not ns["_EMOJI_TAIL_RE"].search(out), f"{note}")
    check("I3", not re.match(r"^\s*\[", out), f"{note}")
    check("I4", "表情" not in out, f"{note}")

bad_patterns = [
    "这表情，像是在说「…」", "这表情，似乎在说「…」", "这表情，仿佛在说「…」",
    "这个表情展示了…", "这表情传达了…", "您的提问如同夜空", "像是闻到了什么",
]
for p in bad_patterns:
    check("I5", bool(ns["_EMOJI_LAZY_RE"].search(p)), f"检测表命中: {p}")

print(f"\n{'全部通过 ✅' if not FAILS else '未过: ' + ', '.join(sorted(set(FAILS))) + ' ❌'}")
sys.exit(1 if FAILS else 0)
