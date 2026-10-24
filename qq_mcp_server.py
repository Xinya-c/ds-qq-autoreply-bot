#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ MCP Bridge —— 零依赖纯 Python 实现（无需 pip install）
=====================================================
把 NapCat (OneBot v11) 暴露的 QQ 收发消息能力，封装成 WorkBuddy 可用的 MCP 工具。

仅使用 Python 标准库，所以你本机任意 Python（如 E:/Python/python.exe）都能直接运行。

--------------------------------------------------------------
配置（环境变量）
--------------------------------------------------------------
  ONEBOT_WS     NapCat 正向 WebSocket 地址，默认 ws://127.0.0.1:3001
                token 可直接写在 URL 中：ws://127.0.0.1:3001/?access_token=你的token
  ONEBOT_TOKEN  （可选）若未写在 URL 里，可用此变量，会以 Authorization: Bearer 发送
  QQ_INBOX      （可选）收件箱归档文件路径，默认脚本同目录 qq_inbox.jsonl

--------------------------------------------------------------
提供的 MCP 工具
--------------------------------------------------------------
  send_private_msg(user_id, message)   发 QQ 私聊消息
  send_group_msg(group_id, message)    发 QQ 群消息
  get_login_info()                     获取当前登录的 QQ 号
  get_group_list()                     获取加入的群列表
  get_friend_list()                    获取好友列表
  get_new_messages(limit, after_seq)   查收 QQ 收到的私聊/群消息（接收消息，内存增量拉取）
  get_inbox_count(after_seq)           查询收件箱里有多少条未读取的新消息
  read_qq_messages(limit, include_archives)  读取自动归档的 QQ 消息文档（按 QQ 号/群号归类）

--------------------------------------------------------------
接收消息的原理
--------------------------------------------------------------
NapCat 的正向 WebSocket 会主动把收到的消息以 OneBot event 形式推过来
（post_type=message）。本服务在后台线程里：
  - 存入内存收件箱（环形缓冲，最多 1000 条，归档到 qq_inbox.jsonl），供 get_new_messages 增量拉取；
  - 同时按「发送者 QQ 号 / 群号」标记后追加写入文档 qq_messages/qq_messages.md。
当前账号发出的消息不会被推送（NapCat 默认 reportSelfMessage=false）。

--------------------------------------------------------------
文档归档与轮转（按 QQ 号 / 群号各自独立文档）
--------------------------------------------------------------
所有收到的消息会按会话分别写入 qq_messages/ 下的独立文档：
  私聊：private_<QQ号>.md        群聊：group_<群号>.md
每个文档内每条格式： [时间] 昵称(QQ号): 文本（群消息会带发送者 QQ）。
当单个文档大小超过 1 MB（可用环境变量 QQ_DOC_MAX_BYTES 调整）时，该文档自动轮转
为 private_<QQ号>_N.md / group_<群号>_N.md（N 递增），并新建文档续写。
用 read_qq_messages(user_id= / group_id=) 读取对应会话；不传则列出全部会话文档。
可用环境变量 QQ_DOC_DIR 改目录。

媒体保存：默认只把图片/语音/视频/文件写成 [类型] 占位标记。开启环境变量
QQ_SAVE_MEDIA=1 后，会把真实媒体下载到 qq_messages/attachments/ 并在文档里写成
「[图片] → attachments/xxx.jpg (12KB)」的引用（NapCat 媒体服务需在线才能下载，
失败则退回占位标记）。

--------------------------------------------------------------
接入 WorkBuddy（在 ~/.workbuddy/mcp.json 中加入，然后到连接器管理里"信任"启用）
--------------------------------------------------------------
{
  "mcpServers": {
    "qq-mcp": {
      "command": "python",
      "args": ["<本仓库的绝对路径>/qq_mcp_server.py"],
      "env": { "ONEBOT_WS": "ws://127.0.0.1:3001" }
    }
  }
}

--------------------------------------------------------------
运行（调试用，正常由 WorkBuddy 拉起）
--------------------------------------------------------------
  python qq_mcp_server.py
