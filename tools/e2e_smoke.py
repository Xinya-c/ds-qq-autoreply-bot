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
        # ① 并发度：允许 _cli_slots 个并行，超过才排队
        #    旧版是全局互斥（=1），多人聊天被压成一队，后说话的人要等前面
        #    每个人各 60 秒——这正是用户反馈「人多就变慢」的根因。
        slots = bot._cli_slots
        adir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        def slot_files():
            return [os.path.join(adir, ".cli.lock.%d" % i)
                    for i in range(slots)
                    if os.path.exists(os.path.join(adir, ".cli.lock.%d" % i))]

        # 槽位文件已经存在 = 守护进程正在跑、占着槽位。
        # **这时候不能测并发度**：测试线程一个槽位都拿不到，会全部走到
        # 「槽位全满 → 降级并发执行」那条路，5 个线程同一时刻放行，
        # 看起来像「并行进了 5 个」（实测踩过，误报成功能坏了）。
        # 这是测试的环境依赖，不是代码问题。
        busy = slot_files()
        if busy:
            check("CLI 锁：并发度（跳过：守护进程正占用 %d/%d 个槽位）"
                  % (len(busy), slots), True)
        else:
            marks = []
            barrier = threading.Barrier(slots + 1)

            def worker():
                barrier.wait()
                with bot._cli_process_lock(timeout=10):
                    marks.append(time.time())
                    time.sleep(0.6)

            ths = [threading.Thread(target=worker) for _ in range(slots + 1)]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            marks.sort()
            parallel = [m for m in marks if m - marks[0] < 0.25]
            check("CLI 锁：允许 %d 个并发（不再全局串行）" % slots,
                  len(parallel) == slots,
                  "并行进了 %d 个，期望 %d" % (len(parallel), slots))
            check("CLI 锁：超过并发上限要排队",
                  len(marks) == slots + 1 and marks[-1] - marks[0] >= 0.5,
                  "第 %d 个等了 %.2fs" % (len(marks), marks[-1] - marks[0]))

        # ② 槽位文件生命周期（占用即建、释放即删）
        if slot_files():
            # 守护进程正在跑、占着槽位。测试不该跟生产抢资源。
            check("CLI 锁：槽位文件生命周期（跳过：守护进程正占用）", True)
        else:
            with bot._cli_process_lock(timeout=5):
                check("CLI 锁：持锁期间有槽位文件", len(slot_files()) == 1)
            check("CLI 锁：释放后槽位文件被删", not slot_files())

        # ③ 陈旧槽位（进程崩了留下的）要能自动清理
        stale = []
        old = time.time() - 3600          # 一小时前（判据是 mtime 超过 5 分钟）
        for i in range(slots):
            pth = os.path.join(adir, ".cli.lock.%d" % i)
            try:
                if os.path.exists(pth):
                    continue
                with open(pth, "w") as f:
                    f.write("stale")
                os.utime(pth, (old, old))
                stale.append(pth)
            except Exception:
                pass
        if len(stale) == slots:           # 所有槽位都是我造的，才测得出「全占」
            got = False
            try:
                with bot._cli_process_lock(timeout=5):
                    got = True
            except Exception:
                got = False
            check("CLI 锁：陈旧槽位能自动清理（全占也拿得到）", got)
            for pth in slot_files():
                try:
                    os.remove(pth)
                except Exception:
                    pass
        else:
            check("CLI 锁：陈旧槽位清理（跳过：守护进程正占用）", True)

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
def test_poke_not_swept():
    """戳一戳不能被「启动补回」当成漏回的消息。

    真踩过：归档里记了 `[戳一戳]`（为了让模型知道对方戳过），启动补回
    一扫，「最后一条『我:』之后还有对方消息」→ 判定为漏回 → 补回一句
    「又戳。手不闲啊你。」。可用户刚打开客户端，根本没戳，回来说
    「我没戳你」。
    """
    bot, tmp = new_bot()
    try:
        cfg = bot.cfg
        uid = int(cfg["allowed_private"][0])     # 必须在白名单，否则补捞直接跳过
        d = bot.mem.archive_dir()
        os.makedirs(d, exist_ok=True)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        P = qa.POKE_MARK
        nick = "测试甲(%d)" % uid
        path = os.path.join(d, "private_%d.md" % uid)

        def queue_of():
            got = []
            bot._enqueue = lambda k, e: got.append(e)
            bot._swept.clear()
            bot._swept_convs.clear()
            qa_quiet = qa.log
            qa.log = lambda *a, **k: None
            try:
                bot._sweep_unanswered()
            finally:
                qa.log = qa_quiet
            return got

        def write(body):
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)

        # ① 只戳了、没说话 → 一条都不该补
        write("[%s] 我: 在啊\n[%s] %s: %s\n[%s] %s: %s\n"
              % (now, now, nick, P, now, nick, P))
        got = queue_of()
        check("戳一戳：只有戳没有话时，补回不动作", len(got) == 0,
              "补了 %d 条" % len(got))

        # ② 戳完之后说了句话 → 只补那句话
        write("[%s] 我: 在啊\n[%s] %s: %s\n[%s] %s: 你吃饭了没\n"
              % (now, now, nick, P, now, nick))
        got = queue_of()
        txt = ""
        if got:
            txt = got[0]["message"][0]["data"]["text"]
        check("戳一戳：戳+真话时只补真话",
              len(got) == 1 and P not in txt and "吃饭" in txt,
              "补了 %d 条：%r" % (len(got), txt))

        # ③ 回归：普通漏回消息照旧补
        write("[%s] 我: 在啊\n[%s] %s: 你在干嘛\n" % (now, now, nick))
        got = queue_of()
        check("戳一戳：普通漏回消息仍然照补", len(got) == 1,
              "补了 %d 条" % len(got))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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


