#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆模块：QQ 自动回复机器人的全部记忆状态与读写。

2026-10-23 从 qq_autoreply.py **纯搬迁**而来（第一批次：逻辑原样、
零行为变化，由 tools/golden_prompt.py 哈希证明）。主程序只通过
self.mem 访问这里的一切；LLM 调用经构造时注入的 llm_fn（内含模型链
与 429 冷却逻辑，留在主程序）—— 记忆模块不直接碰模型细节。

分层记忆：
  短期  recent       每会话最近 N 条原文（recall 供数，同步写）
  中期  stages       每 200 条压一段阶段回顾
  长期  summary      每 50 条一次事实摘要（按会话）
        profile      关系档案（私聊按人 / 群按群）
        user_profile 群内个人档案（g<gid>|u<uid>，与群级并存）
        style        说话风格档案（全局）
        mood         情绪轨迹（关键词粗判，不花额度）
  归档  qq_messages/*.md  全部消息原文（append-only，1MB 轮转）

第二批（未做）计划：后台 writer 线程、pending 两次命中过滤器、
关键词检索（tools/search_replay.py 已标定阈值）、/forget 命令。
"""

import glob
import json
import math
import os
import queue
import re
import threading
import time
import datetime
from collections import Counter, deque

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE, "qq_autoreply.log")

from qq_mcp_server import flatten_message  # noqa: E402

# 向量检索（可选增强）：依赖/模型缺失时全部降级回关键词检索
try:
    import memory_vector as _mv
except Exception:
    _mv = None

_log_lock = threading.Lock()
ARCH_LOCK = threading.Lock()


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    with _log_lock:
        try:
            print(line, flush=True)
        except Exception:
            pass
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ================= 检索层：归档关键词搜索 =================
# 方案与阈值由 tools/search_replay.py 在真实归档上回放标定（2026-10-23，
# N=5161）：df_max=2% / FLOOR=28.7 / RATIO=0.88 / GAP=0.15 / SPREAD=0.10
#   → 真查询保留 69%、最坏情况（leave-one-out）误注 3~4%。
# 部署形态：DF/idf 用全局语料算（分数尺度稳定，一条 FLOOR 通吃所有会话），
# 候选按当前会话过滤（隔离，不串话）。语料量级变化后重跑回放脚本复测。
SEARCH_DF_MAX_RATIO = 0.02
SEARCH_FLOOR = 28.7
SEARCH_RATIO = 0.88
SEARCH_GAP = 0.15
SEARCH_SPREAD = 0.10
SEARCH_MIN_MATCHED = 2
SEARCH_TOPK = 3
SEARCH_HARD_ARM = 2           # query 硬特征 ≥2 才开硬切（单个会误杀）
SEARCH_PREVIEW_MAX = 90       # 行文超过则围绕命中词截取
SEARCH_INDEX_PER_CONV = 2000  # 每会话索引最近这么多条
# 向量通道阈值（2026-10-24 用真实归档 5166 条标定：无关配对 p50=0.41 /
# p90=0.54；查询对 6/6 命中且去前缀后 0.52~0.84）。语义照搬关键词侧的四路。
VEC_FLOOR = 0.52              # ① 绝对底线
VEC_RATIO = 0.88              # ③ 相对长尾比例
VEC_GAP = 0.08                # ③ 绝对差门槛 / ④ 平局跨度共用
VEC_FAR = 0.75                # ③ 远尾：低于 top1×它直接砍
VEC_SPREAD = 0.05             # ④ 平局跨度
VEC_FLAT_KEEP = 3             # ④ 平局时保留条数
VEC_CAND_K = 4                # 召回候选倍数（去重+过滤后再截 top-k）
# 太短的查询不检索旧记忆。实测噪音全来自极短句（「哈哈」→「嘿嘿」相似度
# 0.712，比正经查询还高），因为短句向量不稳定、彼此都"像"。而真需要翻旧账
# 的查询都是长句。卡长度比调阈值可靠——噪音不一定低分，但一定是短文。
SEARCH_MIN_QUERY_CHARS = 6
VEC_FLUSH_EVERY = 100         # 增量编码多少条落盘一次
FACT_PENDING_MAX = 100        # 候选事实观察区上限（按会话）
FACT_DONE_MAX = 200           # 已提升事实名单上限（按会话，防重复写入）
FACT_MIN_LEN = 8              # 短于 8 字的事实不够格
FACT_SIM = 0.6                # 候选比对的二元组相似度阈值
EXACT_CHARS = set("_-./\\:")
ASCII_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-./\\:]*")
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")
CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
ARCH_LINE_RE = re.compile(r"^\[([^\]]+)\]\s*(.+?):\s?(.*)$")
CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
          "七": 7, "八": 8, "九": 9, "十": 10}


def bigrams_of(text):
    """连续 CJK 串切二元组，非 CJK 字符是天然边界。"""
    out, run = [], []
    for ch in text:
        if CJK_RE.match(ch):
            run.append(ch)
        else:
            out.extend(run[i] + run[i + 1] for i in range(len(run) - 1))
            run = []
    out.extend(run[i] + run[i + 1] for i in range(len(run) - 1))
    return out


def ascii_tokens_of(text):
    """ASCII 词元：路径 / 命令 / 常量名 / 中英混排里的英文词。"""
    out = []
    for m in ASCII_TOKEN_RE.finditer(text):
        raw = m.group(0).strip("._-:/\\")
        if len(raw) >= 2:
            out.append(raw)
    return out


def emojis_of(text):
    return [e for e in EMOJI_RE.findall(text) if e.strip()]


def is_exact(token):
    """含路径/命令类字符的词命中率极高，加权。"""
    return any(c in EXACT_CHARS for c in token)


def tokenize(text):
    """[(token, kind)]，kind ∈ bigram / ascii / emoji。"""
    toks = [(t, "bigram") for t in bigrams_of(text)]
    toks += [(t, "ascii") for t in ascii_tokens_of(text)]
    toks += [(t, "emoji") for t in emojis_of(text)]
    return toks


def hard_tokens_of(query_toks):
    """query 里的硬特征：ASCII 词 + emoji（纯中文泛指词不算）。"""
    return [t for t, kind in query_toks if kind in ("ascii", "emoji")]


def dice(a, b):
    """二元组 Dice 相似度（候选事实比对与检索同一把尺子）。"""
    if a == b:
        return 1.0
    A, B = set(bigrams_of(a)), set(bigrams_of(b))
    if not A or not B:
        return 0.0
    return 2.0 * len(A & B) / (len(A) + len(B))


def _cn_int(s):
    if s.isdigit():
        return int(s)
    if s in CN_NUM:
        return CN_NUM[s]
    m = re.fullmatch(r"([一二三])?十([一二三四五六七八九])?", s)
    if m and (m.group(1) or m.group(2)):
        tens = CN_NUM.get(m.group(1), 1) * 10 if m.group(1) else 10
        return tens + CN_NUM.get(m.group(2), 0)
    return 0


def parse_dates(text, today=None):
    """识别 query 里的日期意图，返回 {YYYY-MM-DD}；无日期表达返回 None。

    认得：2026-10-06 / 2026年10月6日 / 10月6日(号)（晚于今天算去年）/
    今天·昨天·前天·大前天 / N天前（阿拉伯与中文数字，含十天前）。
    """
    if today is None:
        today = datetime.date.today()
    out = set()

    def ok(y, m, d):
        try:
            return datetime.date(int(y), int(m), int(d)).isoformat()
        except (ValueError, TypeError):
            return None

    for y, m, d in re.findall(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", text):
        s = ok(y, m, d)
        if s:
            out.add(s)
    for m, d in re.findall(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[号日]", text):
        s = ok(today.year, m, d)
        if s and s > today.isoformat():
            s = ok(today.year - 1, m, d)
        if s:
            out.add(s)
    for kw, delta in (("大前天", 3), ("前天", 2), ("昨天", 1), ("今天", 0)):
        if kw in text:
            out.add((today - datetime.timedelta(days=delta)).isoformat())
    # 「昨晚」「今天晚上」这类跨夜的：晚上说的、凌晨做的，落在两个日期上。
    # 少了这一条，「我昨晚干了啥」根本触发不了日期通道（实测返回 None）。
    if "昨晚" in text or "昨天晚上" in text or "今天晚上" in text \
            or "今天夜里" in text or "昨天夜里" in text \
            or "今天凌晨" in text or "昨天凌晨" in text:
        out.add((today - datetime.timedelta(days=1)).isoformat())
        out.add(today.isoformat())
    for s in re.findall(r"([0-9]+|[一二两三四五六七八九十]+)\s*天前", text):
        n = _cn_int(s)
        if n:
            out.add((today - datetime.timedelta(days=n)).isoformat())
    return out or None


def parse_time_range(text, now=None):
    """把模糊时间词变成**具体时间范围**，返回 (开始时间戳, 结束时间戳, 说明)。

    比 parse_dates 更细：parse_dates 只给日期，这里给到小时——
    「昨晚」不是一整天，是昨天傍晚到今天凌晨。

    认得：2026-10-06 / 10月6日 / 今天 / 今晚 / 今天晚上 / 昨天 / 昨晚 /
    昨天晚上 / 大前天 / 前天 / 前天晚上 / N天前。
    将来时间（明天/明晚）不认——那段时间还没发生，没有记录可查。

    没有时间词返回 None。
    """
    if not text:
        return None
    now = now or time.time()
    today = datetime.date.fromtimestamp(now)

    def ts(d, h=0, mi=0):
        return time.mktime((d.year, d.month, d.day, h, mi, 0, 0, 0, -1))

    def day(d):
        return ts(d), ts(d + datetime.timedelta(days=1))

    def span(d, a_h, a_m, b_d, b_h, b_m):
        return ts(d, a_h, a_m), ts(b_d, b_h, b_m)

    y = today - datetime.timedelta(days=1)
    y2 = today - datetime.timedelta(days=2)
    y3 = today - datetime.timedelta(days=3)

    # 具体日期优先
    for y_, m, d in re.findall(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", text):
        try:
            dd = datetime.date(int(y_), int(m), int(d))
        except ValueError:
            continue
        s, e = day(dd)
        return s, e, "%s（%s 全天）" % (dd.isoformat(), dd.strftime("%m-%d"))
    for m, d in re.findall(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[号日]", text):
        try:
            dd = datetime.date(today.year, int(m), int(d))
        except ValueError:
            continue
        if dd > today:
            dd = datetime.date(today.year - 1, int(m), int(d))
        s, e = day(dd)
        return s, e, "%s（%s 全天）" % (dd.isoformat(), dd.strftime("%m-%d"))

    if "大前天" in text:
        s, e = day(y3)
        return s, e, "大前天（%s）" % y3.strftime("%m-%d")
    if "前天晚上" in text or "前天夜里" in text:
        s, e = span(y2, 18, 0, y, 6, 0)
        return s, e, "前天晚上（%s 18:00 ~ %s 06:00）" % (y2.strftime("%m-%d"),
                                                           y.strftime("%m-%d"))
    if "昨晚" in text or "昨天晚上" in text or "昨天夜里" in text \
            or "昨天凌晨" in text:
        s, e = span(y, 18, 0, today, 6, 0)
        return s, e, "昨晚（%s 18:00 ~ %s 06:00）" % (y.strftime("%m-%d"),
                                                       today.strftime("%m-%d"))
    if "今天晚上" in text or "今晚" in text or "今天夜里" in text:
        s = ts(today, 18, 0)
        if now < s:
            s = ts(today)
        return s, now, "今天晚上（%s 18:00 至今）" % today.strftime("%m-%d")
    if "前天" in text:
        s, e = day(y2)
        return s, e, "前天（%s）" % y2.strftime("%m-%d")
    if "昨天" in text:
        s, e = day(y)
        return s, e, "昨天（%s）" % y.strftime("%m-%d")
    if "今天" in text or "今早" in text:
        s = ts(today)
        return s, now, "今天（%s 00:00 至今）" % today.strftime("%m-%d")
    for s_ in re.findall(r"([0-9]+|[一二两三四五六七八九十]+)\s*天前", text):
        n = _cn_int(s_)
        if n:
            d = today - datetime.timedelta(days=n)
            a, b = day(d)
            return a, b, "%d 天前（%s）" % (n, d.strftime("%m-%d"))
    return None


def is_placeholder(text):
    """是不是「（图片）」「[表情]」「【图片】attachments/x.gif」这类没内容的占位。

    带进 prompt 只会分散注意力——尤其 person_brief 里，一句「【图片】
    attachments/u…gif (3240.1KB)」会被当成「他说过的话」，毫无意义。
    """
    t = (text or "").strip()
    if not t:
        return True
    if "attachments/" in t:
        return True
    for k in ("[图片]", "[表情]", "[语音]", "[视频", "[文件]", "[JSON", "[合并转发]"):
        if k in t and len(t) <= len(k) + 24:
            return True
    if t[0] in "（(【[" and t[-1] in "）)】]":
        return True
    return False


def preview_around(body, query_toks, before=36, after=76):
    """长行围绕最先出现的命中词截取（固定截开头会让模型误判"词碰上内容没对"）。"""
    if len(body) <= SEARCH_PREVIEW_MAX:
        return body
    pos = len(body)
    for t, _kind in query_toks:
        p = body.find(t)
        if p >= 0:
            pos = min(pos, p)
    if pos >= len(body):
        return body[:SEARCH_PREVIEW_MAX] + "…"
    start = max(0, pos - before)
    end = min(len(body), pos + after)
    return ("…" if start > 0 else "") + body[start:end] + \
           ("…" if end < len(body) else "")


def filter_noise(rows, query_toks, recs):
    """四路噪音过滤（语义照搬回放脚本，已按部署态简化）。

    rows: [(score, matched, rec_idx)] 已按分数降序。
    ① 绝对底线：top1 < FLOOR → 一条不留（宁可不注入，不可错注入）
    ② 硬特征：query 的 ASCII/emoji 词在记录文本里一个都不中 → 砍
       （≥2 个硬特征才启用；命中者豁免长尾）
    ③ 长尾：低于 top1×(1-GAP)，或低于 top1×RATIO 且绝对差够大 → 砍
    ④ 平局：前四条挤在 top1×(1-SPREAD) 内 → 只留前 3
    """
    if not rows:
        return []
    top1 = rows[0][0]
    if top1 < SEARCH_FLOOR:
        return []
    hard = hard_tokens_of(query_toks)
    armed = len(hard) >= SEARCH_HARD_ARM
    kept = []
    for sc, m, i in rows:
        if m < SEARCH_MIN_MATCHED:
            continue
        hits = 0
        if hard:
            hay = recs[i][3]
            hits = sum(1 for t in hard if t in hay)
        if armed and hits == 0:
            continue
        if sc < top1 * (1 - SEARCH_GAP):
            continue
        if hits > 0:
            kept.append((sc, m, i))
            continue
        if sc < top1 * SEARCH_RATIO and top1 - sc > top1 * SEARCH_GAP:
            continue
        kept.append((sc, m, i))
    if len(kept) >= 4 and kept[3][0] >= top1 * (1 - SEARCH_SPREAD):
        kept = kept[:3]
    return kept


def filter_noise_vec(rows):
    """向量通道的四路过滤（阈值用余弦标定值，见 VEC_* 常量）。"""
    if not rows:
        return []
    top1 = rows[0][0]
    if top1 < VEC_FLOOR:
        return []
    kept = []
    for sc, _m, i in rows:
        if sc < top1 * VEC_FAR:
            continue
        if sc < top1 * VEC_RATIO and top1 - sc > VEC_GAP:
            continue
        kept.append((sc, _m, i))
    if len(kept) >= 4 and kept[3][0] >= top1 * (1 - VEC_SPREAD):
        kept = kept[:VEC_FLAT_KEEP]
    return kept


class _SearchIndex:
    """归档倒排索引。record = (conv, ts, nick, body)。

    线程模型：写入持 ARCH_LOCK 追加；检索先取长度快照，再按下标读
    （append-only + GIL 下安全），df 走 dict.get（GIL 原子）。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.recs = []
        self.toks = []
        self.post = {}          # token -> set(record_idx)
        self.df = {}            # token -> 文档频率
        # 向量通道（可选）：embedder/vec 就绪且与 recs 对齐才启用
        self.vec = None
        self.embedder = None
        self._vec_building = False
        self._vec_dirty = 0

    def add(self, conv, ts, nick, body):
        with self.lock:
            i = len(self.recs)
            self.recs.append((conv, ts, nick, body))
            s = set(t for t, _ in tokenize(body))
            self.toks.append(s)
            for t in s:
                self.post.setdefault(t, set()).add(i)
                self.df[t] = self.df.get(t, 0) + 1
        # 向量增量（锁外编码；构建期间跳过，由构建循环统一收尾）
        if self.embedder is not None and self.vec is not None \
                and not self._vec_building:
            try:
                self.vec.extend(self.embedder.encode([body]))
                self._vec_dirty += 1
                if self._vec_dirty >= VEC_FLUSH_EVERY:
                    self.vec.flush()
                    self._vec_dirty = 0
            except Exception:
                pass

    def build(self, records):
        with self.lock:
            self.recs, self.toks, self.post, self.df = [], [], {}, {}
            for conv, ts, nick, body in records:
                i = len(self.recs)
                self.recs.append((conv, ts, nick, body))
                s = set(t for t, _ in tokenize(body))
                self.toks.append(s)
                for t in s:
                    self.post.setdefault(t, set()).add(i)
                    self.df[t] = self.df.get(t, 0) + 1

    def search(self, conv, query, k=SEARCH_TOPK):
        """返回 [(score, rec_idx)]，已过四路过滤；无命中返回 []。

        通道顺序：向量（就绪时）→ 关键词 → 时间通道兜底。
        时间通道放最后是血的教训：「今天不去了，不想吃饭」这种日常句
        含日期词，若日期通道优先会把它劫持成"只要今天的记录"，
        语义匹配整个被跳过。日期通道只服务「前天聊了啥」这类
        语义找不到内容的纯时间问句。
        """
        with self.lock:
            n = len(self.recs)
            if n == 0:
                return []
        if self.vec_ready(n):
            hits = self._vec_search(conv, query, k)
            if hits:
                return hits
        hits = self._kw_search(conv, query, k, n)
        if hits:
            return hits
        days = parse_dates(query)
        if days:
            rows = []
            with self.lock:
                for i in range(n):
                    if self.recs[i][0] == conv and self.recs[i][1][:10] in days:
                        rows.append(i)
            if rows:
                return [(99.0, 1, i) for i in rows[-k:]]
        return []

    def _kw_search(self, conv, query, k, n):
        """关键词通道（向量不可用时的主力，也是兜底）。"""
        q = list(set(tokenize(query)))
        with self.lock:
            df_max = max(2, int(n * SEARCH_DF_MAX_RATIO))
            qc = [(t, kind) for t, kind in q
                  if self.df.get(t, 0) <= df_max]
            if not qc:
                return []
            cand = set()
            for t, _kind in qc:
                cand.update(self.post.get(t, ()))
        if not cand:
            return []
        scored = []
        for i in cand:
            if self.recs[i][0] != conv:
                continue
            s = self.toks[i]
            total, matched = 0.0, 0
            for t, kind in qc:
                if t in s:
                    w = math.log(n / (1.0 + self.df.get(t, 0)))
                    if kind == "ascii" and is_exact(t):
                        w *= 2.2
                    elif kind == "emoji":
                        w *= 1.5
                    total += w
                    matched += 1
            if total > 0:
                scored.append((total, matched, i))
        scored.sort(key=lambda x: -x[0])
        return filter_noise(scored, qc, self.recs)[:k]

    # ---------- 向量通道 ----------
    def vec_ready(self, n=None):
        if self.vec is None or self.embedder is None:
            return False
        if n is None:
            with self.lock:
                n = len(self.recs)
        return self.vec.aligned(n)

    def _vec_search(self, conv, query, k):
        """余弦 top-k + 四路过滤 + 去重。任何异常返回 None（走关键词）。"""
        try:
            qv = self.embedder.encode([query])
            cands = self.vec.search(qv, k=max(k * VEC_CAND_K, k + 3))
            rows, seen = [], set()
            for sc, i in cands:
                if self.recs[i][0] != conv:
                    continue
                body = self.recs[i][3].strip()
                if not body:
                    continue
                key = re.sub(r"\s+", "", body)[:40]     # 去重：同句只留一条
                if key in seen:
                    continue
                seen.add(key)
                rows.append((sc, 1, i))
            return filter_noise_vec(rows)[:k]
        except Exception:
            return None

    def build_vectors(self, embedder, vec):
        """后台把全部 recs 编码进向量索引（首次约十几秒；增量在 add 里）。"""
        if self._vec_building:
            return
        self._vec_building = True
        try:
            self.embedder = embedder
            self.vec = vec
            if vec.aligned(len(self.recs)) and len(self.recs) > 0:
                return                          # 缓存对齐，直接用
            log("向量索引构建中（%d 条）…" % len(self.recs))
            i, first, idle = 0, True, 0
            while idle < 3:
                n = len(self.recs)
                if i >= n:
                    idle += 1
                    time.sleep(0.5)
                    continue
                idle = 0
                batch = self.recs[i:i + 64]
                v = embedder.encode([r[3] for r in batch])
                if first:
                    vec.set_all(v)
                    first = False
                else:
                    vec.extend(v)
                i += len(batch)
                if i % 512 < 64:
                    vec.flush()
            vec.flush()
            log("向量索引就绪：%d 条" % len(vec))
        except Exception as e:
            log("向量索引构建失败，回退关键词检索: %s" % e)
            self.vec = None
            self.embedder = None
        finally:
            self._vec_building = False


class BotMemory:
    """一台机器人的全部记忆。llm_fn(prompt) 由主程序注入。"""

    def __init__(self, cfg, llm_fn):
        self.cfg = cfg
        self.llm_fn = llm_fn
        self.self_id = None              # 主程序连上 OneBot 后回填
        self.history = {}                # conv_key -> deque of messages（API 后端用）
        self.recent = {}                 # conv_key -> deque 最近消息（含未 @ 我的群消息）
        self.summary = {}                # conv_key -> 长期记忆摘要文本
        self.pending = {}
        self.stage_pos = {}              # conv -> 归档里已压缩到第几行（中期记忆游标）
        self.stages = {}                 # conv -> 阶段回顾文本（中期记忆）
        self.profile = {}                # conv -> 关系档案（称呼/亲密度/禁忌）
        self.user_profile = {}           # "g<gid>|u<uid>" -> 群内个人档案（与群级并存）
        self.mood = {}                   # conv -> deque 最近情绪（情绪轨迹）
        self.intimacy = {}                # conv -> {n,pos,neg,neu,first,last} 互动量
        self._aliases = {}                # 名字 -> uid（好友备注/昵称，扫好友列表时填）
        self._name_cache = None           # name_index 的缓存
        self._name_key = None
        self._gm_cache = None             # group_members 缓存
        self._gm_key = None
        self._sm_cache = None             # social_map 缓存
        self._sm_key = None
        self.samples = []                # 手挑的风格样本（self_samples.md，可选）
        self.style = ""                  # 长期养成的说话风格（全局，跨会话）
        self.sent_since_style = 0        # 距上次提炼风格累计说过多少句
        # ---- 第二批：事实观察区 / 后台 writer / 检索索引 ----
        self.fact_pending = {}           # conv -> [归一化候选事实]（两次命中才提升）
        self.fact_done = {}              # conv -> [已提升的事实]（防重复写入）
        self._search = _SearchIndex()
        self._wq = queue.Queue()
        self._wq_keys = set()
        self._wq_lock = threading.Lock()
        self._writer_on = False
        self._archive_dir_cache = None   # 归档目录建过一次就别再 makedirs

    # ---------- 初始化（主程序 run() 里调用） ----------
    def load_all(self):
        self._restore_context()
        self._load_summaries()
        self._load_stages()
        self._load_profiles()
        self._load_intimacy()
        self._load_style()
        self._load_samples()
        self._build_search_index()
        threading.Thread(target=self._ensure_vectors, daemon=True).start()

    def start_backlog(self):
        """启动时把积压的历史补压成阶段回顾（后台线程，不挡着收消息）。"""
        threading.Thread(target=self._backlog_stages, daemon=True).start()

    def start_writer(self):
        """后台 writer 线程：摘要/阶段/风格提炼都归它（串行=最强的锁）。"""
        if self._writer_on:
            return
        self._writer_on = True
        threading.Thread(target=self._writer_loop, daemon=True).start()

    # ---------- 上下文缓冲 ----------
    def allowed(self, evt):
        if evt.get("message_type") == "private":
            return int(evt.get("user_id")) in [int(x) for x in self.cfg.get("allowed_private") or []]
        if evt.get("message_type") == "group":
            return int(evt.get("group_id")) in [int(x) for x in self.cfg.get("allowed_groups") or []]
        return False

    def conv_of(self, evt):
        if evt.get("message_type") == "group":
            return "g%s" % evt.get("group_id")
        return "u%s" % evt.get("user_id")

    def remember_incoming(self, evt):
        """把所有消息（含没 @ 我的群消息）记入上下文缓冲。"""
        if evt.get("_replay"):
            return                      # 补回的事件启动时已回读进 recent，不重复入
        if not self.allowed(evt):
            return
        conv = self.conv_of(evt)
        at_me = False
        for seg in (evt.get("message") or []):
            if isinstance(seg, dict) and seg.get("type") == "at" \
                    and str((seg.get("data") or {}).get("qq")) == str(self.self_id):
                at_me = True
                break
        sender = evt.get("sender") or {}
        text = flatten_message(evt.get("message")).replace("@%s" % self.self_id, "").strip()
        self._note_mood(conv, text)
        self.note_intimacy(conv, text)
        d = self.recent.get(conv)
        if d is None:
            d = deque(maxlen=int(self.cfg.get("context_messages", 12)))
            self.recent[conv] = d
        d.append({
            "mid": evt.get("message_id"),
            "nick": sender.get("card") or sender.get("nickname") or str(evt.get("user_id")),
            "uid": evt.get("user_id"),
            "text": text or "[图片]",
            "at_me": at_me,
            "ts": self._epoch(evt.get("time")),
        })

    def remember_bot(self, conv, text):
        d = self.recent.get(conv)
        if d is None:
            d = deque(maxlen=int(self.cfg.get("context_messages", 12)))
            self.recent[conv] = d
        d.append({"mid": None, "nick": "我", "uid": self.self_id,
                  "text": text, "at_me": False, "ts": time.time()})
        self.sent_since_style += 1

    # ---------- 归档（按会话独立文档，超 1MB 轮转）----------
    @staticmethod
    def _ts(t=None):
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(t)))
        except Exception:
            return time.strftime("%Y-%m-%d %H:%M:%S")

    def archive_dir(self):
        cfg = self.cfg.get("archive") or {}
        d = cfg.get("dir") or os.path.join(BASE, "qq_messages")
        # 每条消息都会走到这里好几次（归档 + 各种 _xxx_path），
        # 而 makedirs 是一次真实系统调用；目录建过一次就够了
        if d != self._archive_dir_cache:
            os.makedirs(d, exist_ok=True)
            self._archive_dir_cache = d
        return d

    def archive_write(self, conv, line):
        cfg = self.cfg.get("archive") or {}
        if not cfg.get("enabled", True):
            return
        try:
            d = self.archive_dir()
            prefix = "group" if conv.startswith("g") else "private"
            cid = conv[1:]
            path = os.path.join(d, "%s_%s.md" % (prefix, cid))
            limit = int(cfg.get("max_bytes", 1048576))
            data = line.encode("utf-8")
            with ARCH_LOCK:
                if os.path.exists(path) and os.path.getsize(path) + len(data) > limit:
                    n = 1
                    while os.path.exists(os.path.join(d, "%s_%s_%d.md" % (prefix, cid, n))):
                        n += 1
                    os.rename(path, os.path.join(d, "%s_%s_%d.md" % (prefix, cid, n)))
                if not os.path.exists(path):
                    title = "QQ 群消息归档" if prefix == "group" else "QQ 私聊归档"
                    with open(path, "w", encoding="utf-8") as f:
                        f.write("# %s - %s\n\n> 单文件超 %d 字节自动轮转为 _N.md。\n\n"
                                % (title, cid, limit))
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception as e:
            log("归档写入失败: %s" % e)
            return
        self._index_line(conv, line)      # 增量进检索索引（写入成功才索引）

    def _msg_kind(self, evt):
        """给消息打类型标记（归档结构化用）：图 / 视频 / 语音 / 文件 / 引用 / @我 / 表情。

        注意：**不能是 @staticmethod**。下面判断「@的是不是我」要用 self.self_id，
        写成 staticmethod 就会抛 NameError('self')，被调用方 try 吞掉，
        表现为「存图/归档失败（不影响回复）」——后果是所有带 @ 的消息
        **一条都进不了归档**，而补捞的判定依据正是「归档里有没有它」，
        于是这些消息每次启动都被重新补捞、重复回复。
        """
        segs = evt.get("message") or []
        types = [s.get("type") for s in segs if isinstance(s, dict)]
        for t, name in (("image", "图"), ("video", "视频"),
                        ("record", "语音"), ("file", "文件")):
            if t in types:
                return name
        if "reply" in types:
            return "引用"
        has_text = any(t == "text" and ((s.get("data") or {}).get("text") or "").strip()
                       for s, t in zip(segs, types) if isinstance(s, dict))
        if "at" in types:
            # 必须看 @ 的是不是我：@ 别人也标成「@我」的话，上下文里会渲染成
            # 「（@了你）」喂给模型，它会以为自己被点名了
            for s in segs:
                if not isinstance(s, dict) or s.get("type") != "at":
                    continue
                if str((s.get("data") or {}).get("qq")) == str(self.self_id):
                    return "@我"
            return "@"
        if not has_text and ("face" in types or "mface" in types):
            return "表情"
        return ""

    def archive_incoming(self, evt):
        conv = self.conv_of(evt)
        sender = evt.get("sender") or {}
        nick = sender.get("card") or sender.get("nickname") or str(evt.get("user_id"))
        text = flatten_message(evt.get("message")).replace("@%s" % self.self_id, "").strip()
        # _image 正常是 {"raw","mime","path","size"}；形状不对照样能归档，别让
        # 一个字段差异把整条消息（连带回复）搞没
        img = evt.get("_image")
        if not isinstance(img, dict):
            img = None
        if img:
            # 有真实图片时，去掉 flatten 产生的 [图片] 占位，避免重复
            text = text.replace("[图片]", "").strip()
        parts = [text] if text else []
        if img and img.get("path"):
            try:
                rel = os.path.relpath(img["path"], self.archive_dir()).replace("\\", "/")
            except Exception:
                rel = os.path.basename(img["path"])
            size = int(img.get("size") or 0)
            size_txt = ("%.1fKB" % (size / 1024.0)) if size >= 1024 \
                else ("%dB" % size)
            parts.append("【图片】%s (%s)" % (rel, size_txt))
        elif evt.get("_no_read_media") and evt.get("_media_saved"):
            # 陌生人发的东西：文件存了，但没给模型看。归档必须**明确写出来**，
            # 否则模型看到空正文只会以为对方发了条空消息，答得莫名其妙。
            parts.append("【附件已保存·无读取权限】%s" % evt["_media_saved"])
        elif evt.get("_has_image") and "[图片]" not in text:
            parts.append("【图片】")
        body = " ".join(parts) if parts else "（空消息）"
        kind = self._msg_kind(evt)
        self.archive_write(conv, "[%s] %s(%s): %s%s\n"
                            % (self._ts(evt.get("time")), nick, evt.get("user_id"),
                               body, ("  〔%s〕" % kind) if kind else ""))
        self.pending[conv] = self.pending.get(conv, 0) + 1

    def archive_outgoing(self, conv, text):
        self.archive_write(conv, "[%s] 我: %s\n" % (self._ts(), text))
        self.pending[conv] = self.pending.get(conv, 0) + 1

    def _restore_context(self):
        """启动时从 qq_messages 里回读最近消息，让重启后仍记得之前的聊天。"""
        if not self.cfg.get("restore_context", True):
            return
        if not (self.cfg.get("archive") or {}).get("enabled", True):
            return
        try:
            d = self.archive_dir()
        except Exception:
            return
        n = int(self.cfg.get("context_messages", 12))
        files = []
        for pat, conv_prefix in (("private_*.md", "u"), ("group_*.md", "g")):
            for p in glob.glob(os.path.join(d, pat)):
                files.append((p, conv_prefix))
        total = 0
        convs = 0
        dropped = 0
        for path, conv_prefix in files:
            base = os.path.basename(path)
            stem = base[:-3]
            # private_123 / group_123，排除轮转出来的 _1.md
            m_ = re.match(r"^(private|group)_(\d+)$", stem)
            if not m_:
                continue
            conv = conv_prefix + m_.group(2)
            try:
                with open(path, encoding="utf-8") as f:
                    lines = f.read().splitlines()
            except Exception:
                continue
            items = []
            # 多读几行：按段切分时，最近几段合起来可能超过 context_messages，
            # 只看最后 N 行会把前一段切掉（主人的隔夜对话就常在 N 行之外）
            for ln in lines[-(n * 4):]:
                mm = re.match(r"^\[([^\]]*)\]\s*(.+?):\s?(.*)$", ln)
                if not mm:
                    continue
                ts_s, who, text = mm.group(1), mm.group(2), mm.group(3)
                # 剥掉结构化类型标记 〔图〕 之类（老归档没有，kind 就是空）
                kind = ""
                km = re.search(r"〔([^〔〕]{1,6})〕\s*$", text)
                if km:
                    kind = km.group(1)
                    text = text[:km.start()].rstrip()
                uid = None
                nick = who
                mu = re.match(r"^(.*)\((\d+)\)$", who)
                if mu:
                    nick, uid = mu.group(1), int(mu.group(2))
                items.append({"mid": None, "nick": nick, "uid": uid,
                              "text": text, "at_me": False, "kind": kind,
                              # 时间戳必须留着：丢了它，模型就分不清
                              # 「33 小时前说的」和「刚说的」，于是抓着旧话题不放
                              "ts": self._parse_ts(ts_s)})
            if not items:
                continue
            # 回读哪一段，用「对话段」而不是绝对时长来判断。
            #
            # 之前用「最近 N 小时」一刀切，踩了两个坑：
            # - 群里隔了 33 小时的旧话题该忘掉（"你问的几百年前的问题"），
            #   但主人隔了 14 小时的私聊该记得（"我昨晚干了啥"）——
            #   同样都是"隔了很久"，性质完全不同，用小时数分不开。
            # 所以改成：相邻消息间隔超过 restore_gap_hours 就算断了一段，
            #   私聊保留最近若干段（我和主人上次聊到哪，隔夜也得记得），
            #   群聊只保留最后一段（群里久远的不是当前话题）。
            mcfg = self.cfg.get("memory") or {}
            gap = float(mcfg.get("restore_gap_hours", 3) or 3) * 3600
            is_priv = conv.startswith("u")
            if is_priv:
                keep_segs = int(mcfg.get("restore_segments", 3) or 3)
            else:
                # 群聊也保留几段：更早的内容进「背景参考」层，
                # 模型知道就行，接话还是按「现场」那几条来
                keep_segs = int(mcfg.get("group_restore_segments", 3) or 3)
            segs = []
            cur = []
            prev_ts = None
            for it in reversed(items):
                ts = it.get("ts")
                if prev_ts is not None and ts and prev_ts - ts > gap:
                    segs.append(cur)
                    cur = []
                cur.append(it)
                prev_ts = ts if ts else prev_ts
            if cur:
                segs.append(cur)
            keep = []
            # 条数预算按段从近到远分：每段先分到一份（后面的段少分点，
            # 越靠后的越不重要），段里再**均匀采样**——只取头尾的话，
            # 段中间的内容（「我把抖音接入给你」这种）会被整段漏掉，
            # 而主人问「我昨晚干了啥」要的恰恰是整段。
            nsegs = min(keep_segs, len(segs))
            budget = n
            for i, s in enumerate(segs[:nsegs]):
                share = budget // (nsegs - i)
                if share <= 0:
                    break
                if len(s) <= share:
                    take = s
                else:
                    # 首尾必含、之间均匀：对话开头（「你睡了吗」）常是关键线索，
                    # 不能因为采样而整个丢掉
                    if share == 1:
                        take = [s[0]]
                    else:
                        last = len(s) - 1
                        take = [s[int(round(j * last / float(share - 1)))]
                                for j in range(share)]
                keep.extend(take)
                budget -= len(take)
            keep.reverse()
            dropped += len(items) - len(keep)
            items = keep
            # 群聊再兜一层时间上限：万一最后一段本身就是很久以前的
            if not is_priv:
                max_hours = float(mcfg.get("restore_max_hours", 24) or 0)
                if max_hours > 0:
                    cutoff = time.time() - max_hours * 3600
                    kept = [it for it in items
                            if it.get("ts") and it["ts"] >= cutoff]
                    dropped += len(items) - len(kept)
                    items = kept
            if not items:
                convs += 1
                continue
            dq = deque(maxlen=n)
            dq.extend(items)
            self.recent[conv] = dq
            total += len(items)
            convs += 1
        if total:
            log("已回读上次聊天记录：%d 个会话 / %d 条%s"
                % (convs, total,
                   ("（另有 %d 条不属于最近的对话，没回读）" % dropped
                    if dropped else "")))

    # ---------- 长期记忆：滚动摘要 ----------
    def _summary_path(self, conv):
        prefix = "group" if conv.startswith("g") else "private"
        return os.path.join(self.archive_dir(), "%s_%s_summary.md" % (prefix, conv[1:]))

    def _cache_summary(self, conv):
        """从摘要文件里取 - 开头的要点行，压成一段放进内存。"""
        cfg = self.cfg.get("summary") or {}
        try:
            with open(self._summary_path(conv), encoding="utf-8") as f:
                lines = [ln.rstrip() for ln in f]
        except Exception:
            self.summary.pop(conv, None)
            return
        pts = [ln for ln in lines if ln.startswith("- ")]
        pts = pts[-int(cfg.get("max_points", 12)):]
        text = "\n".join(pts).strip()
        limit = int(cfg.get("max_chars", 1000))
        if len(text) > limit:
            text = text[:limit] + "…"
        if text:
            self.summary[conv] = text
        else:
            self.summary.pop(conv, None)

    def _load_summaries(self):
        try:
            d = self.archive_dir()
        except Exception:
            return
        for f in os.listdir(d):
            m = re.match(r"^(?:private|group)_(\d+)_summary\.md$", f)
            if not m:
                continue
            num = m.group(1)
            # 文件名无法区分私聊/群？靠前缀判断
            conv = ("u" if f.startswith("private_") else "g") + num
            self._cache_summary(conv)

    # ---------- 说话风格档案（全局，跨会话）----------
    def _style_path(self):
        return os.path.join(self.archive_dir(), "self_style.md")

    def _load_style(self):
        """启动时读回风格档案。它是全局的，不按会话拆。"""
        try:
            with open(self._style_path(), encoding="utf-8") as f:
                lines = [ln.rstrip() for ln in f
                         if ln.strip() and not ln.startswith(("#", ">"))]
        except Exception:
            self.style = ""
            return
        self.style = "\n".join(lines).strip()
        if self.style:
            log("已读回说话风格档案：%d 字" % len(self.style))

    def maybe_update_style(self):
        """用「我自己说过的话」当语料，定期把说话习惯沉淀成一份风格档案。

        跟会话摘要分开存：摘要记的是「跟谁聊过什么」（事实，按会话），
        风格档案记的是「我怎么说话」（习惯，全局共用）。
        """
        cfg = self.cfg.get("style") or {}
        if not cfg.get("enabled", True):
            return
        every = int(cfg.get("every", 40) or 0)
        if every <= 0 or self.sent_since_style < every:
            return
        mine = []
        for d in list(self.recent.values()):
            for x in list(d):
                if x.get("nick") == "我" and (x.get("text") or "").strip():
                    mine.append(x["text"].strip())
        mine = mine[-80:]
        if len(mine) < 10:
            return
        self.sent_since_style = 0
        log("  正在提炼说话风格（%d 句自己的回复）…" % len(mine))
        prompt = (
            "下面是「我」在 QQ 上跟朋友们聊天时说过的话，一行一句。\n"
            "请归纳出「我」稳定下来的说话风格，输出 8-12 行，每行以「- 」开头：\n"
            "1) 只写说话方式：常用句式、怎么称呼对方、语气强弱、颜文字/表情包的使用习惯；\n"
            "2) 遇到夸奖、抱怨、求助、闲聊、被质疑时，分别通常怎么接；\n"
            "3) 不要写任何事实，不要写别人说了什么，只写「我」的语言习惯；\n"
            "4) 看不出来的就不写，不要编造。\n\n"
            + "\n".join(mine)
        )
        res = self.llm_fn(prompt)
        if not res:
            log("  风格提炼失败，稍后再试")
            return
        pts = [ln.strip() for ln in res.splitlines() if ln.strip().startswith("-")]
        if not pts:
            log("  风格提炼结果为空，跳过")
            return
        text = "\n".join(pts)
        limit = int(cfg.get("max_chars", 600) or 0)
        if limit and len(text) > limit:
            text = text[:limit] + "…"
        try:
            with open(self._style_path(), "w", encoding="utf-8") as f:
                f.write("# 说话风格档案\n\n"
                        "> 由我自己说过的回复定期提炼而成，重启后仍然记得。\n\n"
                        + text + "\n")
        except Exception as e:
            log("  风格档案写入失败: %s" % e)
            return
        self.style = text
        log("  说话风格已更新：%d 字" % len(text))

    # ---------- 中期记忆：阶段回顾 ----------
    def _stage_path(self, conv):
        prefix = "group" if conv.startswith("g") else "private"
        return os.path.join(self.archive_dir(), "%s_%s_stage.md" % (prefix, conv[1:]))

    def _load_stages(self):
        try:
            d = self.archive_dir()
        except Exception:
            return
        for f in os.listdir(d):
            m = re.match(r"^(?:private|group)_(\d+)_stage\.md$", f)
            if not m:
                continue
            conv = ("u" if f.startswith("private_") else "g") + m.group(1)
            try:
                with open(os.path.join(d, f), encoding="utf-8") as fh:
                    txt = fh.read()
            except Exception:
                continue
            # 保留 "## 日期" 作为时间锚点；剔掉文件标题（"# "）、说明（">"）和游标（<!-- -->）
            pm = re.search(r"<!--\s*pos:(\d+)\s*-->", txt)
            if pm:
                self.stage_pos[conv] = int(pm.group(1))
            body = [ln.rstrip() for ln in txt.splitlines()
                    if ln.strip() and not ln.startswith("# ")
                    and not ln.startswith(">") and not ln.startswith("<!--")]
            if body:
                self.stages[conv] = "\n".join(body).strip()
        if self.stages:
            n = sum(1 for t in self.stages.values()
                    for ln in t.splitlines() if ln.startswith("## "))
            log("已加载阶段回顾：%d 个会话 / 共 %d 段（全部保留）"
                % (len(self.stages), n))

    def _archive_lines(self, conv):
        prefix = "group" if conv.startswith("g") else "private"
        doc = os.path.join(self.archive_dir(), "%s_%s.md" % (prefix, conv[1:]))
        try:
            with open(doc, encoding="utf-8") as f:
                return [ln.rstrip() for ln in f if ln.startswith("[")]
        except Exception:
            return []

    def _append_stage(self, conv, chunk, new_pos):
        """把一段记录压成阶段回顾，追加进阶段文件并写下进度游标。"""
        cfg = self.cfg.get("memory") or {}
        every = int(cfg.get("stage_every", 200) or 0)
        keep = int(cfg.get("stage_max", 0) or 0)   # 0 = 全部保留
        log("  正在生成阶段回顾（%s，%d 条）…" % (conv, len(chunk)))
        prompt = (
            "下面是一段较早的聊天记录（「我:」是我代替主人说的话）。\n"
            "请用 4-6 行概括这一阶段发生过什么，每行以「- 」开头：\n"
            "只写值得记住的进展、决定、约定和转折；寒暄和重复内容不要写；不要编造。\n\n"
            + "\n".join(chunk)
        )
        res = self.llm_fn(prompt)
        if not res:
            log("  阶段回顾生成失败，稍后再试")
            return False
        pts = [ln.strip() for ln in res.splitlines() if ln.strip().startswith("-")]
        if not pts:
            return False
        head = "## %s" % time.strftime("%Y-%m-%d %H:%M")
        old = ""
        try:
            with open(self._stage_path(conv), encoding="utf-8") as f:
                old = f.read()
        except Exception:
            pass
        # 按 "## 日期" 切块；第一块是文件的标题+说明，要剔除（否则会被当成正文写回去）
        # 另外把可能残留在正文里的游标标记清掉，避免文件里出现多个 pos 标记
        blocks = [re.sub(r"<!--.*?-->", "", b).strip()
                  for b in old.split("\n## ")
                  if b.strip() and not b.lstrip().startswith("# ")]
        blocks = [b for b in blocks if b]
        if keep > 0:
            blocks = blocks[-(keep - 1):] if keep > 1 else []
        try:
            with open(self._stage_path(conv), "w", encoding="utf-8") as f:
                f.write("# 阶段回顾 - %s\n\n> 每 %d 条压缩一段；%s。\n\n"
                        % (conv[1:], every,
                           ("最多保留 %d 段" % keep) if keep > 0 else "全部保留，不丢弃"))
                # 游标必须写在头部：放末尾会被当成上一块的内容重新写回，导致出现多个标记
                f.write("<!-- pos:%d -->\n\n" % new_pos)
                for b in blocks:
                    if not b.startswith("## "):
                        b = "## " + b
                    f.write(b.rstrip() + "\n\n")
                f.write(head + "\n" + "\n".join(pts) + "\n")
        except Exception as e:
            log("  阶段回顾写入失败: %s" % e)
            return False
        return True

    def maybe_update_stage(self, conv):
        """按「已压缩到第几行」推进：新消息到了会压，启动时的历史积压也会补。

        短期=最近 N 条原文；中期=这里；长期=风格档案。三层加起来才不会聊久了失忆。
        返回 True 表示这一轮真的压了一段（调用方据此继续压下一段）。
        """
        cfg = self.cfg.get("memory") or {}
        if not cfg.get("stage_enabled", True):
            return False
        every = int(cfg.get("stage_every", 200) or 0)
        if every <= 0:
            return False
        lines = self._archive_lines(conv)
        pos = self.stage_pos.get(conv, 0)
        if pos > len(lines):          # 归档被轮转/清空过，游标作废
            pos = 0
        if len(lines) - pos < every:
            return False
        chunk = lines[pos:pos + every]
        if len(chunk) < 10:
            return False
        if not self._append_stage(conv, chunk, pos + len(chunk)):
            return False
        self.stage_pos[conv] = pos + len(chunk)
        self._load_stages()
        return True

    def _backlog_stages(self):
        """启动时把积压的历史补压成阶段回顾（后台线程跑，不挡着收消息）。"""
        cfg = self.cfg.get("memory") or {}
        if not cfg.get("stage_enabled", True):
            return
        cap = int(cfg.get("backlog_per_run", 0) or 0)   # 0 = 不限
        convs = []
        for pat, pre in (("private_*.md", "u"), ("group_*.md", "g")):
            for p in glob.glob(os.path.join(self.archive_dir(), pat)):
                m = re.match(r"^(private|group)_(\d+)$", os.path.basename(p)[:-3])
                if m:
                    convs.append(pre + m.group(2))
        done = 0
        for conv in convs:
            n = 0
            while self.maybe_update_stage(conv):
                n += 1
                done += 1
                if cap and done >= cap:
                    log("  本轮已补 %d 段，剩下的下次启动继续" % done)
                    return
            if n:
                log("  历史补压：%s 新增 %d 段阶段回顾" % (conv, n))
        if done:
            log("历史补压完成：本次共 %d 段" % done)

    # ---------- 轻量关系图 ----------
    #
    # 为什么做这个：向量/关键词检索是「找相似的句子」，而真人聊天靠的是
    # **联想**——你说「上次那家店」，对方能顺出「你女朋友不吃辣」「那天排了
    # 两小时」。这个跳跃需要「实体之间有关系」这一层，光靠句子相似度做不到。
    #
    # 为什么是「轻量」：不建全量三元组图谱（那要给每条消息调一次 LLM 抽
    # OpenIE，几千条就是几千次调用）。这里的边是**搭档案提炼的顺风车**产出的
    # ——同一次 LLM 调用里，除了写档案，顺手让它把这个人的关系写成三联行。
    # 零额外 token 成本，节点只有几十个（一个人认识的人和事），够用。
    GRAPH_STOP = frozenset({
        "我", "你", "他", "她", "它", "我们", "你们", "他们", "自己",
        "这个", "那个", "这里", "那里", "现在", "今天", "昨天", "明天",
        "什么", "怎么", "为什么", "一个", "东西", "事情",
    })

    def _graph_path(self, conv):
        prefix = "group" if conv.startswith("g") else "private"
        return os.path.join(self.archive_dir(),
                            "%s_%s_graph.jsonl" % (prefix, conv[1:]))

    def _graph_load(self, conv):
        """读这个会话的边，返回 [(主体, 关系, 客体)]（去重）。"""
        out, seen = [], set()
        try:
            with open(self._graph_path(conv), encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        d = json.loads(ln)
                    except ValueError:
                        continue
                    e = (str(d.get("s") or "").strip(),
                         str(d.get("r") or "").strip(),
                         str(d.get("o") or "").strip())
                    if not all(e) or e in seen:
                        continue
                    seen.add(e)
                    out.append(e)
        except OSError:
            pass
        return out

    def graph_add(self, conv, edges):
        """追加边（自动去重、限总量）。edges: [(s, r, o), ...]"""
        edges = [tuple(str(x).strip() for x in e) for e in (edges or [])
                 if e and len(e) == 3 and all(str(x).strip() for x in e)]
        if not edges:
            return 0
        old = set(self._graph_load(conv))
        new = [e for e in edges if e not in old]
        if not new:
            return 0
        try:
            p = self._graph_path(conv)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                for s, r, o in new:
                    f.write(json.dumps({"s": s, "r": r, "o": o,
                                        "t": int(time.time())},
                                       ensure_ascii=False) + "\n")
        except Exception as e:
            log("  关系图写入失败: %s" % e)
            return 0
        # 边太多就压：只保留最近的 GRAPH_MAX 条（老的图价值低于维护成本）
        try:
            all_edges = self._graph_load(conv)
            if len(all_edges) > self.GRAPH_MAX:
                keep = all_edges[-self.GRAPH_MAX:]
                with open(self._graph_path(conv), "w", encoding="utf-8") as f:
                    for s, r, o in keep:
                        f.write(json.dumps({"s": s, "r": r, "o": o,
                                            "t": int(time.time())},
                                           ensure_ascii=False) + "\n")
        except Exception:
            pass
        return len(new)

    GRAPH_MAX = 300

    def graph_neighbors(self, conv, text, hops=2, limit=10):
        """从 text 里提到的实体出发在图上扩散，返回可读的三元组行。

        匹配方式故意用**子串**而不是分词：实体是「豆豆」「火锅店」这种专名，
        分词反而会把「豆豆」切碎。只要实体出现在这句话里就算命中。

        hops=2 就够：一跳是「他提到的人/物」，两跳是「那个东西又连着什么」。
        再远就发散到不相干了。
        """
        t = (text or "").strip()
        if not t:
            return []
        edges = self._graph_load(conv)
        if not edges:
            return []
        ents = set()
        for s, _r, o in edges:
            ents.add(s)
            ents.add(o)
        seed = {e for e in ents
                if len(e) >= 2 and e not in self.GRAPH_STOP and e in t}
        if not seed:
            return []
        visited = set(seed)
        frontier = set(seed)
        picked = []
        seen = set()
        for _ in range(max(1, int(hops))):
            nxt = set()
            for s, r, o in edges:
                if s not in frontier and o not in frontier:
                    continue
                if (s, r, o) in seen:
                    continue
                seen.add((s, r, o))
                picked.append((s, r, o))
                other = o if s in frontier else s
                if other not in visited:
                    visited.add(other)
                    nxt.add(other)
            frontier = nxt
            if not frontier or len(picked) >= limit:
                break
        return ["- %s - %s - %s" % e for e in picked[:limit]]

    def cross_context(self, uid, exclude_conv=None, max_profiles=3, max_msgs=4):
        """把「这个人在别的场合」的信息拼起来，私聊时注入。

        为什么需要：群里的 conv 是 g<群号>，私聊是 u<QQ>，两本完全独立的账。
        他在群里说「我养了只猫叫豆豆」，私聊时机器人不知道——同一件事要
        重新说一遍，很出戏。

        不新建存储、不动 conv 结构（那会影响补捞、检索索引、档案全套），
        只做一层**读视图**：把已有的群内个人档案和各会话 recent 里的发言
        捞出来拼在一起。

        两处都从内存读，不扫文件、不花额度。
        """
        out = []
        try:
            tag = "|u%d" % int(uid)
        except (TypeError, ValueError):
            return out
        seen = set()
        # ① 各群里的个人档案
        for k, v in self.user_profile.items():
            if not k.endswith(tag) or not (v or "").strip():
                continue
            if exclude_conv and k == exclude_conv:
                continue
            body = " ".join(str(v).split()).lstrip("- ").strip()
            key = body[:40]
            if key in seen:
                continue                  # 同一个群里的档案重复（多群同内容）
            seen.add(key)
            out.append("他在别处提过：%s" % body)
            if len(out) >= max_profiles:
                break
        # ② 他在各群最近说的话（有人味，比档案更像"认识这个人"）
        got = []
        for conv, d in list(self.recent.items()):
            if not conv.startswith("g") or conv == exclude_conv or not d:
                continue
            for m in list(d)[-20:]:
                try:
                    if int(m.get("uid") or 0) != int(uid):
                        continue
                except (TypeError, ValueError):
                    continue
                t = (m.get("text") or "").strip()
                # 占位符（（空消息）（图片）【表情】）没信息量，带上反而是噪音
                if not t or len(t) < 2:
                    continue
                if (t[0] in "（(【" and t[-1] in "）)】"):
                    continue
                if t not in got:
                    got.append(t)
        for t in got[-max_msgs:]:
            out.append("他最近在群里说过：%s" % t[:60])
        return out

    def add_alias(self, name, uid):
        """记一个「名字 → QQ 号」的别名（好友备注 / 本人昵称）。

        为什么需要：主人问「你还记得康程锦吗」用的是**他给的备注**，
        而对方本人在 QQ 上叫「Changin丶Lin」——两个都得认，否则主人用
        自己习惯的称呼问，机器人一脸茫然。
        """
        n = (name or "").strip()
        if not (2 <= len(n) <= 20):
            return
        try:
            self._aliases[n] = int(uid)
        except (TypeError, ValueError):
            pass

    def name_index(self):
        """名字 → QQ 号。好友别名 + 归档里出现过的昵称，合并成一张表。

        归档 recs 的 nick 有两种写法：
            私聊  "小猪"              （conv 本身带 uid）
            群聊  "小猪(2970751548)"  （uid 在括号里）
        两种都要解析，否则群里认识的人在私聊里就认不出来了。

        带缓存：2799 条记录每次都遍历一遍太浪费，用「记录数 + 别名数」当
        版本号，变了才重建。
        """
        key = (len(self._search.recs), len(self._aliases))
        if self._name_cache is not None and self._name_key == key:
            return self._name_cache
        idx = dict(self._aliases)
        pat = re.compile(r"[(（]\s*(\d{5,12})\s*[)）]\s*$")
        for conv, _ts, nick, _body in self._search.recs:
            if not nick or nick == "我":
                continue
            name, uid = "", 0
            m = pat.search(nick)
            if m:
                uid = int(m.group(1))
                name = nick[:m.start()].strip()
            elif conv.startswith("u"):
                try:
                    uid = int(conv[1:])
                except ValueError:
                    continue
                name = nick.strip()
            if uid and 2 <= len(name) <= 20:
                idx.setdefault(name, uid)     # 别名的优先级高于从归档猜的
        self._name_cache = idx
        self._name_key = key
        return idx

    def find_person(self, text):
        """从一句话里认出一个人：返回 (uid, 命中的名字) 或 None。

        按名字长度降序取命中最长的那个——「小猪佩奇」不该被认成「小猪」。
        """
        if not text:
            return None
        best = None
        for name, uid in self.name_index().items():
            if name in text and (best is None or len(name) > len(best[1])):
                best = (uid, name)
        return best

    def group_members(self):
        """g<群号> -> {uid, ...}。从归档 nick 里的「(QQ号)」解析。

        用途：判断 A 和 B 是否有共同场合，决定 A 能问出 B 的多少事。
        """
        key = len(self._search.recs)
        if self._gm_cache is not None and self._gm_key == key:
            return self._gm_cache
        pat = re.compile(r"[(（]\s*(\d{5,12})\s*[)）]\s*$")
        out = {}
        for conv, _ts, nick, _body in self._search.recs:
            if not conv.startswith("g"):
                continue
            m = pat.search(nick or "")
            if not m:
                continue
            out.setdefault(conv[1:], set()).add(int(m.group(1)))
        self._gm_cache = out
        self._gm_key = key
        return out

    def shared_groups(self, uid_a, uid_b):
        """两人都在的群号列表（空 = 没有共同场合）。"""
        try:
            a, b = int(uid_a), int(uid_b)
        except (TypeError, ValueError):
            return []
        return [gid for gid, s in self.group_members().items()
                if a in s and b in s]

    THREAD_GAP = 300               # 线程内两条消息最长间隔（秒）

    def social_map(self):
        """uid -> {peer: {...}}：谁跟谁真的有来往。

        信号按可信度排（依据 Gilbert & Karahalios 2009：亲密度 > 强度 > 结构）：
          threads  一起参与过几段对话    ← 最强：真聊过
          turns    在这些对话里说了几句
          at       @ 次数（降权：@ 只是开场，不代表聊过）
          last     最后一次互动时间（recency 是最强预测因子之一）
          g        共同群（最弱：同一个大群里可能从没说过话）

        **对话线程怎么切**（借 Ubuntu Dialogue Corpus 的做法）：
        @ 只是**开场标记**，不是关系本身。真实情况是「@ 完第一次，后面顺着
        聊了几句」——所以先按 @ 找对话起点，再把后续没 @ 的发言按「发送者
        已经在这个线程里 + 时间连续」归进去。中途第三个人 @ 进来，算加入
        这个线程（同一话题），不是另起一段。
        """
        key = len(self._search.recs)
        if self._sm_cache is not None and self._sm_key == key:
            return self._sm_cache
        pat_nick = re.compile(r"[(（]\s*(\d{5,12})\s*[)）]\s*$")
        pat_at = re.compile(r"@(\d{5,12})")

        # ① 按群分组、解析发送者、按时间排序
        by_group = {}
        for conv, ts, nick, body in self._search.recs:
            if not conv.startswith("g"):
                continue
            m = pat_nick.search(nick or "")
            if not m:
                continue
            try:
                t = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
            except Exception:
                continue
            by_group.setdefault(conv[1:], []).append(
                (t, int(m.group(1)), body or ""))

        out = {}

        def edge(a, b):
            return out.setdefault(a, {}).setdefault(
                b, {"g": set(), "threads": set(), "turns": 0, "at": 0,
                    "last": 0.0})

        for gid, rows in by_group.items():
            rows.sort(key=lambda r: r[0])
            gmem = {r[1] for r in rows}
            active = []
            seq = 0
            for t, sender, body in rows:
                ats = [int(x) for x in pat_at.findall(body)
                       if int(x) not in (sender, self.self_id)]
                # 过期的线程不再接收新消息（冷场就算结束）
                active = [a for a in active
                          if t - a["last"] <= self.THREAD_GAP]
                target = None
                if ats:
                    # 有 @：优先并入「sender 已在其中、且被 @ 的人也在」的线程，
                    # 其次并入「sender 已在其中」的线程——后者就是
                    # 「@ 完第一次，后面接着聊」的情形
                    for pool in (lambda a: sender in a["members"]
                                 and set(ats) & a["members"],
                                 lambda a: sender in a["members"]):
                        for a in active:
                            if pool(a):
                                target = a
                                break
                        if target:
                            break
                    if target is None:
                        seq += 1
                        target = {"members": set(), "last": t, "turns": 0,
                                  "tid": (gid, seq)}
                        active.append(target)
                    target["members"] |= {sender} | set(ats)
                else:
                    for a in active:
                        if sender in a["members"]:
                            target = a
                            break
                if target is None:
                    continue              # 冷场里的一句独白，不构成关系
                target["last"] = t
                target["turns"] += 1
                for other in target["members"]:
                    if other == sender:
                        continue
                    e = edge(sender, other)
                    e["g"].add(gid)
                    e["threads"].add(target["tid"])
                    e["turns"] += 1
                    e["last"] = max(e["last"], t)
                    if other in ats:
                        e["at"] += 1
            # 同群补全（最弱的边：只是在一个群里）
            for a in gmem:
                for b in gmem:
                    if a != b:
                        edge(a, b)["g"].add(gid)
        # 对称化：关系是双向的，但统计时「中途加入线程的人」拿不到早期轮次，
        # 会出现 A→B 3 段、B→A 2 段。取并集补平，免得同一段关系两边说法不一。
        for a, peers in list(out.items()):
            for b, d in list(peers.items()):
                rev = out.get(b, {}).get(a)
                if rev is None:
                    out.setdefault(b, {})[a] = {
                        "g": set(d["g"]), "threads": set(d["threads"]),
                        "turns": d["turns"], "at": d["at"], "last": d["last"]}
                else:
                    d["threads"] |= rev["threads"]
                    d["turns"] = max(d["turns"], rev["turns"])
                    d["at"] = max(d["at"], rev["at"])
                    d["last"] = max(d["last"], rev["last"])
        self._sm_cache = out
        self._sm_key = key
        return out

    def tie_line(self, viewer, uid):
        """给 prompt 用：viewer 和 uid 之间是什么关系（没交集返回空）。

        这个是「关系」，不是「内容」——只描述怎么认识的、来往多密，
        不含任何一方的私密信息，所以哪个场景都能说。

        先报「一起聊过几段」而不是「@ 过几次」：@ 是开场动作，聊起来才是
        关系。只 @ 一次后面没接话的，说明更多是「喊了一声」。
        """
        try:
            d = (self.social_map().get(int(viewer)) or {}).get(int(uid)) or {}
        except (TypeError, ValueError):
            return ""
        th = len(d.get("threads") or ())
        at = int(d.get("at") or 0)
        turns = int(d.get("turns") or 0)
        last = float(d.get("last") or 0)
        gs = sorted(d.get("g") or [])
        parts = []
        # 分级：真聊过 > 只喊过 > 只在一个群
        # （原来写成 `if th: 聊过 / elif at: 喊过`，但只要有 @ 就会建线程，
        #  at 那条分支永远走不到——「喊了一声就走」被算成「聊过一回」。）
        if th >= 2 or (th == 1 and turns >= 4):
            parts.append("一起聊过 %d 回" % th)
            if turns >= 4:
                parts.append("来回说了 %d 句" % turns)
        elif at >= 2:
            parts.append("喊过你 %d 次，没怎么接上话" % at)
        elif th == 1:
            parts.append("聊过一两句")
        elif at:
            parts.append("喊过你 %d 次" % at)
        if last:
            age = time.time() - last
            if age < 3600:
                parts.append("刚才还在说")
            elif age < 86400:
                parts.append("今天聊过")
            elif age < 86400 * 3:
                parts.append("%d 天前聊过" % int(age // 86400))
            elif age > 86400 * 30:
                parts.append("很久没来往了")
        if gs and not th:
            parts.append("都在 %s 群" % "、".join(gs[:2]))
        return ("你跟他的关系：" + "，".join(parts)) if parts else ""

    def person_brief(self, uid, scope=None, viewer=None, max_msgs=4):
        """按 QQ 号取一个人的概况。

        **scope 决定能说多少**（隐私分级，不是性能参数）：
          scope is None   → 全套：私聊档案 + 群档案 + 私聊/群里的发言（主人）
          scope = [gid..] → 只看这些群：群内档案 + 该群里的发言，**不碰私聊归档**
          scope = []      → 什么都不给（没有共同场合，不该假装认识）

        为什么这样分：群里说过的话，当时在场的人都听得见，重复一遍不算泄露；
        私聊内容只有本人和机器人知道，谁问都不能说。所以「认人」可以放开，
        「爆料」必须按场合卡。
        """
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            return []
        conv = "u%d" % uid
        if scope is not None and not scope:
            return []                          # 没有共同场合
        out = []
        # 关系行放最前面：它是「怎么认识的」（同群 + @ 次数），不含任何一方
        # 的私密内容，所以哪个场景都能说。模型看到这行才知道该用哪种口气接。
        if viewer is not None and str(viewer) != str(uid):
            _tie = self.tie_line(viewer, uid)
            if _tie:
                out.append(_tie)
        # ① 私聊关系档案：只有主人能看
        if scope is None:
            prof = " ".join((self.profile.get(conv) or "").split())
            if prof:
                out.append("你跟他在私聊里的关系：%s" % prof[:180])
        # ② 群内个人档案：只给 scope 内的群
        for k, v in self.user_profile.items():
            if not k.endswith("|u%d" % uid):
                continue
            gid = k.split("|", 1)[0][1:]
            if scope is not None and gid not in scope:
                continue
            body = " ".join(str(v or "").split())
            if body:
                out.append("他在群里是这样的：%s" % body[:140])
            break
        # ③ 最近说过的话（私聊的只有主人能看；群里的按 scope）
        d = self.recent.get(conv)
        if d and scope is None:
            said = []
            for m in list(d)[-10:]:
                t = (m.get("text") or "").strip()
                if len(t) < 3 or is_placeholder(t):
                    continue
                said.append(t)
            if said:
                out.append("他最近跟你说过：%s" % " / ".join(said[-max_msgs:])[:200])
        # ④ 群体发言 + 最早那几句
        try:
            recs = [(r[0], r[2], r[3].strip()) for r in self._search.recs]
        except Exception:
            recs = []
        if scope is None:
            want = {conv}
        else:
            want = {"g%s" % g for g in scope}

        def ok(c):
            return c in want

        early, last, seen = [], [], set()
        for c, nick, body in recs:
            if not ok(c) or nick == "我":
                continue
            if len(body) < 3 or is_placeholder(body) or body in seen:
                continue
            # 群里的消息要确认是这个人说的：nick 尾部的 (QQ号)
            if c.startswith("g"):
                mm = re.search(r"[(（]\s*(\d{5,12})\s*[)）]\s*$", nick or "")
                if not mm or int(mm.group(1)) != uid:
                    continue
                body = (nick[:mm.start()] + ": " + body) if mm.start() > 0 else body
            seen.add(body)
            if len(early) < 4:
                early.append(body)
            last.append(body)
        if last and scope is not None:
            out.append("他最近在群里说过：%s" % " / ".join(last[-max_msgs:])[:200])
        if early:
            out.append("他最开始跟你聊时说的是：%s" % " / ".join(early)[:180])
        return out

    # ---------- 关系档案 ----------
    def _profile_path(self, key):
        # key 三种形态："u123" 私聊 | "g123" 群级 | "g123|u456" 群内个人
        if "|" in key:
            g, u = key.split("|", 1)
            return os.path.join(self.archive_dir(),
                                "group_%s_%s_profile.md" % (g[1:], u))
        prefix = "group" if key.startswith("g") else "private"
        return os.path.join(self.archive_dir(), "%s_%s_profile.md" % (prefix, key[1:]))

    def _load_profiles(self):
        try:
            d = self.archive_dir()
        except Exception:
            return
        for f in os.listdir(d):
            m = re.match(r"^(?:private|group)_(\d+)_profile\.md$", f)
            if m:
                conv = ("u" if f.startswith("private_") else "g") + m.group(1)
                target, key = self.profile, conv
            else:
                # 群内个人档案：group_<gid>_u<uid>_profile.md
                m = re.match(r"^group_(\d+)_u(\d+)_profile\.md$", f)
                if not m:
                    continue
                key = "g%s|u%s" % (m.group(1), m.group(2))
                target = self.user_profile
            try:
                with open(os.path.join(d, f), encoding="utf-8") as fh:
                    body = [ln.rstrip() for ln in fh if ln.strip()
                            and not ln.startswith(("#", ">"))]
            except Exception:
                continue
            if body:
                target[key] = "\n".join(body).strip()

    def _write_profile(self, key, text):
        if not text:
            return
        if "|" in key:
            g, u = key.split("|", 1)
            title = "群%s 内的 %s" % (g[1:], u[1:])
        else:
            title = key[1:]
        try:
            with open(self._profile_path(key), "w", encoding="utf-8") as f:
                f.write("# 关系档案 - %s\n\n> 称呼 / 亲密度 / 禁忌话题 / 该记住的事。\n\n"
                        % title + text + "\n")
        except Exception as e:
            log("  关系档案写入失败: %s" % e)
            return
        (self.user_profile if "|" in key else self.profile)[key] = text
        log("  关系档案已更新：%s（%d 字）" % (key, len(text)))

    def _merge_profile(self, key, new_text):
        """自动生成的关系要点与已有档案合并，不清盘。

        手写 seed（人物特点）在前，自动行去重后追加；超上限时从最旧的自动行开始截，
        手写内容只要不手动删就永久保留。
        """
        store = self.user_profile if "|" in key else self.profile
        old_lines = [ln.strip() for ln in (store.get(key) or "").splitlines()
                     if ln.strip()]
        new_lines = [ln.strip() for ln in (new_text or "").splitlines() if ln.strip()]
        seen = set(old_lines)
        merged = list(old_lines)
        for ln in new_lines:
            if ln not in seen:
                seen.add(ln)
                merged.append(ln)
        if len(merged) > 24:                  # 防止无限增长，截最旧的自动行
            merged = merged[-24:]
        # 「共同经历」单独限 4 条：它是事件不是事实，越积越挤占档案空间
        # （24 行里如果塞了十几条经历，称呼/禁忌这些更常用的反而被挤掉）。
        # 留最近 4 条，够模型感觉到「我们一起经历过事」就行。
        ev_idx = [i for i, ln in enumerate(merged) if "【经历】" in ln]
        if len(ev_idx) > 4:
            drop = set(ev_idx[:len(ev_idx) - 4])
            merged = [ln for i, ln in enumerate(merged) if i not in drop]
        self._write_profile(key, "\n".join(merged).strip())

    def _apply_rel(self, conv, rel_lines):
        """把「## 关系」段落的要点按人分流：群里每人一份 + 群级一份。

        LLM 输出两种行：
          - 【QQ号】……    → 并入 group_<gid>_u<qq>_profile.md（群内个人档案）
          - 【群】……      → 并入 group_<gid>_profile.md（群级档案）
        没带标记的行（私聊场景、或 LLM 没按格式写）并入会话档案，行为与从前一致。
        """
        per_user, group_lv, plain = {}, [], []
        for ln in rel_lines:
            s = ln.strip()
            if not s:
                continue
            m = re.match(r"^-\s*【(\d+)】\s*(.*)$", s)
            if m and conv.startswith("g"):
                per_user.setdefault(m.group(1), []).append(
                    "- " + m.group(2).strip())
                continue
            if conv.startswith("g") and re.match(r"^-\s*【群】", s):
                group_lv.append(re.sub(r"^-\s*【群】\s*", "- ", s))
                continue
            plain.append(s)
        for uid, lines_ in per_user.items():
            if lines_:
                self._merge_profile("%s|u%s" % (conv, uid),
                                    "\n".join(lines_).strip())
        if conv.startswith("g"):
            # 群级档案 = 带【群】标记的行 + 没带标记的行
            self._merge_profile(conv, "\n".join(group_lv + plain).strip())
        else:
            self._merge_profile(conv, "\n".join(plain).strip())

    # ---------- 情绪轨迹 ----------
    @staticmethod
    def classify_mood(text):
        """按关键词粗判情绪，返回「愉快 / 不耐烦 / 低落 / 求助 / 平静」。

        抽成静态方法是因为亲密度也要按它归色调——两处各写一份的话，
        以后调关键词只改一处，两边就会给出不一样的判断。
        """
        t = text or ""
        low = t.lower()
        if any(k in t for k in ("哈哈", "233", "笑死", "开心", "太好了", "厉害",
                                "牛", "谢谢", "感谢", "喜欢", "爱你", "棒")):
            return "愉快"
        if any(k in t for k in ("生气", "气死", "滚", "讨厌", "烦死", "有病",
                                "毛病", "傻逼", "sb", "闭嘴")):
            return "不耐烦"
        if any(k in t for k in ("难过", "伤心", "哭", "emo", "累", "无聊",
                               "孤独", "抑郁", "不想", "烦", "崩")):
            return "低落"
        if any(k in low for k in ("?", "？", "为什么", "怎么", "吗", "求", "帮")):
            return "求助"
        return "平静"

    def _note_mood(self, conv, text):
        """按关键词粗判对方情绪，存最近几条形成轨迹（不额外消耗额度）。"""
        cfg = self.cfg.get("memory") or {}
        if not cfg.get("mood_enabled", True):
            return
        m = self.classify_mood(text)
        d = self.mood.get(conv)
        if d is None:
            d = deque(maxlen=6)
            self.mood[conv] = d
        d.append(m)

    def mood_line(self, conv):
        d = self.mood.get(conv)
        if not d:
            return ""
        seq = list(d)
        tail = "、".join(seq[-4:])
        # 最近几条里出现最多的那个，算总体倾向。
        # 平局取最先出现的情绪（Counter 保插入序）——旧实现用 max(set(...))，
        # set 迭代顺序随进程哈希种子漂移，平局时"整体偏什么"每次重启都乱变。
        dom = Counter(seq).most_common(1)[0][0]
        return "最近几条的情绪：%s → 整体偏%s" % (tail, dom)

    # ---------- 亲密度（跟每个人的互动量，纯本地计数不花额度） ----------
    def _intimacy_path(self):
        return os.path.join(self.archive_dir(), "intimacy.json")

    def _load_intimacy(self):
        try:
            with open(self._intimacy_path(), encoding="utf-8") as f:
                d = json.load(f) or {}
            self.intimacy = {k: v for k, v in d.items() if isinstance(v, dict)}
        except Exception:
            self.intimacy = {}

    def _save_intimacy(self):
        """落盘失败不影响运行——亲密度只是锦上添花，不能因为它写不进去就不回话。"""
        try:
            p = self._intimacy_path()
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.intimacy, f, ensure_ascii=False, indent=1)
            os.replace(tmp, p)
        except Exception as e:
            log("  亲密度落盘失败（本次运行仍生效）: %s" % e)

    def note_intimacy(self, conv, text):
        """对方每说一句记一轮，顺带按情绪归一次色调。

        为什么不复用 mood：mood 是最近 6 条的滑窗（看"刚才心情"），
        亲密度是累计（看"认识多久、整体合不合得来"），两者生命周期不同。
        每 20 轮才落一次盘——每条都写文件纯属浪费磁盘 IO。
        """
        if not (self.cfg.get("memory") or {}).get("intimacy_enabled", True):
            return
        d = self.intimacy.get(conv)
        if d is None:
            d = self.intimacy[conv] = {"n": 0, "pos": 0, "neg": 0, "neu": 0,
                                       "first": time.time(), "last": 0}
        d["n"] = int(d.get("n") or 0) + 1
        d["last"] = time.time()
        d.setdefault("first", time.time())
        tone = self.classify_mood(text)
        if tone == "愉快":
            d["pos"] = int(d.get("pos") or 0) + 1
        elif tone in ("不耐烦", "低落"):
            d["neg"] = int(d.get("neg") or 0) + 1
        else:
            d["neu"] = int(d.get("neu") or 0) + 1
        if int(d["n"]) % 20 == 0:
            self._save_intimacy()

    def intimacy_line(self, conv, subject="跟他"):
        """给 prompt 用的一行。没有数据就返回空（不注入，别占地方）。

        subject 由调用方给：私聊是「跟他」，群聊是「在这个群」——同一个计数
        在两处的含义不一样（群聊里的 conv 是群，不是某个人）。
        """
        d = self.intimacy.get(conv)
        if not d or not int(d.get("n") or 0):
            return ""
        n = int(d["n"])
        if n < 15:
            stage = "刚认识"
        elif n < 60:
            stage = "聊过几回"
        elif n < 200:
            stage = "挺熟的"
        elif n < 600:
            stage = "老熟人"
        else:
            stage = "认识很久了"
        days = 0
        try:
            days = int((time.time() - float(d.get("first") or time.time())) / 86400)
        except Exception:
            days = 0
        born = ("，认识 %d 天" % days) if days >= 1 else ""
        pos, neg = int(d.get("pos") or 0), int(d.get("neg") or 0)
        tone = ""
        if pos + neg >= 10:                 # 样本太少不下结论，免得一次吵架就定性
            r = pos / float(pos + neg)
            if r >= 0.6:
                tone = "，聊起来大多挺轻松"
            elif r <= 0.3:
                tone = "，最近几回情绪都不太好"
        return "%s聊过 %d 轮%s，%s%s" % (subject, n, born, stage, tone)

    # ---------- Few-shot 风格样本 ----------
    def _load_samples(self):
        """手挑的样本优先（self_samples.md），没有就用下面动态挑选的。"""
        try:
            with open(os.path.join(self.archive_dir(), "self_samples.md"),
                      encoding="utf-8") as f:
                lines = [ln.rstrip() for ln in f
                         if ln.strip() and not ln.startswith(("#", ">"))]
        except Exception:
            self.samples = []
            return
        self.samples = [ln[2:].strip() if ln.startswith("- ") else ln
                        for ln in lines]

    def style_samples(self, conv, n=3):
        """挑几条「我」说得好的原话当样本，比抽象描述更能把风格钉住。

        偏好：长度适中、自然口语的句子（不刻意堆语气词/颜文字）。
        """
        from qq_autoreply import extract_kaomoji  # noqa: E402 避免循环导入
        cfg = self.cfg.get("memory") or {}
        n = int(cfg.get("samples", 3) or 0)
        if n <= 0:
            return []
        if self.samples:
            return self.samples[:n]
        pool = []
        for c, d in list(self.recent.items()):
            for x in list(d):
                if x.get("nick") != "我":
                    continue
                t = (x.get("text") or "").strip()
                if not t or "【图片】" in t:
                    continue
                score = 0
                if 8 <= len(t) <= 70:
                    score += 2
                if extract_kaomoji(t):
                    score += 2
                if any(k in t for k in ("才", "哼", "诶", "嘛", "呀")):
                    score += 1
                pool.append((score + (2 if c == conv else 0), t))
        if not pool:
            return []
        pool.sort(key=lambda x: -x[0])
        out, seen = [], set()
        for _, t in pool:
            if t in seen:
                continue
            seen.add(t)
            out.append(t)
            if len(out) >= n:
                break
        return out

    def maybe_summarize(self, conv):
        cfg = self.cfg.get("summary") or {}
        if not cfg.get("enabled", True):
            return
        # 注意别写成 int(cfg.get("every", 50))：every=0 时 lines[-0:] 等于全文，
        # 会把整个归档塞进摘要 prompt
        every = int(cfg.get("every", 50) or 50)
        cnt = self.pending.get(conv, 0)
        if cnt < every:
            return

        prefix = "group" if conv.startswith("g") else "private"
        doc = os.path.join(self.archive_dir(), "%s_%s.md" % (prefix, conv[1:]))
        try:
            with open(doc, encoding="utf-8") as f:
                lines = [ln.rstrip() for ln in f if ln.startswith("[")]
        except Exception:
            return
        chunk = lines[-every:]
        if not chunk:
            return

        # 一次调用同时产出「事实摘要」和「关系档案」，省额度
        if conv.startswith("g"):
            head = "下面是群里的聊天记录（其中“我:”是我代替主人说的话）。\n"
            fact_prompt = (
                "## 事实\n"
                "不超过 8 行要点，每行以「- 」开头：只写群级事实——聊过什么、有什么约定、"
                "群内发生的事、共同话题、还没做完的事；"
                "群友的个人爱好、身份、计划一律不写（那些属于下面每个人自己的行）；"
                "不要编造，拿不准就不写。\n\n"
            )
            rel_prompt = (
                "## 关系\n"
                "分两类输出，每行以「- 【标记】」开头：\n"
                "1) 群里的每个人单独一行，标记写对方的 QQ 号（消息前面括号里那串数字），"
                "只写这个人：怎么称呼我、亲密度如何、有什么禁忌或明确不喜欢的事、"
                "以及**该怎么跟他说话**（能不能直接损他、要不要先接梗、他说正事时能不能贫）。"
                "例如：- 【对方的QQ号】爱撒娇，管主人叫屁屁，吃醋的时候要哄；可以损，越损越乐\n"
                "2) 群整体另起一行，标记写「群」：只写群级内容——群规、群梗、"
                "共同话题、群内事件、群体习惯、固定称呼/外号。"
                "例如：- 【群】梗群，气氛欢乐，主人有固定的外号\n"
                "注意：【群】行严禁写入任何群友的个人爱好、身份、计划——"
                "那些只属于上面每个人自己的行；看不出来就不写，不要编造。\n\n"
            )
        else:
            head = "下面是主人和对方的聊天记录（其中“我:”是我代替主人说的话）。\n"
            fact_prompt = (
                "## 事实\n"
                "不超过 8 行要点，每行以「- 」开头：只写事实——聊过什么、有什么约定、"
                "对方的偏好、还没做完的事；不要编造，拿不准就不写。\n\n"
            )
            rel_prompt = (
                "## 关系\n"
                "不超过 8 行要点，每行以「- 」开头：只写关系和相处方式——他怎么称呼我、"
                "我怎么称呼他、亲密度如何、有什么禁忌话题或他明确不喜欢的事。\n"
                "**其中必须有 1~2 行写「怎么跟他说话」，格式是「- 【相处】…」。"
                "要写成能照着做的动作，不能是形容词：**\n"
                "  ✓ 「- 【相处】能直接损他，他不会往心里去」\n"
                "  ✓ 「- 【相处】他抱怨的时候先接一句情绪，再给办法，别一上来讲道理」\n"
                "  ✓ 「- 【相处】聊工作时别贫，他要的是方案」\n"
                "  ✓ 「- 【相处】他问技术问题就答，别绕，他不爱听铺垫」\n"
                "  ✗ 「- 【相处】我们关系很好」（这是评价，照着做不了）\n"
                "  ✗ 「- 【相处】要友好地回应他」（废话，对谁都适用）\n"
                "  · 看不出他怎么相处就别硬写——但**如果这几轮里他明显表现出偏好**"
                "（爱开玩笑 / 只想听结论 / 不喜欢被追问），那是该写的。\n"
                "如果这几轮里真的发生过「一起经历的事」，单独写一行，格式是 "
                "「- 【经历】…」，最多一条。\n"
                "  · 写**具体的画面**，不写评价：\n"
                "    ✓ 「凌晨三点陪他改代码，他说眼睛都快闭上了」\n"
                "    ✓ 「他失恋那次，我陪他骂了半小时」\n"
                "    ✓ 「他第一次夸我『还行』」\n"
                "    ✗ 「他很累」「我们关系很好」「聊得很开心」（这是评价，不是经历）\n"
                "  · 没有值得记的事就别硬写这一行——大部分人日常聊天并没有「经历」。\n"
                "看不出来就不写，不要编造。\n\n"
                "## 关系图\n"
                "把这个人和他提到的人/物/事之间的关系，写成三联行，每行格式"
                "「- 主体 - 关系 - 客体」，最多 6 行：\n"
                "  - 老王 - 养了 - 豆豆\n"
                "  - 豆豆 - 是 - 猫\n"
                "  - 老王 - 讨厌 - 加班\n"
                "  - 豆豆 - 让老王 - 头疼\n"
                "要求：只写**具体的人名/物名/事情**；不要用「他」「我」「这个」"
                "这类人称代词当主体或客体；关系用两三个字的动词；"
                "看不出来就别写，宁缺勿滥。\n\n"
            )
        prompt = (
            head
            + "请分两部分输出，中间用单独一行「## 关系」隔开：\n\n"
            + fact_prompt
            + rel_prompt
            + "\n".join(chunk)
        )
        log("  正在生成长期记忆摘要（%s，%d 条）…" % (conv, len(chunk)))
        res = self.llm_fn(prompt)
        if not res:
            # 失败时必须把计数退回去：否则 writer 每 60 秒扫一次、每次都看见
            # 计数够阈值就重新入队，模型 429 期间会变成「每分钟重试一次」的
            # 永久重试风暴。退回去后要再攒够 every 条才会再试，等于天然退避
            self.pending[conv] = max(0, cnt - every)
            log("  摘要生成失败，要再攒 %d 条才重试" % every)
            return

        fact_lines, rel_lines, graph_lines, cur = [], [], [], "fact"
        for ln in res.splitlines():
            s = ln.strip()
            m = re.match(r"^#{1,6}\s*(\S+)", s)
            if m:
                head = m.group(1)
                if head.startswith("关系图") or head.startswith("关系图谱"):
                    cur = "graph"
                elif head.startswith("关系"):
                    cur = "rel"
                else:
                    cur = "fact"
                continue
            if s.startswith("- "):
                if cur == "fact":
                    fact_lines.append(s)
                elif cur == "rel":
                    rel_lines.append(s)
                else:
                    graph_lines.append(s)
        self._apply_rel(conv, rel_lines)      # 群里按人分流：个人档案 + 群级档案
        # 关系图：解析「- 主体 - 关系 - 客体」，存成边
        edges = []
        for s in graph_lines:
            body = s[2:].strip()
            parts = [p.strip() for p in re.split(r"\s*[-－—]\s*", body)]
            parts = [p for p in parts if p]
            if len(parts) == 3:
                edges.append(tuple(parts))
        if edges:
            n_new = self.graph_add(conv, edges)
            if n_new:
                log("  关系图 +%d 条边（共 %d 条）"
                    % (n_new, len(self._graph_load(conv))))
        pts = self._filter_facts(conv, fact_lines)
        if not pts:
            # 正常结果：本轮事实都没到两次命中，候选已进观察区
            log("  本轮事实无一达到两次命中，仅入观察区（%s 现有 %d 条候选）"
                % (conv, len(self.fact_pending.get(conv) or [])))
            self.pending[conv] = 0
            return
        all_pts = []
        try:
            with open(self._summary_path(conv), encoding="utf-8") as f:
                all_pts = [ln.rstrip() for ln in f if ln.startswith("- ")]
        except Exception:
            pass
        all_pts = (all_pts + pts)[-int(cfg.get("max_points", 12)):]
        try:
            with open(self._summary_path(conv), "w", encoding="utf-8") as f:
                f.write("# 长期记忆摘要 - %s\n\n> 自动生成，最多保留 %d 行要点。\n\n"
                        % (conv[1:], int(cfg.get("max_points", 12))))
                f.write("\n".join(all_pts) + "\n")
        except Exception as e:
            log("  摘要写入失败: %s" % e)
            return
        self.pending[conv] = 0
        self._cache_summary(conv)
        log("  长期记忆已更新：%d 条要点" % len(all_pts))

    def context_lines(self, conv, exclude_mid=None, excludes=None):
        d = self.recent.get(conv)
        if not d:
            return []
        items = list(d)
        if exclude_mid is not None:
            items = [x for x in items if x.get("mid") != exclude_mid]
        if excludes:
            ex = set(excludes)
            items = [x for x in items if x.get("mid") not in ex]
        out = []
        now = time.time()
        # 别漏了 `or 12`：slice[-0:] 等于全文，配置填 0 会把整个缓冲塞进 prompt
        for x in items[-int(self.cfg.get("context_messages", 12) or 12):]:
            tag = ""
            if x.get("kind"):
                tag = "（%s）" % x["kind"]
            elif x.get("at_me"):
                tag = "（@了你）"
            # 旧消息标出距今多久——不然模型会把「两天前说的」当成现在的话题
            mark = self.age_mark(x.get("ts"), now)
            out.append("%s: %s%s%s" % (x.get("nick"), x.get("text"), tag, mark))
        return out

    # ---------- 时间标记：让模型分得清「刚才」和「很久以前」 ----------
    @staticmethod
    def _parse_ts(s):
        """归档里的 '[2026-10-22 16:33:36]' → 时间戳。"""
        try:
            return time.mktime(time.strptime(str(s).strip(), "%Y-%m-%d %H:%M:%S"))
        except Exception:
            return None

    @staticmethod
    def _epoch(v):
        try:
            return float(v) if v else None
        except (TypeError, ValueError):
            return None

    def age_mark(self, ts, now=None):
        """超过阈值的旧消息加「（N 小时前）」这类标记。

        这是「机器人揪着很久以前的内容不放」的直接解药：上下文里混着
        两天前和两分钟前的消息时，光有先后顺序，模型判断不出哪个才是
        当下的话题，只能顺着最像「被问到」的那条旧话接下去。
        """
        ts = self._epoch(ts)
        if not ts:
            return ""
        now = now if now is not None else time.time()
        dt = now - ts
        if dt <= 0:
            return ""
        after = int((self.cfg.get("memory") or {}).get("mark_after_min", 15) or 15)
        if dt < after * 60:
            return ""
        if dt < 3600:
            return "（%d 分钟前）" % max(1, int(round(dt / 60.0)))
        if dt < 86400:
            return "（%d 小时前）" % int(dt // 3600)
        return "（%d 天前）" % int(dt // 86400)

    # ================= 两钩子（主程序唯一入口） =================
    @staticmethod
    def _clip_stage(text, limit=1200):
        """阶段回顾按段（## 开头）倒着截到上限，只留最近的段，砍掉的不留痕。"""
        if not text or len(text) <= limit:
            return text
        blocks = re.split(r"(?m)^(?=## )", text)
        kept = []
        total = 0
        for b in reversed(blocks):
            if total + len(b) > limit and kept:
                break
            kept.append(b)
            total += len(b)
        return "".join(reversed(kept)).strip()
    def recall(self, conv, uid=None, text="", mid=None, excludes=None):
        """收消息时调用：一次拿齐全部记忆，返回拼 prompt 用的 dict。

        键：history / summary / profile / user_profile / stages / style /
            samples / mood / context / related
        """
        r = {
            "history": self.history.setdefault(
                conv, deque(maxlen=int(self.cfg["context_size"]) * 2)),
            "summary": self.summary.get(conv) or "",
            "profile": self.profile.get(conv) or "",
            "user_profile": "",
            # 摘要/档案/风格都有上限，唯独阶段回顾默认「永不丢弃」无限累积；
            # 磁盘上照旧全留，只在注入时截断，别让 prompt 越长越大
            "stages": self._clip_stage(
                self.stages.get(conv) or "",
                int((self.cfg.get("memory") or {}).get("stage_max_chars", 1200)
                    or 1200)),
            "style": self.style,
            "samples": self.style_samples(conv),
            "mood": self.mood_line(conv),
            "context": self.context_lines(conv, mid, excludes),
            "related": [],
        }
        if uid is not None and conv.startswith("g"):
            r["user_profile"] = \
                self.user_profile.get("%s|u%s" % (conv, uid)) or ""
        try:
            r["related"] = self.related(conv, text or "")
        except Exception as e:
            log("相关记忆检索失败（不影响本次回复）: %s" % e)
        return r

    def remember(self, conv, uid, q, a):
        """回复发出后调用：重活扔进后台 writer 队列。

        recent 追加与归档写在发送流程里已同步完成（recall 供数不能等队列）；
        摘要/阶段/风格在这里入队——按 (kind, conv) 去重，失败或冷却错过
        由 writer 的周期性 sweep 兜底补触发。q/a 预留（将来做逐轮抽取）。
        """
        self._enqueue(("after_reply", conv))

    # ================= 后台 writer 线程 =================
    def _enqueue(self, key):
        with self._wq_lock:
            if key in self._wq_keys:
                return                      # 同会话同任务已在队列，合并
            self._wq_keys.add(key)
        self._wq.put(key)

    def _writer_loop(self):
        while True:
            try:
                key = self._wq.get(timeout=60)
            except queue.Empty:
                self._sweep()               # 没人发消息也把攒够的压掉
                continue
            try:
                kind, conv = key
                if kind == "after_reply":
                    self.maybe_summarize(conv)      # 事实摘要 + 关系档案
                    self.maybe_update_stage(conv)   # 中期记忆
                    self.maybe_update_style()       # 长期：说话风格
            except Exception as e:
                log("writer 任务失败(%s): %s" % (key, e))
            finally:
                with self._wq_lock:
                    self._wq_keys.discard(key)

    def _sweep(self):
        """兜底：攒够条数但没被触发的会话补进队列（失败重试/冷却错过）。"""
        cfg = self.cfg.get("summary") or {}
        if not cfg.get("enabled", True):
            return
        every = int(cfg.get("every", 50) or 0)
        for conv, cnt in list(self.pending.items()):
            if every > 0 and cnt >= every:
                self._enqueue(("after_reply", conv))

    # ================= 检索：相关旧记忆 =================
    def _build_search_index(self):
        """启动时给归档建倒排索引（每会话取最近 N 条，控制内存）。"""
        d = self.archive_dir()
        recs = []
        for pat, pre in (("private_*.md", "u"), ("group_*.md", "g")):
            for p in glob.glob(os.path.join(d, pat)):
                m = re.match(r"^(private|group)_(\d+)\.md$",
                             os.path.basename(p))
                if not m:
                    continue
                conv = pre + m.group(2)
                try:
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        lines = [ln.rstrip("\n") for ln in fh
                                 if ln.startswith("[")]
                except OSError:
                    continue
                for ln in lines[-SEARCH_INDEX_PER_CONV:]:
                    mm = ARCH_LINE_RE.match(ln)
                    if mm:
                        recs.append((conv, mm.group(1), mm.group(2),
                                     mm.group(3)))
        self._search.build(recs)
        log("检索索引已建立：%d 条 / %d 个会话"
            % (len(recs), len(set(r[0] for r in recs))))

    def _index_line(self, conv, line):
        """归档写入后增量进索引。"""
        try:
            mm = ARCH_LINE_RE.match(line)
            if mm:
                self._search.add(conv, mm.group(1), mm.group(2), mm.group(3))
        except Exception:
            pass

    def _ensure_vectors(self):
        """后台构建向量索引。依赖/模型缺失就安静降级（关键词检索照常）。"""
        if _mv is None or not _mv.deps_ok():
            return                          # 没装 onnxruntime/tokenizers/numpy
        try:
            emb = _mv.Embedder(self.archive_dir())
            vec = _mv.VectorStore(
                os.path.join(self.archive_dir(), "vectors.npy"))
            vec.load()
            self._search.build_vectors(emb, vec)
        except Exception as e:
            log("向量检索初始化失败，回退关键词检索: %s" % e)

    def time_records(self, conv, query, limit=40):
        """按模糊时间词定位那段时间的聊天记录，**带完整时间戳**。

        「我昨晚干了啥」这种问句，语义检索是找不到的（问句和内容对不上），
        但时间范围是确定的——把那个时间段的记录连时间戳一起给模型，
        让它自己对上「昨晚」说的是哪一段，比我们猜「最近几段」准得多。

        返回 (有没有识别出时间词, 记录行列表)。
        """
        rng = parse_time_range(query)
        if not rng:
            return False, []
        start, end, label = rng
        rows = []
        with self._search.lock:
            recs = list(self._search.recs)
        for c, ts, nick, body in recs:
            if c != conv:
                continue
            t = self._parse_ts(ts)
            if t is None or t < start or t > end:
                continue
            rows.append((t, ts, nick, body))
        rows.sort(key=lambda x: x[0])
        if len(rows) > limit:
            # 均匀采样、首尾必含：那段时间可能很长，只取最近的一段
            # 会把开头（「你睡了吗」这种开场）漏掉
            if limit <= 1:
                rows = rows[-1:]
            else:
                step = (len(rows) - 1) / float(limit - 1)
                rows = [rows[int(round(j * step))] for j in range(limit)]
        return True, ["[%s] %s: %s" % (ts, nick, body)
                      for _t, ts, nick, body in rows]

    def related(self, conv, text, k=SEARCH_TOPK):
        """搜本条会话的归档，返回可直接注入 prompt 的行（无命中返回 []）。

        按正文去重：归档里同一句话可能出现多次（对方重复说过、或者你回了两遍），
        检索会分别命中——原样拼进 prompt 就是同一段内容出现好几遍，白占地方。

        短句直接不检索（SEARCH_MIN_QUERY_CHARS）。实测噪音全部来自极短句：
            「哈哈」→ 召回「嘿嘿」（相似度 0.712，比正经查询还高）
            「今天吃啥呢」→ 召回「我昨晚干了啥」（0.612）
            「在吗」→ 召回「在等你呢」（0.672）
        反过来真需要旧记忆的查询都是长句：
            「那个代码bug修好了没」「你还记得我家那只猫吗」「我昨天跟你说的事」
        所以卡长度比调相似度阈值靠谱——噪音不一定低分，但一定是短文。
        """
        t = (text or "").strip()
        if len(t) < SEARCH_MIN_QUERY_CHARS:
            return []
        # 向量没建好时不要退到关键词匹配：口语查询（"那个""我家那只猫"）字面
        # 重叠太少，实测 6 个查询只召回 1 个还全是噪音（"我昨天跟你说的事"
        # 召回"3207399168？这人我真没印象"）。宁可不注入，也别塞垃圾。
        # 放在这里而不是调用处：related() 有三个调用方（正常回复 / 直连 API /
        # 主动搭话），挂在调用处就得写三遍，漏一处就是一个漏洞。
        if not self._search.vec_ready():
            return []
        if not t:
            return []
        qtoks = tokenize(t)
        hits = self._search.search(conv, t, k)
        out, seen = [], set()
        for _sc, _m, i in hits:
            _c, ts, nick, body = self._search.recs[i]
            if body.strip() == t:
                continue                      # 当前消息自己，跳过
            key = re.sub(r"\s+", "", body)[:40]
            if key in seen:
                continue
            seen.add(key)
            out.append("[%s] %s: %s"
                       % (ts[:10], nick, preview_around(body, qtoks)))
        return out

    # ================= 事实过滤：两次命中才提升 =================
    @staticmethod
    def _norm_fact(s):
        return re.sub(
            r"[\s，。！？、~…,.!?;:：；'\"「」『』（）()\[\]【】\-—_*#>`|]+",
            "", s).lower()

    def _filter_facts(self, conv, facts):
        """候选事实两次命中才提升为正式记忆。

        首次见到 → 进观察区（按会话，上限 FACT_PENDING_MAX，重启丢无所谓，
        它只是过滤器不是数据）；与观察区某条归一化相等或二元组相似度
        ≥FACT_SIM → 提升，并记入已提升名单（防重复写入摘要）；
        短于 FACT_MIN_LEN 字的不够格（"好的""在吗"）。
        返回提升列表（写进摘要的）。
        """
        pend = self.fact_pending.setdefault(conv, [])
        done = self.fact_done.setdefault(conv, [])
        promoted = []
        for f in facts:
            n = self._norm_fact(f)
            if len(n) < FACT_MIN_LEN:
                continue
            if any(n == d or dice(n, d) >= FACT_SIM for d in done):
                continue                      # 档案里已有，跳过
            idx = None
            for i, p in enumerate(pend):
                if n == p or dice(n, p) >= FACT_SIM:
                    idx = i
                    break
            if idx is not None:
                pend.pop(idx)
                done.append(n)
                if len(done) > FACT_DONE_MAX:
                    del done[:len(done) - FACT_DONE_MAX]
                promoted.append(f)
            else:
                pend.append(n)
        if len(pend) > FACT_PENDING_MAX:
            del pend[:len(pend) - FACT_PENDING_MAX]
        return promoted

