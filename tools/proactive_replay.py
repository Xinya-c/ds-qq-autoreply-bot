#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/proactive_replay.py —— 群聊主动搭话的回放式测试。

不联网、不写真实数据：用 golden_prompt 的合成语料造一台机器人，
把「攒条数 → 安排 → 生成 → 发送 / 跳过 / 失败」整条路走一遍，
断言计数、冷却、每小时上限、发送内容和 prompt 拼装。

  python tools/proactive_replay.py
"""

import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
os.chdir(os.path.dirname(HERE))          # 让 BASE 落在仓库根

import golden_prompt as G                # noqa: E402
import qq_autoreply as qa                # noqa: E402

qa.log = lambda *a, **k: None            # 别污染 qq_autoreply.log

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("%-38s %s %s" % (name, "OK  " if cond else "FAIL", detail))


class OB:
    """OneBot 桩：记录群消息，不真发。"""

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def call(self, action, params):
        r = {"ok": not self.fail}
        if action == "send_group_msg":
            self.sent.append(params)
        return r


def new_bot(recent, **cfg_over):
    """临时目录 + 合成语料 + 可控的群上下文。"""
    tmp = tempfile.mkdtemp(prefix="qqpro_")
    bot = G.build_bot(tmp)
    bot.cfg["allowed_groups"] = [222]
    bot.cfg["reply_images"]["enabled"] = False
    bot.cfg.update(cfg_over)
    bot.mem.recent["g222"] = list(recent)
    bot.pmsg["g222"] = 0
    return bot, tmp


GROUP_CHAT = [
    {"mid": 1, "nick": "测试乙", "uid": 333, "text": "周末有人去爬山吗", "at_me": False},
    {"mid": 2, "nick": "测试丙", "uid": 444, "text": "我我我，算我一个", "at_me": False},
    {"mid": 3, "nick": "测试乙", "uid": 333, "text": "那早上八点集合？", "at_me": False},
    {"mid": 4, "nick": "测试丁", "uid": 555, "text": "太早了，九点吧", "at_me": False},
    {"mid": 5, "nick": "测试丙", "uid": 444, "text": "九点可以，我带水", "at_me": False},
]


def note_n(bot, n):
    """模拟群里来了 n 条别人的消息。"""
    for _ in range(n):
        bot._proactive_note("g222")


# ---------------------------------------------------------------- 1. 攒条数
def test_counting():
    bot, tmp = new_bot(GROUP_CHAT)
    try:
        captured = []
        bot._enqueue = lambda conv, evt: captured.append((conv, evt))
        bot.ptarget["g222"] = 3                 # 固定目标，别跟随机较劲
        note_n(bot, 2)
        check("差 1 条不入队", len(captured) == 0, "pmsg=%d" % bot.pmsg.get("g222", 0))
        note_n(bot, 1)
        check("攒满入队一次", len(captured) == 1)
        check("入队的是主动事件", captured and captured[0][1].get("_proactive") is True)
        check("入队到对的会话", captured and captured[0][0] == "g222")
        check("群号正确", captured and captured[0][1].get("group_id") == 222)
        check("入队后计数清零", bot.pmsg.get("g222") == 0)
        check("入队后重抽目标", bot.ptarget.get("g222") != 3)
        # 入队即按下冷却：模型再慢也不会连着安排第二次
        note_n(bot, 3)
        check("冷却期内不重复入队", len(captured) == 1,
              "再来 3 条也不发（防排队连发）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 2. 每小时上限
def test_hourly_cap():
    """上限只数「真的开口了」的次数，所以要把入队→发送整条路走完。"""
    bot, tmp = new_bot(GROUP_CHAT)
    try:
        bot.cfg["proactive"] = {"enabled": True, "min_msgs": 2, "max_msgs": 2,
                                "cooldown_sec": 0, "max_per_hour": 2,
                                "context_msgs": 12}
        bot.ob = OB()
        bot.llm_reply = lambda conv, text, *a, **kw: "加我一个"
        enqueued = []
        bot._enqueue = lambda conv, evt: enqueued.append(evt)
        for _ in range(4):
            bot.plast["g222"] = 0               # 手动放行冷却，只测每小时上限
            note_n(bot, 2)
            if enqueued:
                bot._handle(enqueued.pop())
        check("每小时上限拦住第 3 次", len(enqueued) == 0 and len(bot.ob.sent) == 2,
              "cap=2，实发 %d 条" % len(bot.ob.sent))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 3. 完整发送
def test_send():
    bot, tmp = new_bot(GROUP_CHAT)
    try:
        bot.ob = OB()
        bot.llm_reply = lambda conv, text, *a, **kw: "这个我带烤串去，够不够"
        captured = []
        # 只捕获，不真的入队——否则 _conv_worker 会自己再跑一遍 _handle，
        # 同一条主动消息就被发两次
        bot._enqueue = lambda conv, evt: captured.append((conv, evt))
        bot.ptarget["g222"] = 2
        note_n(bot, 2)
        bot._handle(captured[0][1])
        check("主动发言只发一次", len(bot.ob.sent) == 1, "sent=%d" % len(bot.ob.sent))
        seg = bot.ob.sent[0] if bot.ob.sent else {}
        msg = seg.get("message")
        check("发到正确的群", seg.get("group_id") == 222)
        check("不带 @（自然搭话）",
              not any(isinstance(s, dict) and s.get("type") == "at" for s in msg))
        check("内容就是模型那句", "烤串" in str(msg))
        check("计入每小时上限", len(bot.phour.get("g222") or []) == 1)
        check("计数已清零", bot.pmsg.get("g222") == 0)
        # 归档里要留下这句，风格提炼才吃得到
        ap = os.path.join(bot.mem.archive_dir(), "group_222.md")
        arch = open(ap, encoding="utf-8", errors="replace").read() \
            if os.path.exists(ap) else ""
        check("写进群归档", "烤串" in arch)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 4. 模型选择不插话
def test_skip():
    bot, tmp = new_bot(GROUP_CHAT)
    try:
        bot.ob = OB()
        bot.llm_reply = lambda conv, text, *a, **kw: "跳过"
        evt = {"post_type": "message", "message_type": "group", "group_id": 222,
               "user_id": None, "message_id": "p1", "time": int(time.time()),
               "message": [], "sender": {}, "_proactive": True}
        bot._handle(evt)
        check("跳过时什么都不发", len(bot.ob.sent) == 0)
        check("跳过也计入上限", len(bot.phour.get("g222") or []) == 1)
        for t in ("跳过", "跳过。", "（跳过）", " 跳 过 ", "跳过！", "沉默"):
            check("_is_skip(%r)" % t, bot._is_skip(t))
        for t in ("跳过这个话题我们聊点别的", "我有点想跳过这个话题但是不行"):
            check("_is_skip(%r) 应为假" % t, not bot._is_skip(t))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 5. 生成失败
def test_failure():
    bot, tmp = new_bot(GROUP_CHAT)
    try:
        bot.ob = OB()
        bot.llm_reply = lambda conv, text, **kw: None      # CLI 挂了
        evt = {"post_type": "message", "message_type": "group", "group_id": 222,
               "user_id": None, "message_id": "p1", "time": int(time.time()),
               "message": [], "sender": {}, "_proactive": True}
        bot._handle(evt)
        check("没回复就不发", len(bot.ob.sent) == 0)
        check("失败不占每小时上限", not (bot.phour.get("g222") or []))
        check("失败也清零重新攒", bot.pmsg.get("g222") == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 6. 发送失败
def test_send_failure():
    bot, tmp = new_bot(GROUP_CHAT)
    try:
        bot.ob = OB(fail=True)
        bot.llm_reply = lambda conv, text, *a, **kw: "我来了我来了"
        evt = {"post_type": "message", "message_type": "group", "group_id": 222,
               "user_id": None, "message_id": "p1", "time": int(time.time()),
               "message": [], "sender": {}, "_proactive": True}
        bot._handle(evt)
        ap = os.path.join(bot.mem.archive_dir(), "group_222.md")
        arch = open(ap, encoding="utf-8", errors="replace").read() \
            if os.path.exists(ap) else ""
        check("发送失败不进归档", "我来了" not in arch)
        check("没发出去", len(bot.ob.sent) == 1)   # 调过发送接口，只是对端返回失败
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 7. prompt 拼装
def test_prompt():
    bot, tmp = new_bot(GROUP_CHAT)
    try:
        bot.kaomoji_hist["g222"] = ["_(:3」∠)_"]
        ptext = "\n".join(bot._p_lines("g222", 12))
        p, hist = bot._prompt("g222", ptext, proactive=True)
        check("带人设", "大肥鱼" in p)
        check("带安全规则", "安全规则" in p)
        check("带群档案", "群规" in p)
        check("带说话风格", "喜欢用短句" in p)
        check("带颜文字避让", "颜文字" in p)
        check("带群聊记录", "周末有人去爬山吗" in p)
        check("带接话指令", "跳过" in p and "别总结" in p)
        check("没有「对方刚说」", "对方刚说" not in p and "对方之前说" not in p)
        check("返回了 hist", hist is not None)
        check("普通 prompt 不受影响",
              "大肥鱼" in bot._prompt("g222", "在吗", uid=333)[0])
        check("普通 prompt 没有接话指令", "别总结" not in bot._prompt("g222", "在吗",
                                                             uid=333)[0])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 8. 被动回复重置
def test_passive_reset():
    bot, tmp = new_bot(GROUP_CHAT)
    try:
        bot.ob = OB()
        bot.llm_reply = lambda conv, text, *a, **kw: "好呀"
        bot.pmsg["g222"] = 7
        bot.plast["g222"] = 0.0
        bot.phour["g222"] = [time.time()]          # 假装这小时已经主动过一次
        evt = {"post_type": "message", "message_type": "group", "group_id": 222,
               "user_id": 333, "message_id": "m9", "time": int(time.time()),
               "message": [{"type": "at", "data": {"qq": "999"}},
                           {"type": "text", "data": {"text": " 带我一个"}}],
               "sender": {"nickname": "测试乙", "card": "", "user_id": 333}}
        bot._handle(evt)
        check("被 @ 回复后计数清零", bot.pmsg.get("g222") == 0)
        check("被动回复不占主动配额", len(bot.phour.get("g222") or []) == 1)
        check("被动回复也按下冷却", bot.plast.get("g222", 0) > 0)
        check("回复发出去了", len(bot.ob.sent) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 9. 开关
def test_toggles():
    bot, tmp = new_bot(GROUP_CHAT)
    try:
        captured = []
        bot._enqueue = lambda conv, evt: captured.append(evt)
        bot.ptarget["g222"] = 2
        bot.cfg["proactive"] = dict(bot.cfg["proactive"], enabled=False)
        note_n(bot, 5)
        check("enabled=False 完全静默", len(captured) == 0)

        bot.cfg["proactive"] = dict(bot.cfg["proactive"], enabled=True)
        bot.cfg["reply_in_group_when_at"] = False
        note_n(bot, 5)
        check("群回复总开关关掉时也不主动", len(captured) == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 10. 真实入口接线
def test_on_event_wiring():
    """走 on_event（而不是直接调 _proactive_note），确认真消息能攒上数。"""
    def mk(mid, gid=222, uid=333):
        return {"post_type": "message", "message_type": "group", "group_id": gid,
                "user_id": uid, "message_id": mid, "time": int(time.time()),
                "message": [{"type": "text", "data": {"text": "群里唠两句"}}],
                "sender": {"nickname": "测试乙", "card": "", "user_id": uid}}

    bot, tmp = new_bot(GROUP_CHAT)
    try:
        captured = []
        bot._enqueue = lambda conv, evt: captured.append(evt)
        bot.ptarget["g222"] = 2
        bot.on_event(mk("w1"))
        bot.on_event(mk("w2"))
        pros = [e for e in captured if e.get("_proactive")]
        check("on_event 能攒够并安排", len(pros) == 1, "主动事件 %d 个" % len(pros))

        # 白名单外的群不计
        captured.clear()
        bot.ptarget["g222"] = 2
        bot.on_event(mk("w3", gid=777))
        bot.on_event(mk("w4", gid=777))
        check("白名单外的群不计数", not [e for e in captured if e.get("_proactive")])

        # 自己的消息不计（on_event 内部就拦了）
        captured.clear()
        bot.ptarget["g222"] = 2
        bot.on_event(mk("w5", uid=999))
        bot.on_event(mk("w6", uid=999))
        check("自己的消息不计数", not [e for e in captured if e.get("_proactive")])

        # 补回的旧消息不计（不拿历史凑数）
        captured.clear()
        bot.ptarget["g222"] = 2
        for i in range(4):
            e = mk("w7-%d" % i)
            e["_replay"] = True
            bot.on_event(e)
        check("补回的旧消息不计数", not [e for e in captured if e.get("_proactive")])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 11. 直连 API 后端
def test_api_backend():
    """切成 deepseek 直连后端，主动接话的指令不能丢。"""
    bot, tmp = new_bot(GROUP_CHAT)
    try:
        bot.cfg["backend"] = "deepseek"
        bot.api_key = "x"
        bot.ob = OB()
        cap = {}

        def fake_post(msgs, conv, utxt, hist):
            cap["msgs"], cap["utxt"] = msgs, utxt
            return "也算我一个"

        bot._api_post = fake_post
        evt = {"post_type": "message", "message_type": "group", "group_id": 222,
               "user_id": None, "message_id": "p1", "time": int(time.time()),
               "message": [], "sender": {}, "_proactive": True}
        bot._handle(evt)
        msgs = cap.get("msgs") or []
        sysm = msgs[0]["content"] if msgs else ""
        check("API 后端 system 段带人设", "大肥鱼" in sysm)
        check("API 后端带群档案", "群规" in sysm)
        check("API 后端带群聊记录", "爬山" in (cap.get("utxt") or ""))
        check("API 后端 user 段带接话指令",
              "跳过" in (msgs[-1]["content"] if msgs else ""))
        check("API 后端能发出去", len(bot.ob.sent) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("=" * 66)
    for fn in (test_counting, test_hourly_cap, test_send, test_skip, test_failure,
               test_send_failure, test_prompt, test_passive_reset, test_toggles,
               test_on_event_wiring, test_api_backend):
        print("--- %s ---" % fn.__name__)
        fn()
    print("=" * 66)
    bad = [n for n, ok, _ in RESULTS if not ok]
    print("共 %d 项，失败 %d 项" % (len(RESULTS), len(bad)))
    if bad:
        for n in bad:
            print("  FAIL: %s" % n)
        return 1
    print("主动搭话回放全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
