#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/golden_prompt.py —— _prompt 输出的黄金回归测试。

用途：memory.py 纯搬迁批次（以及以后任何改 Prompt 拼装的改动）的
**行为不变证明**。用合成语料（无真实聊天数据）构造一台机器人，
对 6 个典型场景调用 _prompt，比对输出 SHA256。

  搬迁前跑一次 → 把哈希填进 EXPECTED → 搬迁后再跑 → 必须全绿。
  任何一行 Prompt 拼装的变化都会在这里现形。

用法：
    python tools/golden_prompt.py            # 比对 EXPECTED
    python tools/golden_prompt.py --print    # 只打印当前哈希（换基线用）
"""

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import time
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.chdir(os.path.dirname(HERE))          # 让 qq_autoreply 的 BASE 落在仓库根

import qq_autoreply as qa                  # noqa: E402

qa.log = lambda *a, **k: None              # 别污染 qq_autoreply.log
try:                                       # 搬迁后记忆模块有自己的 log
    import memory as _mem_mod              # noqa: E402
    _mem_mod.log = lambda *a, **k: None
except Exception:
    pass
# Golden 只测 Prompt 拼装本身；向量检索层（会按语义命中改 Prompt 内容）
# 由 _smoke 专项覆盖，这里显式关掉避免异步构建的竞态
try:
    import memory as _mem_mod2             # noqa: E402
    _mem_mod2._mv = None
except Exception:
    pass

# ---------------------------------------------------------------- 合成语料
# 全部是编造内容，不含任何真实聊天数据，可安全进公开仓库。

FIXTURE = {
    "private_111.md": (
        "# QQ 私聊归档 - 111\n\n> fixture\n\n"
        "[2026-10-20 10:00:00] 我: 早上好呀\n"
        "[2026-10-20 10:00:05] 测试甲(111): 早上好，今天天气不错\n"
        "[2026-10-20 10:01:00] 我: 是啊，适合出去走走\n"
        "[2026-10-20 10:01:30] 测试甲(111): 晚上一起吃饭吗\n"
        "[2026-10-20 10:02:00] 我: 好啊，老地方见\n"
    ),
    "group_222.md": (
        "# QQ 群消息归档 - 222\n\n> fixture\n\n"
        "[2026-10-20 11:00:00] 测试乙(333): 有人在吗\n"
        "[2026-10-20 11:00:05] 测试丙(444): 在的\n"
        "[2026-10-20 11:01:00] 测试乙(333): @我 这个怎么弄\n"
        "[2026-10-20 11:01:30] 我: 我看看\n"
    ),
    "private_111_profile.md": (
        "# 关系档案 - 111\n\n> fixture\n\n"
        "- 手写：测试甲喜欢猫，不喜欢香菜\n"
        "- 自动：爱撒娇，管主人叫宝宝\n"
    ),
    "group_222_profile.md": (
        "# 关系档案 - 222\n\n> fixture\n\n"
        "- 手写：测试群，群规是不准发广告\n"
    ),
    "group_222_u333_profile.md": (
        "# 关系档案 - 群222 内的 333\n\n> fixture\n\n"
        "- 话痨，管主人叫宝贝，吃醋要哄\n"
    ),
    "private_111_summary.md": (
        "# 长期记忆摘要 - 111\n\n> fixture\n\n"
        "- 约好周末一起吃饭\n"
        "- 测试甲在准备考试\n"
    ),
    "private_111_stage.md": (
        "# 阶段回顾 - 111\n\n> fixture\n\n"
        "<!-- pos:5 -->\n\n"
        "## 2026-10-19 20:00\n- 第一阶段聊了考试安排\n"
    ),
    "self_style.md": (
        "# 说话风格档案\n\n> fixture\n\n"
        "- 喜欢用短句\n"
        "- 偶尔用颜文字\n"
    ),
}

RECENT_U = [
    {"mid": 101, "nick": "我", "uid": None, "text": "昨天睡得好吗", "at_me": False},
    {"mid": 102, "nick": "测试甲", "uid": 111, "text": "还行", "at_me": False},
    {"mid": 103, "nick": "测试甲", "uid": 111, "text": "就是做了个噩梦", "at_me": False},
    {"mid": 104, "nick": "测试甲", "uid": 111, "text": "梦见考试迟到了", "at_me": False},
]
RECENT_G = [
    {"mid": 201, "nick": "测试乙", "uid": 333, "text": "大家好呀", "at_me": False},
    {"mid": 202, "nick": "测试乙", "uid": 333, "text": "@我 求助个事", "at_me": True},
    {"mid": None, "nick": "我", "uid": 999, "text": "说吧", "at_me": False},
]


def build_bot(tmp):
    for name, text in FIXTURE.items():
        with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
            f.write(text)
    cfg = qa.load_config()
    cfg["archive"]["dir"] = tmp
    cfg["reply_images"]["enabled"] = False
    bot = qa.AutoReply(cfg)
    bot.self_id = 999
    # 加载各层（兼容搬迁前后：搬迁后在 bot.mem 上）
    m = getattr(bot, "mem", bot)
    if hasattr(m, "load_all"):
        m.load_all()
    else:
        bot._restore_context()
        bot._load_summaries()
        bot._load_stages()
        bot._load_profiles()
        bot._load_style()
        bot._load_samples()
    # 手工注入 recent / mood（保证上下文精确可控）
    m.recent["u111"] = deque(RECENT_U, maxlen=int(cfg["context_messages"]))
    m.recent["g222"] = deque(RECENT_G, maxlen=int(cfg["context_messages"]))
    m.mood["u111"] = deque(["愉快", "平静", "求助"], maxlen=6)
    bot.kaomoji_hist["u111"] = deque(["_(:3」∠)_"], maxlen=6)
    return bot


def scenarios(bot):
    """返回 [(场景名, prompt 文本)]。"""
    out = []
    out.append(("私聊+画像", bot._prompt("u111", "在吗", uid=111)[0]))
    out.append(("群+个人档案+群档案", bot._prompt("g222", "在吗", uid=333)[0]))
    out.append(("群-无uid", bot._prompt("g222", "在吗")[0]))
    out.append(("私聊-裸@", bot._prompt("u111", "", bare=True, uid=111)[0]))
    out.append(("私聊-带图", bot._prompt("u111", "看这图", has_image=True,
                                        uid=111)[0]))
    out.append(("私聊-上下文排除", bot._prompt("u111", "在吗", mid=102,
                                              excludes={103}, uid=111)[0]))
    # 离线 / 启动补回：prompt 里带「距今多久」，让模型自己判断口吻
    out.append(("私聊-离线30分钟", bot._prompt("u111", "在吗", uid=111,
                                            stale=30 * 60)[0]))
    out.append(("私聊-补回2天前", bot._prompt("u111", "在吗", uid=111,
                                            stale=2 * 86400 + 3600,
                                            replay=True)[0]))
    # 旧消息要带「N 天前」标记——不然模型分不清「三天前说的」和「刚说的」，
    # 于是抓着老话题不放（群里真踩过：隔了一天追问"你买的到底是哪把雷霆"）
    m = getattr(bot, "mem", bot)
    old = dict(RECENT_G[-1])
    old["text"] = "三天前的话题"
    old["ts"] = time.time() - 3 * 86400
    m.recent["g222"] = deque(list(RECENT_G) + [old],
                             maxlen=int(bot.cfg["context_messages"]))
    out.append(("群-旧消息带时间标记", bot._prompt("g222", "在吗", uid=333)[0]))
    return out


# 基线哈希（2026-10-23 搬迁前冻结；改动 Prompt 拼装必须有意识地更新这里）
EXPECTED = {
    "私聊+画像": "f280b5bbc11a96e5",
    "群+个人档案+群档案": "09435d7bd3d17c32",
    "群-无uid": "a23e7ae6101e2ff0",
    "私聊-裸@": "2f2a0677f28c97e5",
    "私聊-带图": "9fd8a6201b046f8f",
    "私聊-上下文排除": "bcaad43478e0a155",
    "私聊-离线30分钟": "86a2c3df2a9f2a08",
    "私聊-补回2天前": "d7e93a65d70742c5",
    "群-旧消息带时间标记": "5d140296a7d4da9e",
}

# human_age 的用例（秒 -> 期望文案），顺手钉住时间颗粒度
AGE_CASES = [
    (30, "不到 1 分钟"),
    (60, "1 分钟"),
    (25 * 60, "25 分钟"),
    (2 * 3600 + 15 * 60, "2 小时 15 分钟"),
    (9 * 3600, "9 小时"),
    (26 * 3600, "1 天 2 小时"),
    (5 * 86400, "5 天"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", help="只打印当前哈希")
    args = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="qqgolden_")
    try:
        bot = build_bot(tmp)
        rows = scenarios(bot)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok = True
    print("=" * 60)
    for name, text in rows:
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        exp = EXPECTED.get(name, "")
        if args.print:
            print("%-14s %s" % (name, h))
            continue
        if not exp:
            print("%-14s %s  （EXPECTED 未设置）" % (name, h))
            ok = False
            continue
        mark = "OK  " if h == exp else "DIFF"
        if h != exp:
            ok = False
        print("%-14s %s  %s" % (name, h, mark))
    # 行为断言：旧消息必须带时间标记，且提示词要交代「别翻旧账」
    texts = dict(rows)
    marked = texts.get("群-旧消息带时间标记", "")
    if "（3 天前）" not in marked:
        print("行为断言：旧消息没带「3 天前」标记")
        ok = False
    else:
        print("行为断言：旧消息带「3 天前」标记              OK")
    if "别翻旧账追问" not in marked:
        print("行为断言：提示词没交代「别翻旧账」")
        ok = False
    else:
        print("行为断言：提示词交代「别翻旧账」              OK")
    print("=" * 60)
    for sec, want in AGE_CASES:
        got = qa.human_age(sec)
        mark = "OK  " if got == want else "DIFF"
        if got != want:
            ok = False
        print("human_age(%-8d) %-14s %s" % (sec, got, mark))
    print("=" * 60)
    if args.print:
        print("把上面的哈希填进 EXPECTED 即可作为回归基线")
        return 0
    if ok:
        print("golden 全过：Prompt 拼装行为未变")
        return 0
    print("golden 失败：Prompt 输出与基线不一致")
    return 1


if __name__ == "__main__":
    sys.exit(main())