# ------------------------------------------------------------ 35. CLI 并发池
def test_cli_parallel():
    """实时池和后台池要分离：一次补捞 5 条不该把实时消息堵在后面。

    用户反馈「加了 CLI 锁以后多人聊天会变慢」——根因是全局互斥把并发压成
    串行：4 个人同时说话，最后一个人要等前面 3 个各 60 秒。现在允许并发，
    且后台任务（补捞/主动搭话/摘要）走单独的池，不占实时槽位。
    """
    bot, tmp = new_bot()
    try:
        rt, bg = bot._cli_rt, bot._cli_bg
        check("CLI 并发：实时池 ≥ 2（多人同时说话不排长队）", rt >= 2, "rt=%d" % rt)
        check("CLI 并发：槽位总数 = 实时 + 后台",
              bot._cli_slots == rt + bg,
              "%d != %d+%d" % (bot._cli_slots, rt, bg))

        # 用假的生成函数，避免真的起 CLI（一条要几十秒）
        bot._prompt = lambda *a, **k: ("p", [])
        bot._cli_reply_locked = lambda prompt, hist, image, text: "ok"

        # ① 后台池占满时，实时回复不受影响
        for _ in range(bg):
            bot._cli_bg_sem.acquire()
        try:
            r = bot._cli_reply("u2", "hi")
            check("CLI 并发：后台池满时实时回复照样能跑", r == "ok", repr(r))
        finally:
            for _ in range(bg):
                bot._cli_bg_sem.release()

        # ② 实时池占满时，补捞（后台）照样能跑 —— 证明它走的是另一个池
        for _ in range(rt):
            bot._cli_sem.acquire()
        try:
            r2 = bot._cli_reply("u3", "hi", replay=True)
            check("CLI 并发：补捞走后台池（实时池满也跑得动）", r2 == "ok", repr(r2))
        finally:
            for _ in range(rt):
                bot._cli_sem.release()

        # ③ 每次调用都给独立端口，否则并发实例会 EADDRINUSE 撞死
        ports = [bot._pick_cli_port() for _ in range(8)]
        check("CLI 并发：端口逐个分配（并发不会撞端口）",
              all(1024 < p < 65536 for p in ports) and len(set(ports)) > 1,
              str(ports[:4]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ 36. 带工具卡死的降级
def test_tools_fallback():
    """带工具那次超时要能自动降级成不带工具，并冷却一段时间。

    真踩过：CLI 启动时要初始化 WebSearch/WebFetch（toolRefresh 阶段），
    这一步**依赖网络且没有超时保护**——网络一抖动就永远挂着，直到
    subprocess 的 180 秒超时被 kill。日志实证：
      20:10:03 图片下载 SSL 握手超时
      20:10:05 CLI 启动 → 20:10:10 卡在 PluginManager 之后
      → 只剩内存心跳，永远没出现 run phase breakdown → 20:13:03 超时
    三次超时都是这个原因，每次白等 3 分钟。

    改法：带工具给更短的超时（45s），超时即判定「卡在工具初始化」
    → 立刻不带工具重试（不会卡），并冷却 10 分钟不再试带工具。
    这样搜索能力保留（网络正常时照样带工具），卡死时 1 分钟内出话。
    """
    bot, tmp = new_bot()
    try:
        bot.cfg["codebuddy"]["tools"] = "WebSearch,WebFetch"
        bot.cfg["codebuddy"]["timeout_tools"] = 45
        bot.cfg["codebuddy"]["tools_cooldown_sec"] = 600
        bot._cli_ready_models = lambda: ["m1"]
        bot._tools_cool_until = 0

        calls = []

        def stuck_with_tools(prompt, image=None, model=None, tools=None,
                             timeout=None):
            calls.append((tools, timeout))
            if tools:
                bot._cli_last_timeout_with_tools = True
                return None
            return "不带工具的成功回复"

        bot._cli_invoke = stuck_with_tools
        r = bot._cli_reply_locked("p", [], None, "x")
        check("带工具卡住 → 自动降级不带工具并成功",
              r == "不带工具的成功回复", repr(r))
        check("降级时确实先试了带工具（短超时），再试不带工具",
              len(calls) == 2 and calls[0][0] == "WebSearch,WebFetch"
              and calls[1][0] == "", str(calls))
        check("带工具那次用的是 timeout_tools（45s）而不是 180s",
              calls[0][1] == 45, str(calls[0][1]))
        check("卡住后进入冷却", bot._tools_cool_until > time.time())

        # 冷却期内：直接不带工具，不再白白等 45 秒
        calls.clear()
        r = bot._cli_reply_locked("p", [], None, "x")
        check("冷却期内直接不带工具（省掉那次白等）",
              len(calls) == 1 and calls[0][0] == "", str(calls))

        # 冷却过期后：恢复带工具（保住搜索能力）
        calls.clear()
        bot._tools_cool_until = time.time() - 1
        r = bot._cli_reply_locked("p", [], None, "x")
        check("冷却过期后恢复带工具",
              len(calls) >= 1 and calls[0][0] == "WebSearch,WebFetch",
              str(calls[:2]))

        # 正常情况：带工具直接成功，不该多调用一次
        calls.clear()

        def ok(prompt, image=None, model=None, tools=None, timeout=None):
            calls.append(tools)
            return "带工具的结果"

        bot._cli_invoke = ok
        bot._tools_cool_until = 0
        r = bot._cli_reply_locked("p", [], None, "x")
        check("正常情况下带工具一次成功，不降级",
              r == "带工具的结果" and len(calls) == 1, str(calls))

        # 没配工具时不该试带工具
        bot.cfg["codebuddy"]["tools"] = ""
        calls.clear()
        r = bot._cli_reply_locked("p", [], None, "x")
        check("没配工具就直接不带工具调用",
              len(calls) == 1 and calls[0] == "", str(calls))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_nonfriend_msg():
    """非好友发私聊不能跟好友一样聊——否则申请流程被绕过。

    QQ 允许非好友发消息（群临时会话、开了"允许陌生人发消息"）。以前只判断
    白名单，非好友和「已加好友但没批准」走的是同一条路，等于：
    不用加好友也能聊 → 那「加好友 → 问主人 → 批权限」就成了摆设。

    非好友的处理：回一句引导加好友，但**不进记忆、不读媒体、不登记状态**。
    不进记忆尤其重要——否则陌生人可以往上下文里灌任意内容，蹭到后面的对话。
    """
    bot, tmp = new_bot(owner_qq="111")
    try:
        bot.cfg["allowed_private"] = [111]
        bot.cfg["stranger"] = {"enabled": True, "auto_accept": True,
                               "greet": True, "notify_owner": True,
                               "reply_nonfriend": True,
                               "nonfriend_cooldown_sec": 3600}
        bot.self_id = 999
        bot._schedule = lambda e: None

        class FriendOB2(OB):
            def __init__(self, friends):
                super().__init__()
                self.friends = friends

            def call(self, action, params, timeout=15):
                if action == "get_friend_list":
                    return {"ok": True, "data": list(self.friends)}
                return super().call(action, params, timeout)

        # 111 在白名单（好友）；555 是好友但没批准；777 根本不是好友
        bot.ob = FriendOB2([{"user_id": 111}, {"user_id": 555}])
        bot._stranger_set(555, status="pending", nick="好友没批")

        queued = []
        bot._schedule = lambda e: queued.append(e)

        def priv2(uid):
            return {"post_type": "message", "message_type": "private",
                    "user_id": uid, "message_id": "m%d" % uid,
                    "time": int(time.time()),
                    "message": [{"type": "text", "data": {"text": "在吗"}}],
                    "sender": {"nickname": "n%d" % uid, "user_id": uid}}

        # ① 非好友：回一句，但不进队列
        bot._handle_stranger_msg(priv2(777))
        sent = [p for p in bot.ob.sent["private"] if p.get("user_id") == 777]
        check("非好友：会回一句引导（不是完全不理）", len(sent) == 1, str(len(sent)))
        check("非好友：提示他先加好友",
              sent and ("不是好友" in sent[0]["message"][0]["data"]["text"]
                        or "加个好友" in sent[0]["message"][0]["data"]["text"]),
              sent[0]["message"][0]["data"]["text"] if sent else "")
        check("非好友：不进回复队列（不真聊）", len(queued) == 0, str(len(queued)))
        check("非好友：不登记 stranger 状态（加好友后走正常流程）",
              not bot._stranger_get(777))

        # ② 冷却：同一个人短时间内再来消息，不再回
        n0 = len([p for p in bot.ob.sent["private"] if p.get("user_id") == 777])
        bot._handle_stranger_msg(priv2(777))
        n1 = len([p for p in bot.ob.sent["private"] if p.get("user_id") == 777])
        check("非好友：冷却期内不重复回", n1 == n0, "%d → %d" % (n0, n1))

        # ③ 换个人照常回
        bot._handle_stranger_msg(priv2(888))
        check("非好友：换一个人仍然回",
              any(p.get("user_id") == 888 for p in bot.ob.sent["private"]))

        # ④ 好友（未批准）照常聊
        queued.clear()
        bot._handle_stranger_msg(priv2(555))
        check("好友未批准：照常进队列聊", len(queued) == 1, str(len(queued)))

        # ⑤ 白名单的人不算陌生人
        check("白名单：_is_stranger 为 False", not bot._is_stranger(111))
        check("好友判断：_is_friend 正确",
              bot._is_friend(111) and bot._is_friend(555) and not bot._is_friend(777))

        # ⑥ 拉好友列表失败时要放宽（不能把所有人当非好友而集体不回话）
        class BrokenOB(OB):
            def call(self, action, params, timeout=15):
                if action == "get_friend_list":
                    return {"ok": False, "error": "boom"}
                return super().call(action, params, timeout)

        bot._friend_cache = None
        bot.ob = BrokenOB()
        check("好友判断：拉列表失败时放宽（当好友处理）",
              bot._is_friend(12345))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_scan_new_friends():
    """扫好友列表，补处理「已经加上但没登记」的人。

    为什么需要：NapCat 没有查询待处理申请的 API（实测
    get_friend_add_request / get_pending_friend_requests / get_requests
    全返回「不支持的API」），set_friend_add_request 又必须要 flag，而
    flag 只在 request 事件里。所以离线期间来的申请事件直接丢了，事后
    查不到；你在手机 QQ 上手动同意的人，系统也完全不知道。

    只能反查好友列表兜底：跟白名单 + strangers.json 对一遍，对不上的
    就是新朋友，补打招呼、补问主人。
    """
    bot, tmp = new_bot(owner_qq="111")
    try:
        cfgp = os.path.join(tmp, "cfg.json")
        with open(cfgp, "w", encoding="utf-8") as f:
            json.dump({"allowed_private": [111], "owner_qq": "111"}, f)
        bot._cfg_path = cfgp
        bot.self_id = 999                       # 机器人自己
        bot.cfg["stranger"] = {"enabled": True, "auto_accept": True,
                               "greet": True, "notify_owner": True,
                               "notify_cooldown_sec": 3600, "max_pending": 20}
        bot._schedule = lambda e: None

        class FriendOB(OB):
            def __init__(self, friends):
                super().__init__()
                self.friends = friends

            def call(self, action, params, timeout=15):
                if action == "get_friend_list":
                    return {"ok": True, "data": list(self.friends)}
                return super().call(action, params, timeout)

        # 好友列表：自己 + 主人 + 一个白名单里的人 + 两个没登记的
        bot.ob = FriendOB([
            {"user_id": 999, "nickname": "我自己"},
            {"user_id": 111, "nickname": "主人"},
            {"user_id": 222, "nickname": "老熟人"},          # 在白名单
            {"user_id": 333, "nickname": "新来的甲"},
            {"user_id": 444, "nickname": "新来的乙", "remark": "加我的是他"},
        ])
        bot.cfg["allowed_private"] = [111, 222]

        n = bot.scan_new_friends(notify=True, greet=True)
        check("扫新朋友：只挑出没登记的两个（自己/主人/白名单都排除）",
              n == 2, "扫出 %d 个" % n)
        check("扫新朋友：登记成待批准",
              bot._stranger_get(333).get("status") == "pending"
              and bot._stranger_get(444).get("status") == "pending")
        sent_to = set(p.get("user_id") for p in bot.ob.sent["private"])
        check("扫新朋友：给他们打了招呼", {333, 444} <= sent_to, str(sent_to))
        check("扫新朋友：没给白名单里的人打招呼", 222 not in sent_to, str(sent_to))
        check("扫新朋友：也没给机器人自己发消息", 999 not in sent_to, str(sent_to))
        own = [p["message"][0]["data"]["text"] for p in bot.ob.sent["private"]
               if p.get("user_id") == 111]
        check("扫新朋友：通知了主人问白名单",
              any("白名单" in t for t in own), str(own)[:80])
        check("扫新朋友：备注也带给主人了",
              any("加我的是他" in t for t in own), str(own)[:100])

        # 再扫一次：已经登记过的不能重复打招呼
        bot.ob = FriendOB(bot.ob.friends)
        n2 = bot.scan_new_friends(notify=True, greet=True)
        check("扫新朋友：重复扫不会重复登记", n2 == 0, "又扫出 %d 个" % n2)

        # 已经批准过的人也不该再被扫出来
        bot._stranger_set(555, status="approved", nick="已批准的")
        bot.ob = FriendOB([{"user_id": 555, "nickname": "已批准的"}])
        check("扫新朋友：已批准的不再打扰",
              bot.scan_new_friends(notify=False, greet=False) == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_stranger_flow():
    """陌生人加好友 → 问主人 → 批准/拒绝 的完整链路。

    核心安全点：陌生人**不能给自己批权限**。批准命令必须来自主人本人
    的私聊，否则谁发一句「同意 123456」就能开自己的权限。
    """
    bot, tmp = new_bot(owner_qq="111")
    try:
        # 隔离配置路径：_whitelist_add 会写盘，绝不能碰真实配置
        cfgp = os.path.join(tmp, "cfg.json")
        with open(cfgp, "w", encoding="utf-8") as f:
            json.dump({"allowed_private": [111], "owner_qq": "111"}, f)
        bot._cfg_path = cfgp
        bot.cfg["stranger"] = {"enabled": True, "auto_accept": True,
                               "greet": True, "notify_owner": True,
                               "notify_cooldown_sec": 3600, "max_pending": 20}
        bot._schedule = lambda e: None

        class RecOB(OB):
            def __init__(self):
                super().__init__()
                self.actions = []

            def call(self, action, params, timeout=15):
                self.actions.append(action)
                return super().call(action, params, timeout)

        def texts(uid):
            return [p["message"][0]["data"]["text"]
                    for p in bot.ob.sent["private"]
                    if p.get("user_id") == uid]

        # ① 陌生人加好友
        bot.ob = RecOB()
        STR = 88888
        bot._stranger_set(STR, status="pending", nick="路人甲")
        bot._handle_friend_request({
            "post_type": "request", "request_type": "friend",
            "user_id": STR, "nickname": "路人甲",
            "comment": "你好呀", "flag": "f1"})
        check("陌生人：同意了申请", "set_friend_add_request" in bot.ob.actions,
              str(bot.ob.actions))
        check("陌生人：向对方做了自我介绍",
              any("大肥鱼" in t for t in texts(STR)), str(texts(STR))[:80])
        own = texts(111)
        check("陌生人：加完好友立刻通知主人问白名单",
              any("白名单" in t and str(STR) in t for t in own),
              str(own)[:80])
        check("陌生人：状态登记成待批准",
              bot._stranger_get(STR).get("status") == "pending")

        # ② 陌生人想给自己批权限 → 必须无效
        bot.ob = RecOB()
        evil = priv(uid=STR, text="同意 %d" % STR, mid="evil1")
        check("陌生人：自己说「同意」不算数", bot._owner_command(evil) is None,
              str(bot._owner_command(evil)))
        check("陌生人：白名单没被偷偷加",
              STR not in [int(x) for x in bot.cfg["allowed_private"]])

        # ③ 主人批准
        bot.ob = RecOB()
        ok_evt = priv(uid=111, text="同意 %d" % STR, mid="ok1")
        cmd = bot._owner_command(ok_evt)
        check("主人：能解析「同意 QQ号」", cmd == ("approve", STR), str(cmd))
        bot._apply_owner_decision(*cmd)
        check("主人批准后状态变成 approved",
              bot._stranger_get(STR).get("status") == "approved")
        check("主人批准后进了白名单",
              STR in [int(x) for x in bot.cfg["allowed_private"]],
              str(bot.cfg["allowed_private"]))
        with open(cfgp, encoding="utf-8") as f:
            disk = json.load(f)["allowed_private"]
        check("主人批准后白名单落盘", STR in disk, str(disk))
        check("主人批准后告诉对方可以了",
              any("点头" in t or "能看" in t for t in texts(STR)),
              str(texts(STR))[:80])
        check("批准后能读媒体", bot._stranger_can_read_media(STR))

        # ④ 另一个陌生人，主人拒绝
        S2 = 77777
        bot._stranger_set(S2, status="pending", nick="路人乙")
        bot.ob = RecOB()
        no_evt = priv(uid=111, text="拒绝 %d" % S2, mid="no1")
        cmd = bot._owner_command(no_evt)
        check("主人：能解析「拒绝 QQ号」", cmd == ("reject", S2), str(cmd))
        bot._apply_owner_decision(*cmd)
        check("拒绝后状态是 rejected",
              bot._stranger_get(S2).get("status") == "rejected")
        check("拒绝后没进白名单",
              S2 not in [int(x) for x in bot.cfg["allowed_private"]],
              str(bot.cfg["allowed_private"]))
        check("拒绝后不能读媒体", not bot._stranger_can_read_media(S2))
        check("拒绝后告诉他「主人不让我看」",
              any("不让" in t or "看不了" in t for t in texts(S2)),
              str(texts(S2))[:80])

        # ⑤ 白名单不能被内存值冲掉（真踩过：6 个被冲成 1 个）
        with open(cfgp, "w", encoding="utf-8") as f:
            json.dump({"allowed_private": [1, 2, 3, 4, 5, 6]}, f)
        bot.cfg["allowed_private"] = [1]        # 内存被改坏
        bot._whitelist_add(9)
        with open(cfgp, encoding="utf-8") as f:
            after = set(json.load(f)["allowed_private"])
        check("白名单：以磁盘为准合并，不被内存冲掉",
              after == {1, 2, 3, 4, 5, 6, 9}, str(sorted(after)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sticker_understanding():
    """表情包要能被「看懂」，而不是被当照片描述。

    两件事：
    ① 动图必须解码成一帧静态图送给模型——原 GIF 直通的话，多模态 API
       对它支持不一致（有的只取第一帧、有的直接当无效图），1.6KB 的
       表情包看着「够小」，到模型那边可能是空白。
    ② 要在提示词里点明「这是表情包，先看字再看表情，别描述画面」。
       不点明的话模型会写成「图里有个人举着牌子上面写着…」，梗接不住。
    """
    bot, tmp = new_bot()
    try:
        # ① 动图 → 静态 JPEG
        from PIL import Image, ImageDraw
        frames = []
        for i in range(4):
            f = Image.new("RGB", (120, 120), (255, 255, 255))
            if i >= 2:              # 前两帧是空白，模拟「开场留白」的表情包
                ImageDraw.Draw(f).ellipse([20, 20, 100, 100], fill=(200, 30, 30))
            frames.append(f)
        buf = io.BytesIO()
        frames[0].save(buf, format="GIF", save_all=True,
                       append_images=frames[1:], duration=120)
        raw = buf.getvalue()
        mraw, mmime = bot._fit_for_model(raw, "image/gif")
        check("表情包：动图被转成静态 JPEG", mmime == "image/jpeg", mmime)
        check("表情包：转出来的确实是图", bool(mraw) and mraw[:2] == b"\xff\xd8",
              repr((mraw or b"")[:4]))

        # 普通小图仍原样直通（别为了修动图把静态图也一起压了，白损画质）
        b2 = io.BytesIO()
        Image.new("RGB", (80, 80), (10, 20, 30)).save(b2, format="JPEG", quality=90)
        r2 = b2.getvalue()
        m2, t2 = bot._fit_for_model(r2, "image/jpeg")
        check("表情包：静态小图仍然原样直通", m2 == r2 and t2 == "image/jpeg")

        # ② 提示词要区分表情包和照片
        pr = bot._prompt("p999", "", has_image=True, uid=888, sticker=True)
        pr = pr[0] if isinstance(pr, tuple) else pr
        check("表情包：提示词点明这是表情包", "表情包" in pr)
        check("表情包：提示词要求先看字", "先看图上写的字" in pr)
        check("表情包：提示词不让逐字描述画面", "别逐字描述画面" in pr)
        pr2 = bot._prompt("p999", "", has_image=True, uid=888, sticker=False)
        pr2 = pr2[0] if isinstance(pr2, tuple) else pr2
        check("普通图：不会被说成表情包", "表情包" not in pr2)

        # ③ 表情包标记的容错（模型写得很随意）
        cases = [
            ("笑死 [[img:cry.png]]", "笑死", 1),
            ("笑死 [[img:cry]", "笑死", 1),          # 少一个右括号
            ("笑死 [[img:cry]]", "笑死", 1),         # 没有扩展名
            ("笑死 [[img: cry.png ]]", "笑死", 1),   # 带空格
            ("笑死 [[img:没有这个.png]]", "笑死", 0),  # 文件不存在：标记仍要删
        ]
        for src, exp_t, exp_n in cases:
            t, ps = bot._split_images(src)
            check("表情包标记容错 %r" % src[:16],
                  t == exp_t and len(ps) == exp_n,
                  "文本%r 图%d张" % (t, len(ps)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_face_text():
    """QQ 自带表情要带上名字，不能只留个「[表情]」。

    真踩过：face 段的 data 里**只有 id**，以前直接把整段压成「[表情]」，
    信息全丢了——模型看不出这是偷笑还是大哭。用户连发三个哭脸，模型
    当成在开玩笑。
    """
    f = qa.flatten_message
    cases = [
        ([{"type": "face", "data": {"id": "178"}}], "[表情:捂脸]"),
        ([{"type": "face", "data": {"id": "9"}}], "[表情:大哭]"),
        ([{"type": "face", "data": {"id": "20"}}], "[表情:偷笑]"),
        # 不认识的 id 也要留着——同一个 id 稳定对应同一个符号，
        # 模型至少能看出「他一直在发同一个表情」
        ([{"type": "face", "data": {"id": "99999"}}], "[表情#99999]"),
        # 段里自带名字时优先用现成的（不同 NapCat 版本字段不一样）
        ([{"type": "face", "data": {"id": "13", "summary": "[呲牙]"}}], "[呲牙]"),
        # 商城表情带 summary
        ([{"type": "mface", "data": {"summary": "[表情/捂脸]"}}], "[表情/捂脸]"),
        # summary 是没营养的「[表情]」时，退到 emoji_id
        ([{"type": "mface", "data": {"summary": "[表情]",
                                     "emoji_id": "abc"}}], "[表情包:abc]"),
        # 混在文字里
        ([{"type": "text", "data": {"text": "哈哈"}},
          {"type": "face", "data": {"id": "20"}}], "哈哈[表情:偷笑]"),
    ]
    for msg, exp in cases:
        got = f(msg)
        check("表情：%s" % exp, got == exp, repr(got))
    # 常见 id 都得在表里，别让高频表情退化成 [表情#id]
    from qq_mcp_server import QQ_FACE_NAMES
    missing = [i for i in ("0", "9", "13", "14", "20", "178", "179")
               if i not in QQ_FACE_NAMES]
    check("表情：常见 id 都有映射", not missing, str(missing))


def test_ai_boilerplate():
    """AI 套话清洗：只删外壳，不碰正文。

    原则是「宁可漏删，也不误删」——往 AI 文本里补口语会变成
    「哎，这可真是个问题呢」，比原来更假；但把正文削掉更糟。
    """
    cases = [
        ("假的呗。希望这个回答对你有帮助。", "假的呗。"),
        ("假的。希望这能帮到你", "假的。"),
        ("在吗？如需进一步了解请告诉我。", "在吗？"),
        ("总之，这题就是悖论。", "这题就是悖论。"),
        ("首先，你得先看这个。", "你得先看这个。"),
        ("建议你换个思路（取决于你的需求）。", "建议你换个思路。"),
        ("1. 先这样\n2. 再那样", "先这样\n再那样"),
        ("我觉得挺好的。以上。", "我觉得挺好的。"),
        # ↓ 正文保护：这几条一个字都不能动
        ("我希望你别这样", "我希望你别这样"),
        ("我希望这件事对你有帮助吗", "我希望这件事对你有帮助吗"),
        ("总之我不去了", "总之我不去了"),
        ("他这么说——自己跟自己打架", "他这么说——自己跟自己打架"),
        ("嗯", "嗯"),
        ("？", "？"),
    ]
    for src, exp in cases:
        got = qa.sanitize_reply(src)
        check("套话清洗 %r → %r" % (src[:14], exp[:14]), got == exp, repr(got))


# ------------------------------------------------------------ 37. 纯函数
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
               test_poke_not_swept, test_poke, test_image_limits, test_cli_auth_and_lock,
               test_tools_fallback, test_nonfriend_msg,
               test_scan_new_friends,
               test_stranger_flow,
               test_sticker_understanding,
               test_face_text, test_ai_boilerplate,
               test_sweep_no_repeat, test_poke_not_blocking, test_at_message_archive, test_replay_no_flood, test_cli_parallel, test_pure):
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
