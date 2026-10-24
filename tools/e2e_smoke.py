#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/e2e_smoke.py —— 主流程端到端冒烟。

跟 golden_prompt（只盯 prompt 拼装）和 proactive_replay（只盯主动搭话）不同，
这里走真实入口 on_event，把整条链路跑一遍：调度 → 合并 → 队列 → 回复 →
发送 → 归档 → 记忆。模型与 OneBot 全部打桩，不联网、不写真实数据。

  python tools/e2e_smoke.py
"""

import os
import shutil
import sys
import tempfile
import threading
import time

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


# ------------------------------------------------------------ 22. 纯函数
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
               test_owner, test_pure):
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
