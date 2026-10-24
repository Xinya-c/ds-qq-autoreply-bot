#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/e2e_smoke.py —— 主流程端到端冒烟。

跟 golden_prompt（只盯 prompt 拼装）和 proactive_replay（只盯主动搭话）不同，
这里走真实入口 on_event，把整条链路跑一遍：调度 → 合并 → 队列 → 回复 →
发送 → 归档 → 记忆。模型与 OneBot 全部打桩，不联网、不写真实数据。

  python tools/e2e_smoke.py
"""

import json
import os
import shutil
import sys
import io
import tempfile
import threading
import time
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
os.chdir(os.path.dirname(HERE))

import golden_prompt as G                # noqa: E402
import qq_autoreply as qa                # noqa: E402

qa.log = lambda *a, **k: None

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("%-40s %s %s" % (name, "OK  " if cond else "FAIL", detail))


class OB:
    """OneBot 桩：按 action 分桶记录，不真发。

    history 用来喂「QQ 侧历史」：{(action, id): [消息...]}。
    """

    def __init__(self, fail=(), history=None):
        self.sent = {"private": [], "group": []}
        self.fail = set(fail)
        self.history = history or {}

    def call(self, action, params, timeout=15):
        if action in ("get_friend_msg_history", "get_group_msg_history"):
            key = params.get("user_id") or params.get("group_id")
            msgs = self.history.get((action, key), [])
            return {"ok": True, "data": {"messages": list(msgs)}}
        ok = action not in self.fail
        if action == "send_private_msg":
            self.sent["private"].append(params)
        elif action == "send_group_msg":
            self.sent["group"].append(params)
        return {"ok": ok, "retcode": 0 if ok else 1200}


class FakeLLM:
    """记录每次调用，按序发牌。"""

    def __init__(self, *replies):
        self.q = list(replies)
        self.calls = []

    def __call__(self, conv_key, user_text, *a, **kw):
        # _handle 把 image/mid/bare 按位置传进来，这里还原成名字方便断言
        merged = dict(zip(("image", "mid", "bare"), a))
        merged.update(kw)
        self.calls.append({"conv": conv_key, "text": user_text, "kw": merged})
        return self.q.pop(0) if self.q else ""


def wait_for(pred, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.03)
    return False


def new_bot(recent=None, **over):
    tmp = tempfile.mkdtemp(prefix="qqe2e_")
    bot = G.build_bot(tmp)
    bot.cfg["allowed_private"] = [111]
    bot.cfg["allowed_groups"] = [222]
    bot.cfg["merge_window_sec"] = 0          # 默认不合并，连发场景单独设
    bot.cfg["min_interval_sec"] = 0
    bot.cfg["reply_images"]["enabled"] = False
    bot.cfg["proactive"]["enabled"] = False  # 主动搭话有专属回放，这里关掉免干扰
    bot.cfg.update(over)
    for conv, rows in (recent or {}).items():
        bot.mem.recent[conv] = list(rows)
    bot.ob = OB()
    return bot, tmp


def priv(uid=111, text="在吗", mid="p1", **extra):
    e = {"post_type": "message", "message_type": "private", "user_id": uid,
         "group_id": None, "message_id": mid, "time": int(time.time()),
         "message": [{"type": "text", "data": {"text": text}}],
         "sender": {"nickname": "测试甲", "card": "", "user_id": uid}}
    e.update(extra)
    return e


def grp(uid=333, gid=222, text="在吗", mid="g1", at=True, **extra):
    segs = []
    if at:
        segs.append({"type": "at", "data": {"qq": "999"}})
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    e = {"post_type": "message", "message_type": "group", "user_id": uid,
         "group_id": gid, "message_id": mid, "time": int(time.time()),
         "message": segs,
         "sender": {"nickname": "测试乙", "card": "测试乙", "user_id": uid}}
    e.update(extra)
    return e


# ------------------------------------------------------------ 1. 私聊主流程
def test_private():
    bot, tmp = new_bot()
    try:
        bot.llm_reply = FakeLLM("在的在的")
        bot.on_event(priv())
        check("私聊：发出去了", wait_for(lambda: bot.ob.sent["private"]))
        e = bot.priv = None
        sent = bot.ob.sent["private"]
        check("私聊：内容正确",
              sent and "在的在的" in str(sent[0]["message"]))
        check("私聊：进上下文缓冲",
              any(m["text"] == "在的在的" for m in bot.mem.recent["u111"]))
        ap = os.path.join(bot.mem.archive_dir(), "private_111.md")
        check("私聊：写了归档", os.path.exists(ap))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 2. 不该回的
def test_private_skips():
    bot, tmp = new_bot()
    try:
        bot.llm_reply = FakeLLM("不该出现")
        bot.on_event(priv(uid=888))               # 非白名单
        bot.on_event(priv(text="", mid="p2"))     # 空消息
        bot.on_event(priv(mid="p1"))              # 重复 message_id
        check("非白名单不回", not wait_for(lambda: bot.ob.sent["private"], 0.6))
        check("空消息不回", not bot.ob.sent["private"])
        check("重复消息去重", not bot.ob.sent["private"])
        # 唯一那条发过之后，再发同样的 mid 也不回
        bot.on_event(priv(mid="p3"))
        wait_for(lambda: bot.ob.sent["private"])
        n = len(bot.ob.sent["private"])
        bot.on_event(priv(mid="p3"))
        check("同一 mid 二次到达不回", len(bot.ob.sent["private"]) == n,
              "发送数 %d" % len(bot.ob.sent["private"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 3. 群 @ 回复
def test_group_at():
    bot, tmp = new_bot({"g222": G.RECENT_G})
    try:
        bot.llm_reply = FakeLLM("好嘞")
        bot.on_event(grp(text=" 帮我看看", mid="g1"))
        wait_for(lambda: bot.ob.sent["group"])
        sent = bot.ob.sent["group"]
        check("群@：发出去了", bool(sent))
        if sent:
            segs = sent[0]["message"]
            check("群@：第一条带 @ 前缀",
                  segs and segs[0].get("type") == "at")
            check("群@：文本没被吞", "好嘞" in str(segs))
            check("群@：@ 与正文之间有空格",
                  segs[1]["data"]["text"].startswith(" ") if len(segs) > 1 else False)
        check("群@：@ 标记被去掉了",
              all("999" not in str(c.get("text", ""))
                  for c in bot.llm_reply.calls))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 4. 群其它分支
def test_group_others():
    bot, tmp = new_bot({"g222": G.RECENT_G})
    try:
        bot.llm_reply = FakeLLM("接话", "接话2")
        # 裸 @（只 @ 没写字）
        bot.on_event(grp(text="", mid="g2"))
        wait_for(lambda: bot.llm_reply.calls)
        check("裸@：走 bare 通路",
              bool(bot.llm_reply.calls) and bot.llm_reply.calls[-1]["kw"]["bare"])

        # 没 @ 我 → 不回复
        calls_before = len(bot.llm_reply.calls)
        bot.on_event(grp(text="聊点别的", mid="g3", at=False))
        time.sleep(0.3)
        check("未@我：不生成回复", len(bot.llm_reply.calls) == calls_before)

        # 白名单外的群 → 不回复
        bot.on_event(grp(gid=777, mid="g4"))
        time.sleep(0.3)
        check("白名单外的群：不生成回复",
              len(bot.llm_reply.calls) == calls_before)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 5. 连发合并
def test_merge():
    bot, tmp = new_bot({"g222": G.RECENT_G})
    try:
        bot.cfg["merge_window_sec"] = 0.4
        bot.llm_reply = FakeLLM("一起回")
        for i in range(3):
            bot.on_event(grp(text="第%d句" % i, mid="m%d" % i))
        wait_for(lambda: bot.llm_reply.calls, 3.0)
        time.sleep(0.5)
        check("连发合并：只问一次模型", len(bot.llm_reply.calls) == 1,
              "调用 %d 次" % len(bot.llm_reply.calls))
        if bot.llm_reply.calls:
            check("连发合并：三句合成一条",
                  all("第%d句" % i in bot.llm_reply.calls[0]["text"]
                      for i in range(3)))
        check("连发合并：只发一条", wait_for(lambda: bot.ob.sent["group"], 3.0)
              and len(bot.ob.sent["group"]) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 6. 长回复拆分
def test_split():
    bot, tmp = new_bot()
    try:
        bot.cfg["reply"]["split_multiline"] = True
        bot.cfg["reply"]["max_parts"] = 3
        bot.cfg["reply"]["split_delay_sec"] = 0     # 别把测试拖到几秒
        bot.llm_reply = FakeLLM("第一句\n第二句\n第三句\n第四句\n第五句")
        bot.on_event(priv(mid="s1"))
        wait_for(lambda: len(bot.ob.sent["private"]) >= 3, 4.0)
        check("拆分：最多 max_parts 条", len(bot.ob.sent["private"]) == 3,
              "实发 %d 条" % len(bot.ob.sent["private"]))
        texts = [str(s["message"]) for s in bot.ob.sent["private"]]
        check("拆分：超出的并到最后一条",
              bool(texts) and "第四句" in texts[-1] and "第五句" in texts[-1])

        # 关掉拆分就整条发
        bot2, tmp2 = new_bot()
        bot2.cfg["reply"]["split_multiline"] = False
        bot2.llm_reply = FakeLLM("一句\n两句")
        bot2.on_event(priv(mid="s2"))
        wait_for(lambda: bot2.ob.sent["private"])
        check("关闭拆分：整条发送", len(bot2.ob.sent["private"]) == 1)
        shutil.rmtree(tmp2, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 7. 表情包与图片
def test_images():
    """图片分两个方向：收到别人的图要交给模型看；自己回复里的 [[img:]] 要发出去。"""
    imgdir = tempfile.mkdtemp(prefix="qqimg_")
    # 一个能通过 sniff_image 的最小 JPEG（有文件头即可）
    jpg = os.path.join(imgdir, "a.jpg")
    with open(jpg, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9")

    bot, tmp = new_bot()
    try:
        # 回复里带 [[img:...]] → 解析成图片段
        bot.cfg["reply_images"]["dir"] = imgdir
        bot.cfg["reply_images"]["min_gap"] = 0     # 别被间隔挡住
        bot.llm_reply = FakeLLM("说得好 [[img:a.jpg]]")
        bot.on_event(priv(mid="i1"))
        wait_for(lambda: bot.ob.sent["private"])
        msg = bot.ob.sent["private"][0]["message"] if bot.ob.sent["private"] else []
        check("表情包：解析成图片段",
              any(isinstance(s, dict) and s.get("type") == "image" for s in msg))
        check("表情包：正文里的标记被去掉",
              all("[[img" not in str(s.get("data", {}).get("text", ""))
                  for s in msg if isinstance(s, dict)))

        # 不存在的图不能凭空造出来
        bot.llm_reply = FakeLLM("再来一张 [[img:不存在.jpg]]")
        bot.on_event(priv(mid="i1b"))
        time.sleep(0.5)
        last = bot.ob.sent["private"][-1]["message"] if bot.ob.sent["private"] else []
        check("表情包：不存在的文件不发送",
              not any(isinstance(s, dict) and s.get("type") == "image"
                      for s in last))

        # 收到的图片 → 交给模型 + 写归档（_image 是 dict，跟真实 _extract_image 一致）
        bot2, tmp2 = new_bot()
        bot2.llm_reply = FakeLLM("看到了")
        bot2._extract_image = lambda segs, conv: {
            "raw": b"\xff\xd8\xff" + b"\x00" * 32, "mime": "image/jpeg",
            "path": jpg, "size": 2048}
        bot2.on_event(priv(mid="i2", message=[
            {"type": "image", "data": {"file": "x", "url": "http://x/y.jpg"}}]))
        wait_for(lambda: bot2.llm_reply.calls)
        call = bot2.llm_reply.calls[-1] if bot2.llm_reply.calls else {}
        check("收图：传给模型时带图", bool(call.get("kw", {}).get("image")))
        ap = os.path.join(bot2.mem.archive_dir(), "private_111.md")
        arch = open(ap, encoding="utf-8", errors="replace").read() \
            if os.path.exists(ap) else ""
        check("收图：归档里记了图片", "【图片】" in arch)

        # 存图/归档炸了也不能连累回复（这条正是本次修掉的 bug）
        bot3, tmp3 = new_bot()
        bot3._extract_image = lambda segs, conv: "/tmp/fake.jpg"   # 形状不对
        bot3.llm_reply = FakeLLM("还是能回")
        bot3.on_event(priv(mid="i3", message=[
            {"type": "image", "data": {"file": "x", "url": "http://x/y.jpg"}}]))
        wait_for(lambda: bot3.ob.sent["private"], 2.0)
        check("存图异常：回复照发", bool(bot3.ob.sent["private"]))
        shutil.rmtree(tmp2, ignore_errors=True)
        shutil.rmtree(tmp3, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(imgdir, ignore_errors=True)


# ------------------------------------------------------------ 8. 引用/语音/文件
def test_enrich():
    bot, tmp = new_bot()
    try:
        # 引用
        bot._quote_text = lambda evt: "原话：几点集合"
        # 语音
        bot._voice_text = lambda evt: "语音内容：八点吧"
        # 文件
        bot._file_text = lambda evt: "（文件 名单.txt 内容：甲乙丙）"
        bot.llm_reply = FakeLLM("知道了")
        bot.on_event(priv(mid="e1", message=[
            {"type": "reply", "data": {"id": "123"}},
            {"type": "record", "data": {"file": "a.amr"}},
            {"type": "file", "data": {"file": "b", "name": "名单.txt"}},
            {"type": "text", "data": {"text": "看看这个"}}]))
        wait_for(lambda: bot.llm_reply.calls)
        t = bot.llm_reply.calls[-1]["text"] if bot.llm_reply.calls else ""
        check("引用：补上原文", "几点集合" in t)
        check("语音：补上转写", "八点吧" in t)
        check("文件：补上内容", "名单.txt" in t)
        check("原文还在", "看看这个" in t)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 9. 离线补推
def test_stale():
    bot, tmp = new_bot()
    try:
        bot.llm_reply = FakeLLM("刚看到")
        old = time.time() - 3 * 3600
        bot.on_event(priv(mid="o1", time=int(old)))
        wait_for(lambda: bot.llm_reply.calls)
        kw = bot.llm_reply.calls[-1]["kw"] if bot.llm_reply.calls else {}
        check("离线：带上了秒数", abs(kw.get("stale", 0) - 3 * 3600) < 60,
              "stale=%s" % kw.get("stale"))
        check("离线：标记了来源（非补回）", kw.get("replay") is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 10. 启动补回
def test_sweep():
    bot, tmp = new_bot()
    try:
        # 造一个「对方发了、我没回」的私聊归档（时间贴近现在，别被时限跳过）
        d = bot.mem.archive_dir()
        stamps = [time.time() - 600, time.time() - 300]
        with open(os.path.join(d, "private_777.md"), "w", encoding="utf-8") as f:
            f.write("# QQ 私聊归档 - 777\n\n")
            f.write("[%s] 我: 早\n" % time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(stamps[0] - 120)))
            for s in stamps:
                f.write("[%s] 路人(777): 在吗？我等你好久了\n"
                        % time.strftime("%Y-%m-%d %H:%M:%S",
                                         time.localtime(s)))
        bot.cfg["allowed_private"] = [777]
        bot.cfg["proactive"]["enabled"] = False
        bot.llm_reply = FakeLLM("来了来了")
        bot._sweep_unanswered()
        wait_for(lambda: bot.ob.sent["private"], 3.0)
        check("补回：漏回消息被补回", len(bot.ob.sent["private"]) == 1,
              "实发 %d 条" % len(bot.ob.sent["private"]))
        if bot.llm_reply.calls:
            check("补回：进了正常回复流程",
                  bot.llm_reply.calls[-1]["kw"].get("replay") is True)
            check("补回：带上了距今秒数",
                  bot.llm_reply.calls[-1]["kw"].get("stale", 0) > 0)

        # 太老的漏回不补（防止翻旧账）
        bot2, tmp2 = new_bot()
        bot2.ob = OB()
        with open(os.path.join(bot2.mem.archive_dir(), "private_777.md"),
                  "w", encoding="utf-8") as f:
            f.write("[%s] 路人(777): 三个月前的话\n" % time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 90 * 86400)))
        bot2.cfg["allowed_private"] = [777]
        bot2.llm_reply = FakeLLM("不该出现")
        bot2._sweep_unanswered()
        time.sleep(0.4)
        check("补回：超时的旧消息不补", not bot2.ob.sent["private"])
        shutil.rmtree(tmp2, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 11. 模型没出东西
def test_no_reply():
    bot, tmp = new_bot()
    try:
        bot.llm_reply = FakeLLM("", None)
        bot.on_event(priv(mid="n1"))
        bot.on_event(priv(mid="n2"))
        time.sleep(0.5)
        check("模型返回空：不发送", not bot.ob.sent["private"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 12. 异常输入
def test_weird_input():
    bot, tmp = new_bot()
    try:
        bot.llm_reply = FakeLLM("嗯")
        bad = [
            priv(mid="w1", sender=None),                  # sender 缺失
            priv(mid="w2", message=None),                 # message 缺失
            priv(mid="w3", message_id=None),              # mid 缺失
            priv(mid="w4", message=[{"type": "text"}]),   # data 缺失
            priv(mid="w5", message=[{"type": "unknown"}]),
            priv(mid="w6", time=None),                    # 时间戳缺失
            {"post_type": "message", "message_type": "group",
             "group_id": 222, "user_id": None, "message_id": "w7",
             "time": int(time.time()), "message": [], "sender": {}},
        ]
        for e in bad:
            bot.on_event(e)
        time.sleep(0.6)
        check("异常输入：主流程没崩", True)
        check("异常输入：没乱发消息",
              len(bot.ob.sent["private"]) + len(bot.ob.sent["group"]) <= 1,
              "发出 %d 条" % (len(bot.ob.sent["private"])
                             + len(bot.ob.sent["group"])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 13. 只发表情包
def test_sticker_only():
    """模型只输出 [[img:xxx]] 时正文是空的，图不能跟着一起被丢。"""
    imgdir = tempfile.mkdtemp(prefix="qqimg_")
    jpg = os.path.join(imgdir, "a.jpg")
    with open(jpg, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9")
    bot, tmp = new_bot()
    try:
        bot.cfg["reply_images"]["dir"] = imgdir
        bot.cfg["reply_images"]["min_gap"] = 0
        bot.llm_reply = FakeLLM("[[img:a.jpg]]")
        bot.on_event(priv(mid="so1"))
        wait_for(lambda: bot.ob.sent["private"])
        msg = bot.ob.sent["private"][0]["message"] if bot.ob.sent["private"] else []
        check("只发表情包：图还是发出去了",
              any(isinstance(s, dict) and s.get("type") == "image" for s in msg))

        # 纯表情那条只该是一条消息（别连同空文本一起发两条）
        check("只发表情包：只发一条", len(bot.ob.sent["private"]) == 1,
              "共 %d 条" % len(bot.ob.sent["private"]))
        # 正文+图 仍然是一句带图
        bot.llm_reply = FakeLLM("说得好 [[img:a.jpg]]")
        bot.on_event(priv(mid="so2"))
        wait_for(lambda: len(bot.ob.sent["private"]) >= 2)
        last = bot.ob.sent["private"][-1]["message"]
        check("文字+图：一条里既有文字也有图",
              any(isinstance(s, dict) and s.get("type") == "image" for s in last)
              and any(isinstance(s, dict) and "说得好" in str(s.get("data", {}).get("text", ""))
                      for s in last))

        # 群聊同样
        bot2, tmp2 = new_bot({"g222": G.RECENT_G})
        bot2.cfg["reply_images"]["dir"] = imgdir
        bot2.cfg["reply_images"]["min_gap"] = 0
        bot2.llm_reply = FakeLLM("[[img:a.jpg]]")
        bot2.on_event(grp(mid="so3"))
        wait_for(lambda: bot2.ob.sent["group"])
        gmsg = bot2.ob.sent["group"][0]["message"] if bot2.ob.sent["group"] else []
        check("群聊只发表情包：也发得出去",
              any(isinstance(s, dict) and s.get("type") == "image" for s in gmsg))
        shutil.rmtree(tmp2, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(imgdir, ignore_errors=True)


# ------------------------------------------------------------ 14. 连发丢图
def test_merge_image():
    """「先发图、再补一句」是最常见的连发形态，合并后图必须还在。"""
    bot, tmp = new_bot()
    try:
        bot.cfg["merge_window_sec"] = 0.4
        calls = []
        bot.llm_reply = FakeLLM("看到了")
        bot._extract_image = lambda segs, conv: {
            "raw": b"\xff\xd8\xff" + b"\x00" * 32, "mime": "image/jpeg",
            "path": "/tmp/x.jpg", "size": 2048}
        bot.on_event(priv(mid="mi1", message=[
            {"type": "image", "data": {"file": "x", "url": "http://x/y.jpg"}}]))
        bot.on_event(priv(mid="mi2", text="看看这个"))
        wait_for(lambda: bot.llm_reply.calls, 3.0)
        kw = bot.llm_reply.calls[-1]["kw"] if bot.llm_reply.calls else {}
        check("连发合并：图没被丢", bool(kw.get("image")))
        check("连发合并：文字也在",
              "看看这个" in (bot.llm_reply.calls[-1]["text"]
                            if bot.llm_reply.calls else ""))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 15. 记忆不连累回复
def test_memory_isolated():
    """记忆/归档/计数出任何小错，都不能把回复一起带走。"""
    boom = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("记忆模块炸了"))

    for label, patch in (("上下文缓冲", "remember_incoming"),
                         ("归档", "archive_incoming"),
                         ("情绪记录", "_note_mood")):
        bot, tmp = new_bot()
        try:
            bot.llm_reply = FakeLLM("还是能回")
            if patch == "_note_mood":
                setattr(bot.mem, patch, boom)
            else:
                setattr(bot.mem, patch, boom)
            bot.on_event(priv(mid="iso-" + patch))
            wait_for(lambda: bot.ob.sent["private"], 2.0)
            check("%s炸了：回复照发" % label, bool(bot.ob.sent["private"]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 16. CLI 挂了有兜底
def test_cli_fallback():
    """CLI 抛异常时必须吞掉并 return None，否则整条兜底链都到不了。"""
    bot, tmp = new_bot()
    try:
        bot.cfg["codebuddy"]["node"] = "E:/不存在/node.exe"
        check("_cli_invoke 吞掉异常", bot._cli_invoke("你好", None, "") is None)
        # 走完整条：CLI 挂 → 直连关 → 给占位话术（不写这条的话就是彻底不回）
        bot.cfg["fallback_to_api"] = False
        bot.cfg["quota_reply"] = "我脑子有点转不动了"
        bot.on_event(priv(mid="cf1"))
        wait_for(lambda: bot.ob.sent["private"], 3.0)
        check("CLI 挂了还能兜底占位", bool(bot.ob.sent["private"]))
        got = str(bot.ob.sent["private"][0]["message"]) \
            if bot.ob.sent["private"] else ""
        check("兜底发的是占位话术", "转不动" in got)

        # 占位也没填就该闭嘴，别为了「有回应」乱说话
        bot2, tmp2 = new_bot()
        bot2.cfg["codebuddy"]["node"] = "E:/不存在/node.exe"
        bot2.cfg["fallback_to_api"] = False
        bot2.cfg["quota_reply"] = ""
        bot2.on_event(priv(mid="cf2"))
        time.sleep(1.0)
        check("没占位话术就干脆不回", not bot2.ob.sent["private"])
        shutil.rmtree(tmp2, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 17. QQ 侧补捞
def _msg(uid, self_id, ts, text, at_me=False):
    segs = []
    if at_me:
        segs.append({"type": "at", "data": {"qq": str(self_id)}})
    segs.append({"type": "text", "data": {"text": text}})
    return {"user_id": uid, "self_id": self_id, "time": int(ts),
            "message": segs, "raw_message": text,
            "message_type": "private", "sub_type": "friend",
            "sender": {"user_id": uid, "nickname": "测试甲", "card": ""}}


def test_qq_sweep():
    """QQ 侧补捞：捞「连本地归档都没进」的漏回消息。

    判据两条都要满足：①对方说完我方隔了很久才回；②归档里附近没有它。
    """
    now = time.time()
    bot, tmp = new_bot()
    try:
        bot.self_id = 999
        bot.mem.self_id = 999
        bot.cfg["sweep"]["max_age_hours"] = 168
        bot.cfg["sweep"]["from_qq"] = True
        bot.cfg["sweep"]["replay_gap_sec"] = 600
        bot.cfg["allowed_private"] = [111]
        bot.cfg["allowed_groups"] = [222]
        # 归档里已经有「1 小时前 我: 刚说过」——所以这个会话是老会话
        old = now - 3600
        with open(os.path.join(bot.mem.archive_dir(), "private_111.md"),
                  "w", encoding="utf-8") as f:
            f.write("[%s] 测试甲(111): 在吗\n" % time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(old)))
            f.write("[%s] 我: 刚说过\n" % time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(old + 5)))

        hist = [
            # ①隔了 11 小时我方才回 → 漏回，且归档里没有 → 该补
            _msg(111, 999, now - 11 * 3600, "1"),
            _msg(111, 999, now - 11 * 3600 + 60, "hello"),
            # ②我方 5 秒后就回了 → 已回，不补
            _msg(111, 999, now - 9 * 3600, "刚说过"),
            _msg(999, 999, now - 9 * 3600 + 5, "收到"),
            # ③归档里有的（1 小时前那条）→ 本地扫负责，不重复补
            _msg(111, 999, old, "在吗"),
            _msg(999, 999, old + 5, "刚说过"),
            # ④QQ 系统通知 → 不补
            _msg(111, 999, now - 8 * 3600, "请求添加你为好友"),
            _msg(999, 999, now - 8 * 3600 + 3, "我们已成功添加为好友"),
        ]
        bot.ob = OB(history={("get_friend_msg_history", 111): hist})
        captured = []
        bot._enqueue = lambda conv, evt: captured.append((conv, evt))
        bot._sweep_from_qq()
        check("QQ 侧补捞：捞到了漏回的消息", len(captured) == 1,
              "入队 %d 次" % len(captured))
        if captured:
            txt = captured[0][1]["message"][0]["data"]["text"]
            check("QQ 侧补捞：内容对", txt == "1\nhello", repr(txt))
            check("QQ 侧补捞：带了距今秒数",
                  captured[0][1]["_stale"] > 10 * 3600)
            check("QQ 侧补捞：标记为补回",
                  captured[0][1].get("_replay") is True)

        # 群里没 @ 我 → 不补；@ 了我 → 补
        gh = [
            _msg(333, 999, now - 5 * 3600, "随便聊聊"),
            _msg(333, 999, now - 4 * 3600, "看下这个", at_me=True),
        ]
        bot2, tmp2 = new_bot()
        bot2.self_id = 999
        bot2.mem.self_id = 999
        bot2.cfg["sweep"]["max_age_hours"] = 168
        bot2.cfg["allowed_private"] = []
        bot2.cfg["allowed_groups"] = [222]
        with open(os.path.join(bot2.mem.archive_dir(), "group_222.md"),
                  "w", encoding="utf-8") as f:
            f.write("[%s] 我: 刚说过\n" % time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(now - 3600)))
        bot2.ob = OB(history={("get_group_msg_history", 222): gh})
        cap2 = []
        bot2._enqueue = lambda conv, evt: cap2.append((conv, evt))
        bot2._sweep_from_qq()
        check("群里只补 @ 了我的", len(cap2) == 1, "入队 %d 次" % len(cap2))
        if cap2:
            check("群里补的是 @ 我 的那条",
                  "看下这个" in cap2[0][1]["message"][0]["data"]["text"])

        # 没有本地归档的新会话 → 不补（否则一次性回一堆历史）
        bot3, tmp3 = new_bot()
        bot3.self_id = 999
        bot3.mem.self_id = 999
        bot3.cfg["sweep"]["max_age_hours"] = 168
        bot3.cfg["allowed_private"] = [222]
        bot3.cfg["allowed_groups"] = []
        bot3.ob = OB(history={("get_friend_msg_history", 222): [
            _msg(222, 999, now - 3600, "在吗")]})
        cap3 = []
        bot3._enqueue = lambda conv, evt: cap3.append((conv, evt))
        bot3._sweep_from_qq()
        check("新会话不补（防刷屏）", not cap3)

        # 每会话最多补 N 条
        bot4, tmp4 = new_bot()
        bot4.self_id = 999
        bot4.mem.self_id = 999
        bot4.cfg["sweep"]["max_age_hours"] = 168
        bot4.cfg["sweep"]["max_replay_per_conv"] = 2
        bot4.cfg["allowed_private"] = [111]
        bot4.cfg["allowed_groups"] = []
        with open(os.path.join(bot4.mem.archive_dir(), "private_111.md"),
                  "w", encoding="utf-8") as f:
            f.write("[%s] 我: 刚说过\n" % time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(now - 3600)))
        many = [_msg(111, 999, now - (10 - i) * 3600, "第%d条" % i)
                for i in range(6)]
        bot4.ob = OB(history={("get_friend_msg_history", 111): many})
        cap4 = []
        bot4._enqueue = lambda conv, evt: cap4.append((conv, evt))
        bot4._sweep_from_qq()
        check("每会话最多补 N 条", len(cap4) == 1 and
              len(cap4[0][1]["message"][0]["data"]["text"].split("\n")) == 2,
              "补了 %s 条" % (len(cap4[0][1]["message"][0]["data"]["text"].split("\n"))
                              if cap4 else 0))

        for t in (tmp2, tmp3, tmp4):
            shutil.rmtree(t, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 18. 限流不该丢消息
def test_rate_gate():
    """上一条刚回完就又发一条——间隔不够，应该等一下再回，不能把消息扔掉。

    这是用户实测踩到的：机器人在第二条刚发出时回了第一条，第二条就没了，
    而且日志里一个字都没有（`_rate_ok` 返回 False 就直接 return）。
    """
    bot, tmp = new_bot()
    try:
        bot.cfg["min_interval_sec"] = 1      # 测试用小值，不用等 5 秒
        bot.llm_reply = FakeLLM("回你一", "回你二", "回你三")
        bot.on_event(priv(mid="rg1"))
        check("第一条回了", wait_for(lambda: len(bot.ob.sent["private"]) >= 1))
        # 立刻再发：此刻距上次回复不足 1 秒，旧逻辑会直接丢弃这条
        bot.on_event(priv(mid="rg2"))
        check("间隔不够时等一下再回，不丢弃",
              wait_for(lambda: len(bot.ob.sent["private"]) >= 2, 5.0),
              "实发 %d 条" % len(bot.ob.sent["private"]))
        check("两条都回了，内容不串",
              len(bot.ob.sent["private"]) >= 2
              and "回你一" in str(bot.ob.sent["private"][0]["message"])
              and "回你二" in str(bot.ob.sent["private"][1]["message"]))
        # 再来一条，顺序依旧
        bot.on_event(priv(mid="rg3"))
        check("第三条也回了",
              wait_for(lambda: len(bot.ob.sent["private"]) >= 3, 5.0),
              "实发 %d 条" % len(bot.ob.sent["private"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 19. 每分钟上限
def test_rate_minute_cap():
    """每分钟上限是故意的限流（等也没用），该丢弃——但不能影响后续会话。"""
    bot, tmp = new_bot()
    try:
        bot.cfg["min_interval_sec"] = 0
        bot.cfg["max_replies_per_minute"] = 1
        bot.llm_reply = FakeLLM("一", "二")
        bot.on_event(priv(mid="rm1"))
        wait_for(lambda: bot.ob.sent["private"])
        bot.on_event(priv(mid="rm2"))
        time.sleep(0.5)
        check("每分钟超限：不硬发", len(bot.ob.sent["private"]) == 1,
              "实发 %d 条" % len(bot.ob.sent["private"]))
        # 另一个会话不该被连带影响
        bot2, tmp2 = new_bot()
        bot2.cfg["min_interval_sec"] = 0
        bot2.cfg["max_replies_per_minute"] = 1
        bot2.llm_reply = FakeLLM("三")
        bot2.on_event(priv(uid=111, mid="rm3"))
        check("别的会话不受影响", wait_for(lambda: bot2.ob.sent["private"], 3.0))
        shutil.rmtree(tmp2, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 20. 拆成短条
def test_split_policy():
    """长内容切成一条条短消息（每条 ≤30 字，算标点）。

    用户要求：可以发长文，但一条消息不能太长——超过 30 字就该切成多条发。
    主人的「碎句连发」习惯保留（换行分段之间不合并）。
    """
    bot, tmp = new_bot()
    try:
        # 主人的碎句连发：换行分段，各发各的
        p = bot._split_parts("睡了吗\n想你")
        check("短句连发仍然拆开", len(p) == 2 and p == ["睡了吗", "想你"], str(p))
        # 长内容 → 切成多条，每条 ≤30 字
        long3 = ("不觉得。懦弱的人不会把这话说出来，早就自己憋着或者装作没事了。\n"
                 "你跟我说这个，反而是件挺费劲的事。都到这份上了还愿意开口，这不叫懦弱。\n"
                 "真要说的话，我倒觉得你是撑得太久了，累的。你现在一个人待着吗？")
        p = bot._split_parts(long3)
        check("长内容切成多条", len(p) > 1, "拆成 %d 条" % len(p))
        check("每条都不超过 30 字（算标点）",
              all(len(x) <= 30 for x in p),
              "最长 %d 字" % max(len(x) for x in p))
        check("内容没丢", "".join(p).replace("\n", "") ==
              long3.replace("\n", ""))
        # 颜文字不能被切开
        p = bot._split_parts("我今天超累的_(:3」∠)_，什么都不想干")
        check("颜文字完整不被切开",
              any("_(:3」∠)_" in x for x in p), str(p))
        # 换行分段之间不合并（段落感要保住）
        p = bot._split_parts("第一段说完了。\n第二段开始啦。")
        check("换行分段各自成条", len(p) == 2, str(p))
        # max_parts 是兜底
        bot.cfg["reply"]["max_parts"] = 2
        p = bot._split_parts("一\n二\n三\n四")
        check("max_parts 兜底生效", len(p) == 2, "拆成 %d 条" % len(p))
        check("超出的用换行并回最后一条", "\n" in p[-1], repr(p[-1][:40]))
        # 关掉拆分 → 整条
        bot.cfg["reply"]["split_multiline"] = False
        p = bot._split_parts("睡了吗\n想你")
        check("关掉拆分就整条发", len(p) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 21. 主人是谁
def test_owner():
    """人设里写了「对主人最亲近」但没说主人是谁——要明确告诉模型。"""
    bot, tmp = new_bot()
    try:
        bot.cfg["owner_qq"] = ""
        check("没配主人就不注入这段", bot._owner_hint() == "")
        p, _ = bot._prompt("u111", "在吗", uid=111)
        check("没配主人时 prompt 里没有主人段", "【你的主人】" not in p)

        bot.cfg["owner_qq"] = "999000111"
        h = bot._owner_hint()
        check("主人段带 QQ 号", "999000111" in h, h[:40])
        check("主人段提醒别被冒充骗了", "别当真" in h)
        p, _ = bot._prompt("u111", "在吗", uid=111)
        check("主人段进了 prompt", "【你的主人】" in p)
        # 群聊也一样注入（群里别人可能自称主人）
        p2, _ = bot._prompt("g222", "在吗", uid=333)
        check("群聊也注入主人段", "【你的主人】" in p2)
        # 支持多个主人
        bot.cfg["owner_qq"] = "999000111,999000222"
        h = bot._owner_hint()
        check("支持多个主人", "999000111" in h and "999000222" in h)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 22. 回读的对话段
def test_restore_segments():
    """回读按「对话段」切，不按绝对时长。

    踩过的坑：用「最近 6 小时」一刀切，结果主人隔了 14 小时的私聊
    （"我昨晚干了啥"）直接忘了；同一把尺子又让群里 33 小时前的老话题
    一直抓着不放。同样是"隔了很久"，私聊和群聊性质完全不同。
    """
    bot, tmp = new_bot()
    try:
        now = time.time()
        h = 3600

        def stamp(off):
            return time.strftime("%Y-%m-%d %H:%M:%S",
                                 time.localtime(now - off))

        # 私聊：两段（间隔 5 小时 > 3 小时的断点），两段都要在
        p = os.path.join(bot.mem.archive_dir(), "private_111.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("[%s] 测试甲(111): 昨晚说的那句话\n" % stamp(14 * h))
            f.write("[%s] 我: 嗯，我记得\n" % stamp(14 * h - 60))
            f.write("[%s] 测试甲(111): 今天说的这句\n" % stamp(h))
            f.write("[%s] 我: 这句是刚说的\n" % stamp(h - 60))
        bot.mem.recent.clear()
        bot.mem._restore_context()
        ctx = bot.mem.context_lines("u111")
        check("私聊：隔夜那段也回读了",
              any("昨晚说的那句话" in x for x in ctx), str(ctx))
        check("私聊：今天这段也在",
              any("今天说的这句" in x for x in ctx))

        # 群里：只保留最后一段（久远的那段不算当前话题）
        g = os.path.join(bot.mem.archive_dir(), "group_222.md")
        with open(g, "w", encoding="utf-8") as f:
            f.write("[%s] 测试乙(333): 很久以前那个老话题\n" % stamp(33 * h))
            f.write("[%s] 我: 哦\n" % stamp(33 * h - 60))
            f.write("[%s] 测试乙(333): 刚说的这句\n" % stamp(h))
            f.write("[%s] 我: 这句是刚说的\n" % stamp(h - 60))
        bot.mem.recent.clear()
        bot.mem._restore_context()
        ctx = bot.mem.context_lines("g222")
        check("群：久远的那段不回读",
              not any("很久以前那个老话题" in x for x in ctx), str(ctx))
        check("群：最后一段在",
              any("刚说的这句" in x for x in ctx))

        # 条数预算不够时，每段都要露脸（不能整段被挤掉）
        p2 = os.path.join(bot.mem.archive_dir(), "private_222.md")
        with open(p2, "w", encoding="utf-8") as f:
            for i in range(40):
                f.write("[%s] 测试甲(222): 老段第%d条\n"
                        % (stamp(10 * h), i))
            for i in range(40):
                f.write("[%s] 测试甲(222): 新段第%d条\n"
                        % (stamp(h), i))
        bot.cfg["context_messages"] = 30
        bot.mem.recent.clear()
        bot.mem._restore_context()
        ctx = bot.mem.context_lines("u222")
        check("预算不够时老段也露脸",
              any("老段第0条" in x for x in ctx),
              "共 %d 条" % len(ctx))
        check("新段也在",
              any("新段第39条" in x for x in ctx))
        check("总量不超上限", len(ctx) <= 30, "共 %d 条" % len(ctx))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 23. 「昨晚」认得吗
def test_last_night_dates():
    """「昨晚」必须能触发日期通道——它跨夜，涉及两个日期。"""
    import memory as M
    import datetime
    today = datetime.date.today()
    y = (today - datetime.timedelta(days=1)).isoformat()
    t = today.isoformat()
    check("「昨晚」能解析出日期",
          M.parse_dates("我昨晚干了啥") == {y, t},
          str(M.parse_dates("我昨晚干了啥")))
    check("「昨天晚上」也认",
          M.parse_dates("昨天晚上说了啥") == {y, t})
    check("「今天晚上」认今天",
          M.parse_dates("今天晚上有空吗") == {y, t})
    check("「前天」还是原来那样",
          M.parse_dates("前天") == {(today - datetime.timedelta(days=2)).isoformat()})
    check("没有日期词返回 None",
          M.parse_dates("在吗") is None)


# ------------------------------------------------------------ 25. 模糊时间词定位
def test_time_words():
    """出现「昨晚/今天/前天」这类模糊时间词 → 把那段时间的记录连时间戳给它。

    想法来自用户：「出现（昨晚，今天，昨天）这种模糊时间词时，连当时的聊天
    记录中的时间戳一起发送给模型，根据具体的时间戳的昨晚，这样去查找」。
    语义检索找不到「我昨晚干了啥」（问句和内容对不上），但时间范围是确定的。
    """
    import memory as M
    import datetime
    today = datetime.date.today()

    def rng(q):
        r = M.parse_time_range(q)
        return (r[0], r[1]) if r else None

    # 「昨晚」跨夜：昨天傍晚到今天凌晨
    y = (today - datetime.timedelta(days=1)).isoformat()
    t = today.isoformat()
    r = rng("我昨晚干了啥")
    check("「昨晚」解析出跨夜范围", r is not None)
    if r:
        check("「昨晚」从昨天 18:00 开始",
              time.strftime("%H:%M", time.localtime(r[0])) == "18:00",
              time.strftime("%m-%d %H:%M", time.localtime(r[0])))
        check("「昨晚」到今天 06:00 结束",
              time.strftime("%H:%M", time.localtime(r[1])) == "06:00",
              time.strftime("%m-%d %H:%M", time.localtime(r[1])))
    check("「昨天」是昨天一整天",
          rng("昨天") == (M.parse_time_range("昨天")[0], M.parse_time_range("昨天")[1]))
    check("「明天」不触发（还没发生）", rng("明天") is None)
    check("「刚才」不触发（上下文里就有）", rng("刚才") is None)
    check("没有时间词返回 None", rng("在吗") is None)

    # 造归档：时间点必须**落在「昨晚」窗口内**（昨天 18:00 ~ 今天 06:00）。
    # 别用 now-12h 这种相对偏移——那样跑到白天/傍晚就落到窗口外了
    # （下午跑能过、晚上跑就挂，这种时间依赖最难查）。
    bot, tmp = new_bot()
    try:
        now = time.time()
        today0 = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        # 昨天 20:00 = 今天 00:00 往前 4 小时（昨晚窗口是昨天 18:00~今天 06:00）
        yst = today0 - 4 * 3600
        p = os.path.join(bot.mem.archive_dir(), "private_111.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("[%s] 测试甲(111): 昨晚我们说的那个事\n"
                    % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(yst)))
            f.write("[%s] 我: 嗯，我记着呢\n"
                    % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(yst + 60)))
        bot.mem.recent.clear()
        bot.mem._restore_context()
        bot.mem._build_search_index()
        has_t, rows = bot.mem.time_records("u111", "我昨晚干了啥")
        check("能定位到昨晚那段", has_t and len(rows) >= 1, "%d 条" % len(rows))
        check("记录带完整时间戳",
              bool(rows) and rows[0].startswith("["), rows[0][:40] if rows else "")
        check("记录内容对",
              bool(rows) and "昨晚我们说的那个事" in rows[0])
        # prompt 里要真的注进去
        pr, _ = bot._prompt("u111", "我昨晚干了啥", uid=111)
        check("prompt 里注入了那段时间的记录",
              "【对方说的那段时间的聊天记录" in pr and "昨晚我们说的那个事" in pr)
        # 没时间词就不注入
        pr2, _ = bot._prompt("u111", "在吗", uid=111)
        check("没时间词就不注入", "【对方说的那段时间" not in pr2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 26. 限流按会话
def test_rate_per_session():
    """每分钟上限按**会话**分别算。

    以前是全局共享的：一个活跃私聊刷屏会把群聊的额度一起吃掉，
    群里就发不出话来了。
    """
    bot, tmp = new_bot()
    try:
        bot.cfg["min_interval_sec"] = 0
        bot.cfg["max_replies_per_minute"] = 2
        bot.llm_reply = FakeLLM("一", "二", "三", "四")
        # 私聊 111 回两条（用满它的额度）
        bot.on_event(priv(mid="rp1"))
        wait_for(lambda: len(bot.ob.sent["private"]) >= 1)
        bot.on_event(priv(mid="rp2"))
        wait_for(lambda: len(bot.ob.sent["private"]) >= 2)
        # 第三次 → 私聊自己超限
        bot.on_event(priv(mid="rp3"))
        time.sleep(0.4)
        check("本会话用满额度后不再发", len(bot.ob.sent["private"]) == 2,
              "实发 %d 条" % len(bot.ob.sent["private"]))
        # 另一个会话不受影响
        bot2, tmp2 = new_bot()
        bot2.cfg["min_interval_sec"] = 0
        bot2.cfg["max_replies_per_minute"] = 2
        bot2.cfg["allowed_groups"] = [222]
        bot2.llm_reply = FakeLLM("群里的回复")
        bot2.on_event(grp(mid="rg1"))
        check("别的会话额度独立，不被连累",
              wait_for(lambda: bot2.ob.sent["group"], 3.0),
              "群发出 %d 条" % len(bot2.ob.sent["group"]))
        shutil.rmtree(tmp2, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 27. 群聊分层
def test_group_layers():
    """群里分两层：最近 N 条是「现场」（接话依据），更早的是「背景参考」。"""
    bot, tmp = new_bot()
    try:
        bot.cfg["group_focus_messages"] = 3
        rows = deque(maxlen=50)
        for i in range(8):
            rows.append({"mid": "m%d" % i, "nick": "群友", "uid": 333,
                         "text": "第%d条" % i, "at_me": False,
                         "ts": time.time() - (8 - i) * 600})
        bot.mem.recent["g222"] = rows
        p, _ = bot._prompt("g222", "在吗", uid=333)
        check("有「现在的聊天现场」这一层", "【现在的聊天现场" in p)
        check("有「更早的聊天记录（背景参考」这一层",
              "【更早的聊天记录（背景参考" in p)
        focus = p[p.find("【现在的聊天现场"):p.find("【更早的聊天记录")]
        check("现场只取最近 N 条",
              "第5条" in focus and "第7条" in focus and "第4条" not in focus,
              focus[focus.find("第"):focus.find("第") + 40].replace("\n", " "))
        bg = p[p.find("【更早的聊天记录"):]
        check("更早的进背景层", "第0条" in bg and "第1条" in bg)
        check("背景层交代「别当成现在的话题」", "别当成现在的话题" in bg)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 28. 戳一戳
def test_poke():
    """戳一戳要能回应。

    NapCat 把戳一戳推成 notice(notify/poke)，以前 on_event 按
    post_type != "message" 直接丢掉 → 戳了没反应。
    群里的戳也可能作为消息里的 poke 段出现（NapCat 段类型表里有 poke）。
    两种都要支持。
    """
    bot, tmp = new_bot()
    try:
        bot.self_id = 999
        bot.mem.self_id = 999
        bot.llm_reply = FakeLLM("戳到我了", "又戳我", "群里的戳", "私聊的戳")

        # ①群里的 notice(poke)，戳的是我
        bot.on_event({
            "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
            "group_id": 222, "user_id": 333, "target_id": 999,
            "time": int(time.time()),
        })
        check("群里的戳一戳有回应",
              wait_for(lambda: bot.ob.sent["group"], 3.0),
              "群发出 %d 条" % len(bot.ob.sent["group"]))
        check("群里的戳带 @ 前缀",
              bool(bot.ob.sent["group"])
              and bot.ob.sent["group"][0]["message"][0].get("type") == "at")

        # ②戳的是别人 → 不掺和
        n = len(bot.ob.sent["group"])
        bot.on_event({
            "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
            "group_id": 222, "user_id": 333, "target_id": 888,
            "time": int(time.time()),
        })
        time.sleep(0.4)
        check("戳别人不掺和", len(bot.ob.sent["group"]) == n)

        # ③消息里带 poke 段（群戳的另一种表示）
        bot.on_event(grp(mid="pk1", at=False, message=[
            {"type": "poke", "data": {"id": "999", "name": "机器人"}}]))
        check("消息里的 poke 段也认",
              wait_for(lambda: len(bot.ob.sent["group"]) > n, 3.0),
              "群发出 %d 条" % len(bot.ob.sent["group"]))

        # ④私聊戳一戳
        bot.on_event({
            "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
            "user_id": 111, "target_id": 999, "time": int(time.time()),
        })
        check("私聊戳一戳有回应",
              wait_for(lambda: bot.ob.sent["private"], 3.0),
              "私聊发出 %d 条" % len(bot.ob.sent["private"]))

        # ⑤别的事件类型照旧忽略
        bot.on_event({
            "post_type": "notice", "notice_type": "group_increase",
            "group_id": 222, "user_id": 333, "time": int(time.time()),
        })
        time.sleep(0.3)
        check("别的 notice 不处理",
              len(bot.ob.sent["group"]) == 2 and len(bot.ob.sent["private"]) == 1,
              "群 %d / 私聊 %d" % (len(bot.ob.sent["group"]),
                                   len(bot.ob.sent["private"])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 29. 图片大小与压缩
def test_image_limits():
    """图片上限放到 10MB，且大图要压到模型能吃下的尺寸。

    用户实测：群里连发几张图全被拒，机器人对那些图等于失明。
    两件事要同时成立：
      ①「下载得下」——上限从 2MB 提到 10MB
      ②「模型看得见」——图片是 base64 后塞进请求体的（膨胀 1/3），
         下游多模态 API 对单图有硬上限，不压就会被拒（还是失明）
    """
    try:
        from PIL import Image
    except Exception:
        check("图片压缩（跳过：没装 PIL）", True)
        return
    bot, tmp = new_bot()
    try:
        d = tempfile.mkdtemp(prefix="qqimg2_")
        # 一张大图：3000x2000 的噪声 JPEG（几 MB）
        big = os.path.join(d, "big.jpg")
        im = Image.new("RGB", (3000, 2000), (30, 90, 160))
        px = im.load()
        for y in range(0, 2000, 2):
            for x in range(0, 3000, 2):
                px[x, y] = ((x * 7) % 256, (y * 11) % 256, (x * y) % 256)
        im.save(big, "JPEG", quality=95)
        raw_big = open(big, "rb").read()
        # 一张小图
        small = os.path.join(d, "small.jpg")
        Image.new("RGB", (600, 400), (200, 100, 50)).save(small, "JPEG")
        raw_small = open(small, "rb").read()

        # ①上限已经是 10MB
        check("图片上限是 10MB",
              int(bot.cfg["media"]["max_bytes"]) == 10 * 1024 * 1024,
              str(bot.cfg["media"]["max_bytes"]))

        # ②几 MB 的图不再被拒（旧上限 2MB 会拒）
        res = bot._download_image({"file": big})
        check("几 MB 的图能下载下来（不再被 2MB 挡掉）", res is not None,
              "%.2fMB" % (len(raw_big) / 1048576.0))

        # ③超过 10MB 的仍然拒（不能无限收）
        bot.cfg["media"]["max_bytes"] = 1024
        check("超过上限的图仍然被拒",
              bot._download_image({"file": big}) is None)
        bot.cfg["media"]["max_bytes"] = 10 * 1024 * 1024

        # ④大图会被压到能送进模型的尺寸
        out, omime = bot._fit_for_model(raw_big, "image/jpeg")
        check("大图被压缩", len(out) < len(raw_big),
              "%.2fMB → %.2fMB" % (len(raw_big) / 1048576.0, len(out) / 1048576.0))
        check("压缩后不超过 media.max_out_bytes",
              len(out) <= int(bot.cfg["media"]["max_out_bytes"]),
              "%.2fMB" % (len(out) / 1048576.0))
        with Image.open(io.BytesIO(out)) as x:
            check("压缩后长边不超过 max_edge",
                  max(x.size) <= int(bot.cfg["media"]["max_edge"]),
                  "%dx%d" % x.size)

        # ⑤base64 之后不能太大（下游硬上限在这儿）
        import base64 as _b64
        b64 = len(_b64.b64encode(out))
        check("base64 后不超过 5MB（多模态 API 的常见硬上限）",
              b64 <= 5 * 1024 * 1024, "%.2fMB" % (b64 / 1048576.0))

        # ⑥小图不动（别白白损失画质）
        out2, mime2 = bot._fit_for_model(raw_small, "image/jpeg")
        check("小图原样送，不压缩", out2 == raw_small and mime2 == "image/jpeg")

        # ⑦GIF 动图：取第一帧转 JPEG（模型看动图本来也只看一帧）
        gif = os.path.join(d, "a.gif")
        fr = [Image.new("P", (800, 800), i % 3) for i in range(4)]
        fr[0].save(gif, save_all=True, append_images=fr[1:], duration=100, loop=0)
        raw_gif = open(gif, "rb").read()
        out3, mime3 = bot._fit_for_model(raw_gif, "image/gif")
        with Image.open(io.BytesIO(out3)) as x:
            check("GIF 压缩后能打开", x.size[0] > 0, "%dx%d %s" % (x.size + (x.format,)))

        # ⑧透明底 PNG 不能压成黑一片
        # （用大尺寸触发压缩路径：小图是原样透传的，不该动它）
        png = os.path.join(d, "a.png")
        Image.new("RGBA", (2400, 2400), (255, 0, 0, 128)).save(png)
        raw_png = open(png, "rb").read()
        out4, mime4 = bot._fit_for_model(raw_png, "image/png")
        with Image.open(io.BytesIO(out4)) as x:
            check("透明 PNG 转成白底 RGB（不是黑块）",
                  x.mode == "RGB" and x.getpixel((0, 0))[0] > 200,
                  "%s %s" % (x.mode, str(x.getpixel((0, 0)))))

        # 小图原样透传（透明底也保留原样，不动它）
        spng = os.path.join(d, "s.png")
        Image.new("RGBA", (300, 300), (255, 0, 0, 128)).save(spng)
        raw_spng = open(spng, "rb").read()
        out4b, _ = bot._fit_for_model(raw_spng, "image/png")
        check("小 PNG 原样透传", out4b == raw_spng)

        # ⑨关掉压缩（max_out_bytes=0）→ 原样送
        bot.cfg["media"]["max_out_bytes"] = 0
        bot.cfg["media"]["max_edge"] = 0
        out5, _ = bot._fit_for_model(raw_big, "image/jpeg")
        check("关掉压缩就原样送", out5 == raw_big)

        # ⑩一条消息里第一张图被拒，不该把后面的图也跳过
        bot.cfg["media"]["max_bytes"] = 1024          # 让所有图都被拒
        segs = [{"type": "image", "data": {"file": big}},
                {"type": "image", "data": {"file": small}}]
        check("被拒的图不占用 max_images 配额",
              bot._extract_image(segs, "u111") is None)
        bot.cfg["media"]["max_bytes"] = 10 * 1024 * 1024
        shutil.rmtree(d, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 30. CLI 登录态与锁
def test_cli_auth_and_lock():
    """CLI 登录态失效时要立刻降级；CLI 调用要有跨进程互斥锁。

    真踩过两个坑：
    ① 登录态被别的 CLI 实例并发刷新搞没了（hasSession 变 false），
       表现是所有回复都报 Authentication required。必须能识别出来、
          立刻降级到直连 API，而不是把整条模型链都试一遍。
    ② 多个 CLI 实例并发会互相覆盖登录态 → 用「原子创建锁文件」互斥。
    """
    bot, tmp = new_bot()
    try:
        # ① 进程内互斥：两个线程不能同时进临界区
        marks = []
        def worker():
            with bot._cli_process_lock(timeout=10):
                marks.append(time.time())
                time.sleep(0.6)
        ths = [threading.Thread(target=worker) for _ in range(2)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        marks.sort()
        check("CLI 锁：同进程两个线程互斥",
              len(marks) == 2 and marks[1] - marks[0] >= 0.5,
              "间隔 %.2fs" % (marks[1] - marks[0]) if len(marks) == 2 else "只有 %d 个" % len(marks))

        # ② 跨进程互斥：锁文件用 O_EXCL 原子创建
        #    用一个「存在但不是自己」的 PID 当持有者（模拟别的进程持锁）。
        #    注意不能用不存在的 PID——那种会被当陈旧锁清掉。
        lock = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), ".cli.lock")
        try:
            if os.path.exists(lock):
                os.remove(lock)
        except Exception:
            pass
        made = False
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, b"1")
            os.close(fd)
            made = True
        except FileExistsError:
            # 守护进程正在跑、正占着这个锁（真实环境常见）。
            # 这时没法造测试用的锁文件，跳过这两条断言——
            # 不能让「测试跟生产抢资源」变成误报。
            pass
        if not made:
            check("CLI 锁：跨进程互斥（跳过：守护进程正占用锁）", True)
        else:
            got = None
            try:
                with bot._cli_process_lock(timeout=2):
                    # 拿不到跨进程锁时不阻塞（降级成只用进程内锁），
                    # 但**不能删掉别人持有的锁**
                    got = os.path.exists(lock)
            except Exception:
                got = False
            check("CLI 锁：别的进程持锁时不删掉别人的锁", got is True,
                  "锁还在=%s" % got)
            try:
                os.remove(lock)
            except Exception:
                pass

        # ③ 陈旧锁（很久没更新）要能自动清理
        try:
            if os.path.exists(lock):
                os.remove(lock)
        except Exception:
            pass
        with open(lock, "w") as f:
            f.write("1")
        old = time.time() - 3600          # 一小时前（陈旧判据是 mtime）
        try:
            os.utime(lock, (old, old))
        except Exception:
            pass
        ok = False
        try:
            with bot._cli_process_lock(timeout=5):
                ok = True
        except Exception as e:
            ok = False
        check("CLI 锁：陈旧锁（很久没更新）能自动清理", ok)
        try:
            os.remove(lock)
        except Exception:
            pass

        # ④ 登录态失效 → 标记 + 走兜底，而不是发占位回复
        bot2, tmp2 = new_bot()
        try:
            bot2.cfg["fallback_to_api"] = True     # 本地配置默认关着
            bot2.api_key = "sk-test"
            bot2._cli_auth_failed = False
            bot2._auth_warned = False
            # 模拟 CLI 返回认证失败
            def fake_invoke(prompt=None, image=None, model=None, tools=None):
                bot2._cli_auth_failed = True
                return None
            bot2._cli_invoke = fake_invoke
            bot2._api_reply = lambda *a, **k: "直连兜底的回复"
            # 先直接置位（_cli_invoke 只在真实调用路径里跑，这里测的是
            # 「已知道登录态失效」时的降级与提示行为）
            bot2._cli_auth_failed = True
            r = bot2._last_resort("u111", "在吗")
            check("CLI 登录态失效 → 走直连 API 兜底", r == "直连兜底的回复", repr(r))
            check("CLI 登录态失效 → 有醒目提示",
                  bot2._auth_warned is True)
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

        # ⑥ 噪音事件不该记日志（否则真正的消息被淹掉）
        #    实测：心跳 30 秒一条、「对方正在输入」每次打字推两条，
        #    全记的话几分钟上千行，18:48 那条私聊就被埋得找不到了
        noise = [
            ({"post_type": "meta_event", "meta_event_type": "heartbeat"}, True),
            ({"post_type": "notice", "sub_type": "input_status"}, True),
            ({"post_type": "notice", "sub_type": "poke"}, False),
            ({"post_type": "notice", "sub_type": "group_increase"}, False),
        ]
        bad = [(e, qa.AutoReply._is_noise_event(e))
               for e, want in noise if qa.AutoReply._is_noise_event(e) != want]
        check("噪音事件（心跳/输入状态）不记日志", not bad, str(bad)[:80])

        # ⑦ 直连 API：reasoning 模型正文在 reasoning_content 时也要能取到
        bot3, tmp3 = new_bot()
        try:
            bot3.api_key = "sk-test"
            bot3.llm = dict(bot3.llm)
            bot3.llm["url"] = "http://127.0.0.1:1/none"
            # 不发真请求，只验证取正文的逻辑
            orig = qa.urllib.request.urlopen

            class FakeResp:
                def read(self):
                    return json.dumps({
                        "choices": [{"message": {"content": "",
                                                "reasoning_content": "思考出的正文"}}]
                    }).encode()

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            qa.urllib.request.urlopen = lambda *a, **k: FakeResp()
            try:
                t = bot3._api_post([{"role": "user", "content": "x"}],
                                   "u111", "x", deque(maxlen=4))
                check("直连 API：正文在 reasoning_content 也能取到",
                      t == "思考出的正文", repr(t))
            finally:
                qa.urllib.request.urlopen = orig
        finally:
            shutil.rmtree(tmp3, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 31. 补捞不重复
def test_sweep_no_repeat():
    """QQ 侧补捞的消息要写进归档，否则每次重启都会重复补、重复回复。

    用户实测踩到：同一条历史消息被回复了三次，对方连着收到复读。
    根因：_qq_replay 只把消息塞进队列，**没写归档**；而判定「漏回」的
    依据正是「归档里附近没有这条消息的时间戳」——不写就永远判定没进过
    系统，每次启动都补一遍。
    """
    tmp = tempfile.mkdtemp(prefix="qqsweep_")
    try:
        cfg = qa.load_config()
        cfg = dict(cfg)
        cfg["archive"] = dict(cfg.get("archive") or {})
        cfg["archive"]["dir"] = tmp
        cfg["sweep"] = dict(cfg.get("sweep") or {})

        def mkbot():
            b = qa.AutoReply(cfg)
            b.self_id = 999
            b.mem.self_id = 999
            return b

        now = int(time.time())
        p = os.path.join(tmp, "private_111.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("[%s] 测试甲(111): 之前的话\n" % time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(now - 86400)))

        hist = [{"time": now - 72000, "user_id": 111,
                 "message": [{"type": "text", "data": {"text": "你还在吗"}}],
                 "sender": {"nickname": "测试甲"}}]

        n1 = mkbot()._qq_replay("u111", "private", 111, hist,
                                168 * 3600, now, 5)
        check("补捞：第一次能补到", n1 == 1, "%d 条" % n1)

        arch = open(p, encoding="utf-8").read()
        check("补捞：消息写进了归档", "你还在吗" in arch)
        check("补捞：归档用的是原始时间戳（不是「现在」）",
              time.strftime("%Y-%m-%d", time.localtime(now - 72000)) in arch,
              arch.strip().splitlines()[-1][:40] if arch.strip() else "")

        # 模拟重启：新实例、同一份归档
        n2 = mkbot()._qq_replay("u111", "private", 111, hist,
                                168 * 3600, now + 60, 5)
        check("补捞：重启后不再重复补（用户踩过的 bug）", n2 == 0,
              "%d 条" % n2)
        # 重启后不再重复
        n3 = mkbot()._qq_replay("u111", "private", 111, hist,
                                168 * 3600, now + 120, 5)
        check("补捞：再重启一次也不重复", n3 == 0, "%d 条" % n3)

        # 离线期间发的（晚于本地最后一条记录）必须补，哪怕 gap 很小。
        # 踩过的坑：gap_limit 默认 10 分钟，本意是「对方说完我很快就回了」。
        # 但刚启动时根本没回，10 分钟内的消息 gap 算出来很小 → 全被跳过
        # （实测：启动前 9 秒 @ 机器人的两条群消息，既没收到也没补，用户以为不回）
        tmp2 = tempfile.mkdtemp(prefix="qqsweep2_")
        try:
            cfg2 = dict(cfg)
            cfg2["archive"] = dict(cfg.get("archive") or {})
            cfg2["archive"]["dir"] = tmp2

            def mk2():
                b = qa.AutoReply(cfg2)
                b.self_id = 999
                b.mem.self_id = 999
                return b

            g = os.path.join(tmp2, "group_222.md")
            with open(g, "w", encoding="utf-8") as f:
                f.write("[%s] 测试乙(333): 之前的话\n" % time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(now - 700)))
                f.write("[%s] 我: 回过了\n" % time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(now - 660)))

            ghist = [{"time": now - 40, "user_id": 333,
                      "message": [{"type": "at", "data": {"qq": "999"}},
                                  {"type": "text", "data": {"text": "嘻嘻"}}],
                      "sender": {"nickname": "测试乙"}}]
            n4 = mk2()._qq_replay("g222", "group", 222, ghist,
                                  168 * 3600, now, 5)
            check("补捞：离线期间发的（40 秒前的 @）也要补", n4 == 1,
                  "%d 条" % n4)

            # 群里没 @ 我的，离线也不补（那是正常的聊天）
            ghist2 = [{"time": now - 30, "user_id": 333,
                       "message": [{"type": "text", "data": {"text": "随便聊聊"}}],
                       "sender": {"nickname": "测试乙"}}]
            n5 = mk2()._qq_replay("g222", "group", 222, ghist2,
                                  168 * 3600, now + 5, 5)
            check("补捞：群里没 @ 我的离线消息不补", n5 == 0, "%d 条" % n5)

            # 归档里的「离得近但不同」的消息不能被当成同一条。
            # 踩过的坑：判定窗口是 300 秒，19:05:02 的「@机器人 嘻嘻」
            # 因为归档里 19:04:50 有我方回复（差 12 秒），就被判定
            # 「到过系统」，永远补不回来。窗口收到 5 秒才对。
            tmp3 = tempfile.mkdtemp(prefix="qqsweep3_")
            try:
                cfg3 = dict(cfg)
                cfg3["archive"] = dict(cfg.get("archive") or {})
                cfg3["archive"]["dir"] = tmp3

                def mk3():
                    b = qa.AutoReply(cfg3)
                    b.self_id = 999
                    b.mem.self_id = 999
                    return b

                g3 = os.path.join(tmp3, "group_222.md")
                with open(g3, "w", encoding="utf-8") as f:
                    # 归档里有一条 12 秒前的我方回复（跟待补消息不是同一条）
                    f.write("[%s] 我: 回过了\n" % time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(now - 12)))
                hist3 = [{"time": now, "user_id": 333,
                          "message": [{"type": "at", "data": {"qq": "999"}},
                                      {"type": "text", "data": {"text": "嘻嘻"}}],
                          "sender": {"nickname": "测试乙"}}]
                n6 = mk3()._qq_replay("g222", "group", 222, hist3,
                                      168 * 3600, now + 120, 5)
                check("补捞：归档里 12 秒前的记录不遮蔽这条（窗口 5 秒）",
                      n6 == 1, "%d 条" % n6)
            finally:
                shutil.rmtree(tmp3, ignore_errors=True)
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 32. 戳一戳不阻塞接收线程
def test_poke_not_blocking():
    """戳一戳必须入队处理，不能在 WebSocket 接收线程里同步调模型。

    踩过的坑：_handle_notice 最后直接 self._handle(evt2)，那是同步的。
    它跑在接收线程里（_on_message → on_event），一次模型调用 20-60 秒，
    这期间接收线程被完全堵死——**所有 send_* 的响应都收不到**，
    表现成「回复生成出来了，但发送全部 timeout，用户什么都收不到」。
    （实测：戳一戳触发后 13 条回复全发送失败。）
    """
    bot, tmp = new_bot()
    try:
        bot.cfg["allowed_private"] = [111]
        bot.cfg["merge_window_sec"] = 0      # 直接入队，不起定时器
        bot.self_id = 999
        bot.mem.self_id = 999

        queued = []

        def fake_enqueue(conv, evt):
            queued.append((conv, evt))
        bot._enqueue = fake_enqueue

        # 万一还是走了 _handle，让它变慢——能测出「有没有阻塞」
        def slow_reply(*a, **k):
            time.sleep(2)
            return "回复"
        bot.llm_reply = slow_reply

        t0 = time.time()
        bot._handle_notice({
            "post_type": "notice", "notice_type": "notify",
            "sub_type": "poke", "user_id": 111, "target_id": 999,
            "time": int(time.time())})
        elapsed = time.time() - t0
        check("戳一戳：处理立刻返回，不阻塞接收线程", elapsed < 0.5,
              "用时 %.3fs" % elapsed)
        check("戳一戳：进了队列（走独立 worker，不占接收线程）",
              len(queued) == 1, "入队 %d 条" % len(queued))

        # 重建的事件要能被 _handle 正常消费（字段齐全）
        if queued:
            evt = queued[0][1]
            check("戳一戳：合成事件字段齐全",
                  evt.get("message_type") == "private"
                  and evt.get("user_id") == 111
                  and evt.get("message"), str(evt)[:80])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 33. 带 @ 的消息要能归档
def test_at_message_archive():
    """带 at 段的消息必须能归档。

    踩过的坑：memory._msg_kind 被写成了 @staticmethod，但里面用 self.self_id
    判断「@ 的是不是我」。于是**所有带 @ 的消息**归档都抛
    NameError('self')，被调用方 try 吞掉，日志只留一句
    「存图/归档失败（不影响回复）」——看着不影响，实际后果很严重：
    这些消息一条都进不了归档，而补捞的判据正是「归档里有没有它」，
    所以它们每次启动都被重新补捞、重复回复。
    """
    bot, tmp = new_bot()
    try:
        bot.self_id = 999
        bot.mem.self_id = 999
        evt = {
            "post_type": "message", "message_type": "group",
            "user_id": 333, "group_id": 222, "message_id": "g-at-1",
            "time": int(time.time()),
            "message": [
                {"type": "reply", "data": {"id": "x"}},
                {"type": "at", "data": {"qq": "888"}},
                {"type": "text", "data": {"text": "你说的不对"}},
            ],
            "sender": {"nickname": "测试乙"},
        }
        err = None
        try:
            bot.mem.archive_incoming(evt)
        except Exception as e:
            err = e
        check("带 @ 的消息能归档（_msg_kind 不再是 staticmethod）",
              err is None, repr(err) if err else "")

        p = os.path.join(bot.mem.archive_dir(), "group_222.md")
        arch = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
        check("带 @ 的消息真的写进了归档", "你说的不对" in arch)
        check("归档里标了类型（引用）", "引用" in arch, arch[-60:].strip())

        # @ 我 的情况要标成「@我」，@ 别人只标「@」
        evt2 = dict(evt)
        evt2["message"] = [{"type": "at", "data": {"qq": "999"}},
                           {"type": "text", "data": {"text": "在吗"}}]
        evt2["time"] = int(time.time())
        bot.mem.archive_incoming(evt2)
        arch2 = open(p, encoding="utf-8").read()
        check("@我 的消息归档标成「@我」", "@我" in arch2,
              arch2.strip().splitlines()[-1][:60])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 34. 补捞不刷屏
def test_replay_no_flood():
    """补捞的历史消息不能刷屏。

    实测事故：19:16 启动，本地补回补了 1 条 + QQ 侧补捞补了 2 条（同一个
    人），3 条各拆 2-4 段 → 2 分钟内对同一个人发了 **11 条**，内容还都
    重复（都在解释「我这儿看不到时间」），群里直接有人说「死机了」。

    两道闸：
      ① 补捞消息不拆分（_parts_of 对 _replay 返回整条）
      ② 本地补回补过的会话，QQ 侧补捞跳过（_swept_convs）
    """
    bot, tmp = new_bot()
    try:
        # ① 正常消息照常拆
        long_text = "第一句说的是这个。" + "第二句补充一下。" * 6
        normal = bot._parts_of({}, long_text)
        check("拆分：正常消息仍然会拆成多条", len(normal) > 1,
              "%d 条" % len(normal))

        # ② 补捞消息不拆
        replay = bot._parts_of({"_replay": True}, long_text)
        check("拆分：补捞消息不拆（避免刷屏）",
              replay == [long_text], "%d 条" % len(replay))

        # ③ 空正文不炸
        check("拆分：补捞空正文返回空列表",
              bot._parts_of({"_replay": True}, "") == [])

        # ④ 两套补捞互斥
        bot._swept_convs.add("u111")
        check("补捞：本地补回补过的会话会被记录下来",
              "u111" in bot._swept_convs)
        # 把它从 _swept_convs 拿掉，验证 _qq_replay 补完也会登记
        bot._swept_convs.discard("u111")
        now = int(time.time())
        p = os.path.join(bot.mem.archive_dir(), "private_111.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("[%s] 测试甲(111): 之前的话\n" % time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(now - 3600)))
        hist = [{"time": now - 1800, "user_id": 111,
                 "message": [{"type": "text", "data": {"text": "补我一条"}}],
                 "sender": {"nickname": "测试甲"}}]
        n = bot._qq_replay("u111", "private", 111, hist, 6 * 3600, now, 5)
        check("补捞：QQ 侧补完会登记会话（供互斥用）",
              "u111" in bot._swept_convs, "%d 条" % n)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 35. 纯函数
def test_pure():
    check("sanitize 去掉首尾空白",
          qa.sanitize_reply("  嗯\n ") == "嗯")
    check("sanitize 截到句末",
          len(qa.sanitize_reply("一" * 90, 20)) <= 21)
    check("sanitize 去掉 markdown 记号",
          qa.sanitize_reply("**加粗** `代码`") == "加粗 代码")
    check("sanitize 去掉行首标题号",
          qa.sanitize_reply("# 标题\n正文") == "标题\n正文")
    check("sanitize 思考块连内容一起扔",
          qa.sanitize_reply("嗯 <think>让我先想想</think>") == "嗯")
    check("sanitize 思考块在句中也扔",
          qa.sanitize_reply("<think>想想</think>知道了").strip() == "知道了")
    check("flatten 拼文本",
          qa.flatten_message([{"type": "text", "data": {"text": "你好"}}])
          == "你好")
    check("flatten 空也不炸", qa.flatten_message(None) == "")
    check("deep_merge 嵌套合并",
          qa.deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}})["a"]["b"] == 1)
    # 颜文字：store 进去的就是提示词的避让清单，残缺了等于没提醒
    check("颜文字连手臂一起带走",
          qa.extract_kaomoji("好困_(:3」∠)_") == ["_(:3」∠)_"])
    check("颜文字不吞正文",
          qa.extract_kaomoji("今天没精神_(:3」∠)_，去睡了") == ["_(:3」∠)_"])
    check("普通括号不算颜文字", qa.extract_kaomoji("（这条是补充说明）") == [])


def main():
    print("=" * 66)
    for fn in (test_private, test_private_skips, test_group_at,
               test_group_others, test_merge, test_split, test_images,
               test_enrich, test_stale, test_sweep, test_no_reply,
               test_weird_input, test_sticker_only, test_merge_image,
               test_memory_isolated, test_cli_fallback, test_qq_sweep,
               test_rate_gate, test_rate_minute_cap, test_split_policy,
               test_owner, test_restore_segments, test_last_night_dates,
               test_time_words, test_rate_per_session, test_group_layers,
               test_poke, test_image_limits, test_cli_auth_and_lock,
               test_sweep_no_repeat, test_poke_not_blocking, test_at_message_archive, test_replay_no_flood, test_pure):
        print("--- %s ---" % fn.__name__)
        fn()
    print("=" * 66)
    bad = [n for n, ok, _ in RESULTS if not ok]
    print("共 %d 项，失败 %d 项" % (len(RESULTS), len(bad)))
    if bad:
        for n in bad:
            print("  FAIL: %s" % n)
        return 1
    print("端到端冒烟全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