"""

import os
import sys
import json
import uuid
import time
import base64
import struct
import socket
import threading
import collections
import itertools
import urllib.parse
import urllib.request
import datetime
import glob


# =====================================================================
# 收件箱（接收消息）
# =====================================================================
MSG_BUFFER = collections.deque(maxlen=1000)
MSG_ID = itertools.count(1)
BUF_LOCK = threading.Lock()
INBOX_FILE = None
_INBOX_FH = None


def init_inbox():
    global INBOX_FILE, _INBOX_FH, MSG_ID
    INBOX_FILE = os.environ.get("QQ_INBOX") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "qq_inbox.jsonl"
    )
    try:
        _INBOX_FH = open(INBOX_FILE, "a", encoding="utf-8")
    except Exception:
        _INBOX_FH = None
    # 启动时常驻加载最近 200 条归档，避免 WorkBuddy 重启期间漏接的消息丢失
    try:
        with open(INBOX_FILE, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        recs = []
        max_seq = 0
        for ln in lines[-200:]:
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            r.pop("raw", None)
            max_seq = max(max_seq, r.get("seq", 0))
            recs.append(r)
        with BUF_LOCK:
            for r in recs:
                MSG_BUFFER.append(r)
        if max_seq:
            MSG_ID = itertools.count(max_seq + 1)
    except Exception:
        pass


def _archive(rec):
    if _INBOX_FH is None:
        return
    try:
        _INBOX_FH.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _INBOX_FH.flush()
    except Exception:
        pass


# QQ 自带表情（face 段）的 id → 名字。
# 为什么需要：face 段的 data 里**只有 id**（没有名字、没有 emoji 字符），
# 直接把整段压成「[表情]」等于把信息全丢了——模型不知道对方发的是
# 偷笑还是大哭，情绪就接不上。
# 只收经典那批有把握的 id；不认识的退化成「[表情#id]」，至少让模型知道
# 「这里有个表情」以及它是不是同一个（同一个 id 会稳定对应同一个符号）。
QQ_FACE_NAMES = {
    "0": "微笑", "1": "撇嘴", "2": "色", "3": "发呆", "4": "得意",
    "5": "流泪", "6": "害羞", "7": "闭嘴", "8": "睡", "9": "大哭",
    "10": "尴尬", "11": "发怒", "12": "调皮", "13": "呲牙", "14": "惊讶",
    "15": "难过", "16": "酷", "17": "冷汗", "18": "抓狂", "19": "吐",
    "20": "偷笑", "21": "可爱", "22": "白眼", "23": "傲慢", "24": "饥饿",
    "25": "困", "26": "惊恐", "27": "流汗", "28": "憨笑", "29": "悠闲",
    "30": "奋斗", "31": "咒骂", "32": "疑问", "33": "嘘", "34": "晕",
    "35": "折磨", "36": "衰", "37": "骷髅", "38": "敲打", "39": "再见",
    "96": "冷汗", "97": "擦汗", "98": "抠鼻", "99": "鼓掌", "100": "糗大了",
    "101": "坏笑", "102": "左哼哼", "103": "右哼哼", "104": "哈欠",
    "105": "鄙视", "106": "委屈", "107": "快哭了", "108": "阴险",
    "109": "亲亲", "110": "吓", "111": "可怜", "112": "菜刀", "113": "西瓜",
    "114": "啤酒", "115": "篮球", "116": "乒乓", "117": "咖啡", "118": "饭",
    "119": "猪头", "120": "玫瑰", "121": "凋谢", "122": "示爱", "123": "爱心",
    "124": "心碎", "125": "蛋糕", "126": "闪电", "127": "炸弹", "128": "刀",
    "129": "足球", "130": "瓢虫", "131": "便便", "132": "月亮", "133": "太阳",
    "134": "礼物", "135": "拥抱", "136": "强", "137": "弱", "138": "握手",
    "139": "胜利", "140": "抱拳", "141": "勾引", "142": "拳头", "143": "差劲",
    "144": "爱你", "145": "NO", "146": "OK", "147": "爱情", "148": "飞吻",
    "149": "跳跳", "150": "发抖", "151": "怄火", "152": "转圈", "153": "磕头",
    "154": "回头", "155": "跳绳", "156": "挥手", "157": "激动", "158": "街舞",
    "159": "献吻", "160": "左太极", "161": "右太极", "162": "开心",
    "163": "双喜", "164": "鞭炮", "165": "灯笼", "166": "发财", "167": "K歌",
    "168": "购物", "169": "邮件", "170": "帅", "171": "喝彩", "172": "祈祷",
    "173": "爆筋", "174": "棒棒糖", "175": "喝奶", "176": "下面",
    "177": "香蕉", "178": "捂脸", "179": "辣眼睛", "180": "哦哟", "181": "头秃",
    "182": "问号脸", "183": "暗中观察", "184": "emm", "185": "吃瓜",
    "186": "呵呵哒", "187": "我酸了", "188": "太南了", "189": "打脸",
    "190": "膜拜", "191": "牛气冲天", "192": "汪汪", "193": "汗",
    "194": "无眼笑", "195": "敬礼", "196": "狂笑", "197": "面无表情",
    "198": "摸鱼", "199": "魔鬼笑", "200": "哦", "201": "请", "202": "睁眼",
    "203": "敲开心", "204": "震惊", "205": "让我看看", "206": "吐血",
    "207": "胡言乱语", "208": "咩", "209": "比心", "210": "笑哭",
    "211": "点赞", "212": "皱眉", "213": "耶", "214": "666",
    "215": "强壮", "216": "原谅我", "217": "抱抱", "218": "无语",
}


def _face_text(data):
    """face 段 → 可读文本。认得就出名字，不认得留 id。"""
    d = data or {}
    fid = str(d.get("id") or d.get("face_id") or "").strip()
    # 有些实现会把名字直接塞在 summary/raw 里，优先用现成的
    for k in ("summary", "text", "name"):
        v = str(d.get(k) or "").strip()
        if v and v not in ("[表情]", "[表情包]"):
            return v
    if not fid:
        return "[表情]"
    name = QQ_FACE_NAMES.get(fid)
    return "[表情:%s]" % name if name else "[表情#%s]" % fid


def _mface_text(data):
    """商城表情（mface）→ 可读文本。这类一般带 summary（如「[表情/捂脸]」）。"""
    d = data or {}
    for k in ("summary", "text"):
        v = str(d.get(k) or "").strip()
        # summary 有时候就是个没信息量的「[表情]」，那就别用它
        if v and v not in ("[表情]", "[表情包]"):
            return v
    name = str(d.get("emoji_id") or d.get("name") or "").strip()
    return "[表情包:%s]" % name if name else "[表情包]"


def flatten_message(msg):
    """把 OneBot 的 message（string 或 array 段）压成纯文本，便于阅读。"""
    if msg is None:
        return ""
    if isinstance(msg, str):
        return msg
    if isinstance(msg, list):
        parts = []
        for seg in msg:
            if not isinstance(seg, dict):
                continue
            t = seg.get("type")
            data = seg.get("data", {}) or {}
            if t == "text":
                parts.append(data.get("text", ""))
            elif t == "at":
                parts.append(f"@{data.get('qq', '')}")
            elif t == "face":
                # 以前这里直接写「[表情]」，把 id 丢了——模型看不出情绪
                # （踩过：对方连发三个 [表情] 全是哭脸，模型当成在开玩笑）
                parts.append(_face_text(data))
            elif t == "image":
                parts.append("[图片]")
            elif t == "record":
                parts.append("[语音]")
            elif t == "video":
                parts.append("[视频（无法查看内容）]")
            elif t == "mface":
                parts.append(_mface_text(data))
            elif t == "file":
                parts.append("[文件]")
            elif t == "reply":
                parts.append("[回复]")
            elif t == "poke":
                # 戳一戳：NapCat 的群戳会以这种段出现在消息里
                parts.append("[戳一戳]")
            elif t == "markdown":
                parts.append(data.get("content", "[Markdown]"))
            elif t == "json":
                parts.append("[JSON卡片]")
            elif t == "forward":
                parts.append("[合并转发]")
            elif t == "contact":
                parts.append("[名片]")
            else:
                parts.append(f"[{t}]")
        return "".join(parts)
    return str(msg)


def record_event(evt):
    """把一条收到的 QQ 消息存入收件箱（后台线程调用）。"""
    if evt.get("post_type") != "message":
        return
    sender = evt.get("sender", {}) or {}
    rec = {
        "seq": next(MSG_ID),
        "time": evt.get("time"),
        "type": evt.get("message_type"),
        "message_id": evt.get("message_id"),
        "user_id": evt.get("user_id"),
        "nickname": sender.get("nickname"),
        "card": sender.get("card"),
        "group_id": evt.get("group_id"),
        "group_name": evt.get("group_name"),
        "text": flatten_message(evt.get("message")),
    }
    with BUF_LOCK:
        MSG_BUFFER.append(rec)
    _archive(rec)
    append_to_doc(rec, evt.get("message"))


def inbox_public_view(after_seq=0, limit=20):
    with BUF_LOCK:
        items = [m for m in MSG_BUFFER if m["seq"] > after_seq]
    items = items[-limit:]
    return {"ok": True, "count": len(items), "messages": items}


def inbox_count(after_seq=0):
    with BUF_LOCK:
        n = sum(1 for m in MSG_BUFFER if m["seq"] > after_seq)
    return {"ok": True, "count": n}


# =====================================================================
# 文档化收件箱（按发送者 QQ 号 / 群号 各自独立文档，每个超大小自动轮转）
# =====================================================================
DOC_DIR = None
ATT_DIR = None
DOC_MAX_BYTES = 1 << 20          # 每个文档上限：1 MB（可用环境变量 QQ_DOC_MAX_BYTES 调整）
SAVE_MEDIA = False               # 是否下载真实图片/语音/视频/文件（环境变量 QQ_SAVE_MEDIA=1 开启）
DOC_WRITE = True                 # 是否写归档文档（QQ_DOC_WRITE=0 关闭，避免与守护进程重复写入）
DOC_LOCK = threading.Lock()


def init_doc():
    global DOC_DIR, ATT_DIR, DOC_MAX_BYTES, SAVE_MEDIA, DOC_WRITE
    DOC_DIR = os.environ.get("QQ_DOC_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "qq_messages"
    )
    os.makedirs(DOC_DIR, exist_ok=True)
    ATT_DIR = os.path.join(DOC_DIR, "attachments")
    os.makedirs(ATT_DIR, exist_ok=True)
    try:
        DOC_MAX_BYTES = int(os.environ.get("QQ_DOC_MAX_BYTES", str(1 << 20)))
    except Exception:
        DOC_MAX_BYTES = 1 << 20
    SAVE_MEDIA = os.environ.get("QQ_SAVE_MEDIA", "0").strip().lower() in ("1", "true", "yes", "on")
    DOC_WRITE = os.environ.get("QQ_DOC_WRITE", "1").strip().lower() not in ("0", "false", "no", "off")


def _fmt_time(t):
    try:
        return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conv_path(conv_type, cid):
    prefix = "private" if conv_type == "private" else "group"
    return os.path.join(DOC_DIR, "%s_%s.md" % (prefix, cid))


def _rot_name(path, n):
    base, _ = os.path.splitext(path)
    return "%s_%d.md" % (base, n)


def _write_conv(path, header, text):
    """写入某个会话的文档：不存在则带 header 新建；超过上限则轮转后新建续写。"""
    with DOC_LOCK:
        if os.path.exists(path):
            if os.path.getsize(path) + len(text.encode("utf-8")) > DOC_MAX_BYTES:
                n = 1
                while os.path.exists(_rot_name(path, n)):
                    n += 1
                os.rename(path, _rot_name(path, n))
                with open(path, "w", encoding="utf-8") as f:
                    f.write(header)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(header)
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def _guess_ext(seg_type, url, name):
    if name and "." in name:
        return os.path.splitext(name)[1][:8]
    std = {
        "image": ".jpg", "record": ".silk", "voice": ".silk",
        "video": ".mp4", "file": ".bin",
    }.get(seg_type)
    if std:
        return std
    if url:
        path = urllib.parse.urlparse(url).path
        if "." in path:
            return os.path.splitext(path)[1][:8]
    return ".bin"


def _save_media(seg, conv_key):
    """尝试把一条媒体段下载到 attachments/，返回 {rel,size} 或 None。"""
    data = seg.get("data", {}) or {}
    seg_type = seg.get("type")
    url = data.get("url") or ""
    file_field = data.get("file") or ""
    name = data.get("name") or ""
    raw = None
    try:
        if url.startswith("http"):
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
        elif file_field.startswith("base64://"):
            raw = base64.b64decode(file_field[len("base64://"):])
        elif file_field.startswith("data:"):
            raw = base64.b64decode(file_field.split(",", 1)[-1])
        elif file_field and (os.path.isabs(file_field) or file_field.startswith("/")):
            with open(file_field, "rb") as f:
                raw = f.read()
    except Exception:
        raw = None
    if not raw:
        return None
    ext = _guess_ext(seg_type, url, name)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = "%s_%s_%s%s" % (conv_key, ts, uuid.uuid4().hex[:6], ext)
    try:
        with open(os.path.join(ATT_DIR, fname), "wb") as f:
            f.write(raw)
    except Exception:
        return None
    return {"rel": "attachments/" + fname, "size": len(raw)}


MEDIA_LABEL = {"image": "图片", "record": "语音", "voice": "语音", "video": "视频", "file": "文件"}


def _format_line_text(rec, message, conv_key):
    """生成文档行的正文：开启 QQ_SAVE_MEDIA 时把图片/文件等下载并写成引用。"""
    if SAVE_MEDIA and isinstance(message, list):
        parts = []
        for seg in message:
            if not isinstance(seg, dict):
                continue
            t = seg.get("type")
            if t in MEDIA_LABEL:
                ref = _save_media(seg, conv_key)
                if ref:
                    kb = ref["size"] // 1024 or 1
                    parts.append("【%s】%s (%dKB)\n" % (MEDIA_LABEL[t], ref["rel"], kb))
                else:
                    parts.append("【%s】" % MEDIA_LABEL[t])
            else:
                parts.append(flatten_message([seg]))
        return "".join(parts).strip()
    return rec.get("text", "")


def append_to_doc(rec, message=None):
    """按会话（QQ 号 / 群号）分别写入独立文档。message 为原始消息段，用于媒体下载。"""
    if not DOC_WRITE:
        return
    try:
        ts = _fmt_time(rec.get("time"))
        if rec.get("type") == "group":
            gid = rec.get("group_id")
            conv_key = "g%s" % gid
            label = (rec.get("group_name") or "群") + "(%s)" % gid
            header = "# QQ 群消息归档 - %s\n\n> 群号 %s。单文件超 %d 字节自动轮转。\n\n" % (
                label, gid, DOC_MAX_BYTES)
            line = "[%s] %s(%s): %s\n" % (
                ts, rec.get("nickname") or "", rec.get("user_id"),
                _format_line_text(rec, message, conv_key))
            _write_conv(_conv_path("group", gid), header, line)
        else:
            uid = rec.get("user_id")
            conv_key = "u%s" % uid
            label = (rec.get("nickname") or "私聊") + "(%s)" % uid
            header = "# QQ 私聊消息归档 - %s\n\n> QQ 号 %s。单文件超 %d 字节自动轮转。\n\n" % (
                label, uid, DOC_MAX_BYTES)
            line = "[%s] %s: %s\n" % (
                ts, rec.get("nickname") or "", _format_line_text(rec, message, conv_key))
            _write_conv(_conv_path("private", uid), header, line)
    except Exception:
        pass


def read_qq_messages(user_id=None, group_id=None, limit=200, include_archives=False):
    """读取某个会话的文档。不给 user_id/group_id 时，列出所有会话文档。"""
    if group_id is not None:
        prefix, cid = "group", group_id
        path = _conv_path("group", cid)
    elif user_id is not None:
        prefix, cid = "private", user_id
        path = _conv_path("private", cid)
    else:
        files = sorted(glob.glob(os.path.join(DOC_DIR, "private_*.md")) +
                       glob.glob(os.path.join(DOC_DIR, "group_*.md")))
        convs = [os.path.basename(f) for f in files]
        return {"ok": True, "mode": "list", "count": len(convs), "conversations": convs}

    def _collect(p):
        out = []
        try:
            with open(p, "r", encoding="utf-8") as f:
                for ln in f:
                    ln = ln.rstrip("\n")
                    if ln.strip() and not ln.startswith("#") and not ln.startswith(">"):
                        out.append(ln)
        except Exception:
            pass
        return out

    entries = _collect(path)
    recent = entries[-max(1, int(limit)):]
    result = {"ok": True, "file": path, "count": len(recent), "messages": recent}
    if include_archives:
        archs = sorted(glob.glob(os.path.join(DOC_DIR, "%s_%s_*.md" % (prefix, cid))))
        older = []
        for a in archs:
            older.extend(_collect(a))
        take = max(0, int(limit) - len(recent))
        older = older[-take:] if take > 0 else []
        result["archives"] = [os.path.basename(a) for a in archs]
        result["messages"] = older + recent
        result["count"] = len(result["messages"])
    return result


# =====================================================================
# 简易 WebSocket 客户端（标准库实现；RFC6455：客户端发送必须掩码）
# =====================================================================
class OneBotWS:
    def __init__(self, url, token=None):
        self.token = token
        self._pending = {}
        self._pending_lock = threading.Lock()
        # 发送锁：多个会话线程会同时往这一个 socket 写。不带锁的话，
        # 大帧（带图回复的 base64 常见上百 KB）sendall 必然分片，
        # 分片之间被别的线程插进来就会把两帧的字节交错，对端收到非法帧
        self._send_lock = threading.Lock()
        self._on_event = None
        self._stop = False

        parsed = urllib.parse.urlparse(url)
        self.host = parsed.hostname
        self.port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        self.path = path

        self.sock = socket.create_connection((self.host, self.port), timeout=10)
        self._handshake()
        # 握手完成后必须取消读超时：否则空闲超过 10 秒就会抛 socket.timeout，
        # 被误判为「连接断开」而不停重连（NapCat 心跳间隔是 30 秒，中间必然空闲）。
        self.sock.settimeout(None)
        self._rthread = threading.Thread(target=self._recv_loop, daemon=True)
        self._rthread.start()
        time.sleep(0.3)  # 等握手/连接稳定

    # ---- 握手 ----
    def _handshake(self):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
        )
        if self.token:
            req += f"Authorization: Bearer {self.token}\r\n"
        req += "\r\n"
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手时无响应")
            buf += chunk
        header = buf.split(b"\r\n\r\n")[0]
        if b"101" not in header.split(b"\r\n")[0]:
            raise RuntimeError("WebSocket 握手失败: " + header.decode(errors="replace"))

    # ---- 发送（客户端必须掩码） ----
    def _send_frame(self, payload: bytes, opcode: int = 0x1):
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        elif length < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", length)
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", length)
        # （掩码 + 发送必须连着做：见上面 _send_lock 的说明）
        with self._send_lock:
            masked = bytes(payload[i] ^ mask[i % 4] for i in range(length))
            self.sock.sendall(header + mask + masked)

    # ---- 接收循环 ----
    def _recv_loop(self):
        try:
            while not self._stop:
                try:
                    frame = self._read_frame()
                except socket.timeout:
                    continue          # 空闲读超时不算断线
                if frame is None:
                    break
                opcode, data = frame
                if opcode == 0x8:            # close
                    break
                elif opcode == 0x9:          # ping -> pong
                    self._send_frame(data, 0xA)
                elif opcode == 0xA:          # pong
                    pass
                elif opcode in (0x1, 0x2):   # text / binary
                    self._on_message(data)
        except Exception:
            pass
        finally:
            # 关键：连接断开时必须置停止标志，否则调用方以为还连着，
            # 会出现「守护进程还活着但再也收不到消息」的假死。
            self._stop = True

    def _read_frame(self):
        hdr = self._recvn(2)
        if not hdr:
            return None
        b0, b1 = hdr[0], hdr[1]
        opcode = b0 & 0x0F
        masked = (b1 & 0x80) >> 7
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recvn(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recvn(8))[0]
        mask = self._recvn(4) if masked else None
        payload = self._recvn(length)
        if payload is None:
            return None
        if masked:
            payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
        return opcode, payload

    def _recvn(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _on_message(self, data: bytes):
        try:
            msg = json.loads(data.decode("utf-8"))
        except Exception:
            return
        if "echo" in msg and msg["echo"] is not None:
            with self._pending_lock:
                fut = self._pending.pop(msg["echo"], None)
            if fut is not None:
                fut["result"] = msg
                fut["event"].set()
        else:
            if self._on_event:
                # 必须包住：这个回调跑在**接收线程**里，它一抛异常就会
                # 一路冒到 _recv_loop，接收线程直接退出（finally 里置
                # _stop=True）。之后 socket 还连着、call() 还能 send，
                # 但**永远收不到响应** → 所有动作都 timeout，
                # 表现为「进程活着、消息发不出去」。
                try:
                    self._on_event(msg)
                except Exception:
                    pass

    # ---- 调用 OneBot action 并等待响应 ----
    def call(self, action, params=None, timeout=15):
        echo = str(uuid.uuid4())
        payload = {"action": action, "params": params or {}, "echo": echo}
        fut = {"event": threading.Event(), "result": None}
        with self._pending_lock:
            self._pending[echo] = fut
        self._send_frame(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if fut["event"].wait(timeout):
            resp = fut["result"] or {}
            if resp.get("status") == "failed" or "data" not in resp:
                return {"ok": False, "raw": resp}
            return {"ok": True, "data": resp.get("data")}
        # 超时时必须摘掉：否则对端卡住期间每次调用都在 _pending 里留一个，
        # 单条连接生命周期内无界增长
        with self._pending_lock:
            self._pending.pop(echo, None)
        return {"ok": False, "error": "timeout"}

    def set_event_handler(self, cb):
        self._on_event = cb

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except Exception:
            pass


# =====================================================================
# MCP 工具定义
# =====================================================================
TOOLS = [
    {
        "name": "send_private_msg",
        "description": "发送 QQ 私聊消息给指定 QQ 号",
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "目标 QQ 号"},
                "message": {"type": "string", "description": "要发送的消息内容"}
            },
            "required": ["user_id", "message"]
        }
    },
    {
        "name": "send_group_msg",
        "description": "发送 QQ 群消息到指定群号",
        "inputSchema": {
            "type": "object",
            "properties": {
                "group_id": {"type": "integer", "description": "目标群号"},
                "message": {"type": "string", "description": "要发送的消息内容"}
            },
            "required": ["group_id", "message"]
        }
    },
    {
        "name": "get_login_info",
        "description": "获取当前已登录的 QQ 账号信息（QQ 号、昵称）",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_group_list",
        "description": "获取当前账号加入的 QQ 群列表",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_friend_list",
        "description": "获取当前账号的 QQ 好友列表",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_new_messages",
        "description": (
            "查收 QQ 收到的私聊/群消息（接收消息能力）。"
            "返回 seq 大于 after_seq 的最近 limit 条消息；不传 after_seq 则取最近 limit 条。"
            "每条包含 seq、时间、类型(private/group)、发送者 QQ 与昵称、群号/群名、文本内容。"
            "拿到 seq 后下次传 after_seq=该 seq 即可增量拉取未读消息。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回条数，默认 20，最大 200"},
                "after_seq": {"type": "integer", "description": "只返回 seq 大于此值的消息，用于增量拉取"}
            }
        }
    },
    {
        "name": "get_inbox_count",
        "description": "查询收件箱里有多少条新消息（seq 大于 after_seq），用于在不取正文的情况下判断是否有未读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "after_seq": {"type": "integer", "description": "基准 seq，只统计大于此值的消息"}
            }
        }
    },
    {
        "name": "set_qq_online_status",
        "description": (
            "设置当前 QQ 账号的在线状态。可选值：在线、Q我吧、离开、忙碌、请勿打扰、隐身、听歌中。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "在线 / Q我吧 / 离开 / 忙碌 / 请勿打扰 / 隐身 / 听歌中",
                    "enum": ["在线", "Q我吧", "离开", "忙碌", "请勿打扰", "隐身", "听歌中"]
                }
            },
            "required": ["status"]
        }
    },
    {
        "name": "read_qq_messages",
        "description": (
            "读取自动归档的 QQ 消息文档。归档按「每个 QQ 号 / 每个群号」各自独立成文档"
            "（private_<QQ号>.md / group_<群号>.md），每个文档超 1MB 自动轮转。"
            "传 group_id 读某个群；传 user_id 读某个私聊；两者都不传则返回所有会话文档列表。"
            "include_archives=true 时合并该会话的历史轮转文件。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "读某个 QQ 号的私聊归档"},
                "group_id": {"type": "integer", "description": "读某个群号的群消息归档"},
                "limit": {"type": "integer", "description": "最多返回条数，默认 200，最大 1000"},
                "include_archives": {"type": "boolean", "description": "是否合并该会话的历史轮转文档"}
            }
        }
    },
]


def tool_call(ob, name, args):
    if name == "send_private_msg":
        return ob.call("send_private_msg", {"user_id": args["user_id"], "message": args["message"]})
    if name == "send_group_msg":
        return ob.call("send_group_msg", {"group_id": args["group_id"], "message": args["message"]})
    if name == "get_login_info":
        return ob.call("get_login_info")
    if name == "get_group_list":
        return ob.call("get_group_list")
    if name == "get_friend_list":
        return ob.call("get_friend_list")
    if name == "get_new_messages":
        limit = min(int(args.get("limit", 20)), 200)
        after = int(args.get("after_seq", 0) or 0)
        return inbox_public_view(after, limit)
    if name == "get_inbox_count":
        return inbox_count(int(args.get("after_seq", 0) or 0))
    if name == "set_qq_online_status":
        mp = {
            "在线": (10, 0, 0), "Q我吧": (60, 0, 0), "离开": (30, 0, 0),
            "忙碌": (50, 0, 0), "请勿打扰": (70, 0, 0), "隐身": (40, 0, 0),
            "听歌中": (10, 1028, 0),
        }
        name_ = args.get("status", "")
        if name_ not in mp:
            return {"ok": False, "error": "未知状态: %s" % name_}
        s, e, b = mp[name_]
        r = ob.call("set_online_status",
                    {"status": s, "ext_status": e, "battery_status": b})
        return {"ok": bool(r.get("ok")), "status": name_, "raw": r}
    if name == "read_qq_messages":
        limit = min(int(args.get("limit", 200)), 1000)
        inc = bool(args.get("include_archives", False))
        uid = args.get("user_id")
        gid = args.get("group_id")
        return read_qq_messages(
            user_id=int(uid) if uid is not None else None,
            group_id=int(gid) if gid is not None else None,
            limit=limit, include_archives=inc,
        )
    return {"ok": False, "error": "unknown tool: " + name}


# =====================================================================
# MCP over stdio（newline-delimited JSON-RPC 2.0）
# =====================================================================
def main():
    init_inbox()
    init_doc()
    ws_url = os.environ.get("ONEBOT_WS", "ws://127.0.0.1:3001")
    token = os.environ.get("ONEBOT_TOKEN")

    try:
        ob = OneBotWS(ws_url, token)
    except Exception as e:
        # 连不上也照常启动 MCP 服务，避免 WorkBuddy 直接判失败；
        # 真正调用工具时再报错，方便排查。
        sys.stderr.write(f"[qq-mcp] OneBot 连接失败: {e}\n")
        sys.stderr.flush()
        ob = None

    if ob:
        ob.set_event_handler(record_event)  # 关键：把收到的 QQ 消息存入收件箱

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue

        method = req.get("method")
        rid = req.get("id")

        if method == "initialize":
            resp = {
                "jsonrpc": "2.0", "id": rid,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "qq-mcp-bridge", "version": "1.4.0"}
                }
            }
        elif method == "ping":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {}}
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
        elif method == "tools/call":
            name = req.get("params", {}).get("name")
            args = req.get("params", {}).get("arguments", {})
            if name in ("get_new_messages", "get_inbox_count"):
                result = tool_call(None, name, args)  # 本地收件箱，不依赖 OneBot
            elif ob is None:
                result = {"ok": False, "error": "OneBot 未连接，请先启动并配置 NapCat OneBot"}
            else:
                result = tool_call(ob, name, args)
            text = json.dumps(result, ensure_ascii=False)
            resp = {
                "jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": text}]}
            }
        else:
            resp = {"jsonrpc": "2.0", "id": rid, "result": {}}

        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    if ob:
        ob.close()


if __name__ == "__main__":
    main()
