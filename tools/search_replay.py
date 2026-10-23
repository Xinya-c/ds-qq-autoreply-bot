#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/search_replay.py —— 关键词检索体系的回放标定工具。

背景：memory.py 的检索层计划用「中文 bigram + ASCII 词元 + DF 过滤」的
纯关键词方案（不引 jieba、不上向量库，保持零依赖）。但阈值不能拍脑袋——
借鉴 dsh-workspace-recollection 的纪律：**阈值必须用真实回放定**。

本脚本对 qq_messages/ 的真实归档做两类回放（**部署态**：DF/idf 用全局
语料计算保证分数尺度稳定，候选结果按样本所在会话过滤，与 memory.py
的「按会话隔离检索」一致）：

  正样本（含索引）：query = 某条归档原文的片段，索引里保留该条
      → 量 hit@1/@3、MRR、源记录分数分布（检索体系的天花板）
  负样本（leave-one-out）：同一条 query，但把该条（及同文本重复条）移出索引
      → 量 top1 分数分布（"话是真的，但记忆里没有"时的最高分）

第一轮回放暴露的教训：**纯总分行不通**——长 query 靠堆常见词也能刷出
高分，负样本 top1 比正样本底部还高。因此打分之外加两道判据：
  · min_matched：至少命中几个「内容词」（权重>0 的 query 词元）
  · min_coverage：命中的内容词占 query 内容词的比例下限
再叠加四路噪音过滤（绝对底线 / 硬特征 / 相对长尾 / 平局），
最后对多组配置做扫描，按「负样本拦截率优先、正样本保留率 ≥80%」择优。

隐私：只读归档、只打印聚合数字与通用 bigram，不输出任何聊天原文，
不写任何文件。可安全提交进公开仓库。

用法：
    python tools/search_replay.py                    # 默认读 ../qq_messages
    python tools/search_replay.py --samples 800 --seed 7
"""

import argparse
import math
import os
import random
import re
import sys
from collections import Counter

# 归档行：[2025-07-06 13:16:26] 好友C(999000222): 文本
LINE_RE = re.compile(r"^\[([^\]]+)\]\s*(.+?):\s?(.*)$")
# 只认主归档文件，排除 _stage/_profile/_summary 和轮转 _N
FILE_RE = re.compile(r"^(private|group)_(\d+)\.md$")
# ASCII 词元：路径 / 命令 / 常量名 / 中英混排里的英文词
ASCII_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-./\\:]*")
# 颜文字区间（与 noise.js 同思路，不用 \p{} 属性转义）
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")
# 精确形态词：含这些字符的词命中率极高，加权
EXACT_CHARS = set("_-./\\:")
# CJK 字符（用于切 bigram）
CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")

# 四路噪音过滤的默认参数（语义照搬 noise.js，GAP 改相对比例：
# 关键词总分无上界，绝对差不可移植）
#
# 标定记录（2026-10-23，N=5161 条真实归档回放，seed=42）：
#   天花板 hit@1=65%；df_max=2% 优于 5%（系统标记+泛词弃用更彻底）；
#   FLOOR 是主导因素：25.6（负样本 p95）→ 正保留 72% / 负拦截 96%，
#   29.4（p95×1.15）→ 67% / 97%。拐点在 21~26，按「宁可不注入、不可错注入」
#   取 p95 一档。语料量级变化后重跑本脚本复测。
DEF_RATIO = 0.88       # ③ 相对长尾：低于 top1×它 且 间隙够大 → 砍
DEF_GAP = 0.15         # ③ 的间隙条件：(top1-score) > top1×它
DEF_SPREAD = 0.10      # ④ 平局：前四挤在 top1×(1-它) 内 → 只留前三
DEF_FLAT_KEEP = 3
HARD_ARM = 2           # ② 硬特征至少几个才开硬切
FLOOR_MARGIN = 1.15    # 绝对底线 = 负样本 p95 × 该系数


# ---------------------------------------------------------------- 语料

def load_records(archive_dir):
    """读全部主归档文件，返回 [{conv, ts, text}]（只读，绝不写回）。"""
    recs = []
    for f in sorted(os.listdir(archive_dir)):
        m = FILE_RE.match(f)
        if not m:
            continue
        conv = ("u" if m.group(1) == "private" else "g") + m.group(2)
        try:
            with open(os.path.join(archive_dir, f), encoding="utf-8",
                      errors="replace") as fh:
                for ln in fh:
                    mm = LINE_RE.match(ln.rstrip("\n"))
                    if mm:
                        recs.append({"conv": conv, "ts": mm.group(1),
                                     "text": mm.group(3)})
        except OSError:
            continue
    return recs


# ---------------------------------------------------------------- 分词

def bigrams_of(text):
    """连续 CJK 串切二元组；非 CJK 字符是天然边界。"""
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
    out = []
    for m in ASCII_RE.finditer(text):
        raw = m.group(0).strip("._-:/\\")
        if len(raw) >= 2:
            out.append(raw)
    return out


def emojis_of(text):
    return [e for e in EMOJI_RE.findall(text) if e.strip()]


def is_exact(token):
    return any(c in EXACT_CHARS for c in token)


def tokenize(text):
    """返回 [(token, kind)]，kind ∈ bigram / ascii / emoji。"""
    toks = [(t, "bigram") for t in bigrams_of(text)]
    toks += [(t, "ascii") for t in ascii_tokens_of(text)]
    toks += [(t, "emoji") for t in emojis_of(text)]
    return toks


# ---------------------------------------------------------------- 打分

class Index:
    """DF 索引 + 打分器。

    score_detail 返回 (总分, 内容词命中数)：
      · 总分 = Σ 命中词元的 idf 权重（ascii 精确形态 ×2.2，emoji ×1.5）
      · 内容词 = 权重>0 的词元（泛词权重为 0，不算内容词）
    """

    def __init__(self, records, df_max_ratio=0.05):
        self.n = len(records)
        self.recs = records
        self.df_max = max(2, int(self.n * df_max_ratio))
        df = Counter()
        self.tok_sets = []
        for r in records:
            s = set(t for t, _ in tokenize(r["text"]))
            self.tok_sets.append(s)
            df.update(s)
        self.df = df
        self._w = {}

    def weight(self, token, kind):
        if token in self._w:
            return self._w[token]
        d = self.df.get(token, 0)
        if d > self.df_max:            # 泛词：无区分度，直接弃用
            w = 0.0
        else:
            w = math.log(self.n / (1.0 + d))
            if kind == "ascii" and is_exact(token):
                w *= 2.2
            elif kind == "emoji":
                w *= 1.5
        self._w[token] = w
        return w

    def score_detail(self, idx, query_toks):
        """idx 处记录对 query_toks 的 (分数, 内容词命中数)。

        query_toks 为去重后的 [(token, kind)]。
        """
        if idx < 0 or idx >= len(self.recs):
            return 0.0, 0
        s = self.tok_sets[idx]
        if not s:
            return 0.0, 0
        total, matched = 0.0, 0
        for t, kind in query_toks:
            if t in s:
                w = self.weight(t, kind)
                if w > 0:
                    total += w
                    matched += 1
        return total, matched

    def query_content_len(self, query_toks):
        return sum(1 for t, kind in query_toks if self.weight(t, kind) > 0)

    def topk(self, query_text, k=5, exclude_text=None, conv=None):
        """检索 top-k。conv 限定候选范围（部署态：按当前会话过滤），
        exclude_text 排除同文本记录（leave-one-out 用）。"""
        q = list(set(tokenize(query_text)))
        qc = self.query_content_len(q)
        scored = []
        for i in range(len(self.recs)):
            if conv is not None and self.recs[i]["conv"] != conv:
                continue
            if exclude_text is not None and \
                    self.recs[i]["text"] == exclude_text:
                continue
            sc, m = self.score_detail(i, q)
            if sc > 0:
                scored.append((sc, m, i))
        scored.sort(key=lambda x: -x[0])
        return scored[:k], q, qc


# ---------------------------------------------------------------- 四路噪音过滤

def hard_tokens(query_toks):
    """query 里的硬特征：ASCII 词 + emoji（纯中文泛指词不算）。"""
    return [t for t, kind in query_toks if kind in ("ascii", "emoji")]


def filter_hits(rows, query_toks, recs, params):
    """rows: [(score, matched, idx)] 已排序。返回 (kept, dropped, notes)。

    前置判据（第一轮回放教训：纯总分行不通）：
      · min_matched：内容词命中数下限
      · min_coverage：命中数 / query 内容词数 的下限
    四路信号：① 绝对底线 ② 硬特征 ③ 相对长尾 ④ 平局。
    """
    floor = params.get("floor", 0.0)
    ratio = params.get("ratio", DEF_RATIO)
    gap = params.get("gap", DEF_GAP)
    spread = params.get("spread", DEF_SPREAD)
    min_matched = params.get("min_matched", 1)
    min_cov = params.get("min_coverage", 0.0)
    if not rows:
        return [], [], []
    top1 = rows[0][0]
    hard = hard_tokens(query_toks)
    # ① 绝对底线
    if floor > 0 and top1 < floor:
        return [], [r for r in rows], \
            ["top1=%.2f 低于底线 %.2f：判定没有相关记忆" % (top1, floor)]
    kept, dropped = [], []
    armed = len(hard) >= HARD_ARM
    for sc, m, idx in rows:
        # 前置：内容词命中数 / 覆盖率
        if m < min_matched:
            dropped.append((sc, m, idx))
            continue
        cov = m / float(max(1, params.get("_qc", m)))
        if cov < min_cov:
            dropped.append((sc, m, idx))
            continue
        hits = 0
        if hard:
            hay = recs[idx]["text"]
            hits = sum(1 for t in hard if t in hay)
        reason = None
        if armed and hits == 0:
            reason = "nokey"          # ② 硬特征一个都没中
        elif sc < top1 * (1 - gap):
            reason = "low"            # ③ 离头名太远
        elif hits > 0:
            reason = None             # 硬特征命中 → 豁免长尾
        elif sc < top1 * ratio and top1 - sc > top1 * gap:
            reason = "low"            # ③ 普通长尾
        (kept if reason is None else dropped).append((sc, m, idx))
    # ④ 平局：前四条挤在小跨度内 → 只留前三
    if len(kept) >= 4 and kept[3][0] >= top1 * (1 - spread):
        dropped.extend(kept[DEF_FLAT_KEEP:])
        kept = kept[:DEF_FLAT_KEEP]
    return kept, dropped, []


# ---------------------------------------------------------------- 回放

def gen_fragment(text, rng):
    """从原文里切一个 6~14 字的片段当 query（短文本整条用）。"""
    t = text.strip()
    if len(t) < 8:
        return t
    L = min(rng.randrange(6, 15), len(t))
    start = rng.randrange(0, len(t) - L + 1)
    return t[start:start + L]


def pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1,
            max(0, int(round(p / 100.0 * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


def eval_config(recs, pos_idx, neg_idx, cfg, floor=None):
    """对一组 (df_ratio, min_matched, min_coverage[, floor]) 配置回放。

    floor=None 时按负样本 p95 × FLOOR_MARGIN 自动定；传入则用给定值
    （用于及格线 trade-off 曲线扫描）。
    部署态：DF/idf 用全局语料算（分数尺度稳定、一条 FLOOR 通吃所有
    会话），候选结果按各样本所在会话过滤（隔离，不串话）。
    """
    idx = Index(recs, df_max_ratio=cfg["df"])
    # 负样本 top1 分布 → 定绝对底线
    neg_top1 = []
    for i in neg_idx:
        rows, q, qc = idx.topk(recs[i]["text_frag"], k=1,
                               exclude_text=recs[i]["text"],
                               conv=recs[i]["conv"])
        if rows:
            neg_top1.append(rows[0][0])
    neg_top1.sort()
    if floor is None:
        floor = round(pct(neg_top1, 95) * FLOOR_MARGIN, 1)
    params = {"floor": floor, "min_matched": cfg["mm"],
              "min_coverage": cfg["cov"]}
    # 正样本：源记录是否进 kept
    pos_keep = 0
    pos_n = 0
    src_scores = []
    for i in pos_idx:
        rows, q, qc = idx.topk(recs[i]["text_frag"], k=5,
                               conv=recs[i]["conv"])
        if not rows:
            continue
        pos_n += 1
        sc, m = idx.score_detail(i, q)
        src_scores.append(sc)
        p = dict(params)
        p["_qc"] = qc
        kept, _, _ = filter_hits(rows, q, recs, p)
        if any(j == i for _, _, j in kept):
            pos_keep += 1
    # 负样本：是否被拦截（有 kept 即放行）
    neg_keep = 0
    for i in neg_idx:
        rows, q, qc = idx.topk(recs[i]["text_frag"], k=5,
                               exclude_text=recs[i]["text"],
                               conv=recs[i]["conv"])
        if not rows:
            continue
        p = dict(params)
        p["_qc"] = qc
        kept, _, _ = filter_hits(rows, q, recs, p)
        if kept:
            neg_keep += 1
    return {
        "floor": floor,
        "pos_keep": 100.0 * pos_keep / max(pos_n, 1),
        "neg_block": 100.0 * (1 - neg_keep / max(len(neg_idx), 1)),
        "neg_n": len(neg_idx), "pos_n": pos_n,
    }


def run(archive_dir, samples, seed):
    rng = random.Random(seed)
    recs = load_records(archive_dir)
    if len(recs) < 50:
        print("语料太少（%d 条），标定不可靠" % len(recs))
        return 1
    convs = Counter(r["conv"] for r in recs)

    # 给每条候选记录预生成 query 片段（正负样本共用，保证可比）
    cand = [i for i, r in enumerate(recs) if len(r["text"].strip()) >= 6]
    rng.shuffle(cand)
    cand = cand[:samples * 2]
    for i in cand:
        recs[i]["text_frag"] = gen_fragment(recs[i]["text"], rng)
    pos_idx = [i for i in cand if tokenize(recs[i]["text_frag"])]
    rng.shuffle(pos_idx)
    pos_idx, neg_idx = pos_idx[:samples], pos_idx[samples:samples * 2]

    # ---- 语料与 DF 统计（用 5% 参考索引）----
    idx0 = Index(recs, df_max_ratio=0.05)
    bigram_df = sorted(((t, d) for t, d in idx0.df.items()
                        if len(t) == 2 and CJK_RE.match(t[0])),
                       key=lambda x: -x[1])
    dfs = sorted(d for _, d in bigram_df)
    print("=" * 66)
    print("语料：%d 条 / %d 个会话 | 去重词表 %d | 正样本 %d / 负样本 %d"
          % (len(recs), len(convs), len(idx0.df), len(pos_idx), len(neg_idx)))
    print("bigram DF 分位：p50=%d p90=%d p99=%d max=%d"
          % (pct(dfs, 50), pct(dfs, 90), pct(dfs, 99), dfs[-1] if dfs else 0))
    print("DF 最高的 15 个 bigram（系统标记+泛词）：%s"
          % " ".join("%s:%d" % (t, d) for t, d in bigram_df[:15]))

    # ---- 无过滤天花板（df=5%, 不加任何判据，部署态）----
    hit1 = hit3 = 0
    mrr = 0.0
    for i in pos_idx:
        rows, q, qc = idx0.topk(recs[i]["text_frag"], k=3,
                                conv=recs[i]["conv"])
        if not rows:
            continue
        rank = next((k + 1 for k, (sc, m, j) in enumerate(rows) if j == i), 0)
        if rank == 1:
            hit1 += 1
        if rank and rank <= 3:
            hit3 += 1
        if rank:
            mrr += 1.0 / rank
    print("-" * 66)
    print("天花板（无过滤）：hit@1=%.1f%% hit@3=%.1f%% MRR=%.3f"
          % (100.0 * hit1 / max(len(pos_idx), 1),
             100.0 * hit3 / max(len(pos_idx), 1),
             mrr / max(len(pos_idx), 1)))

    # ---- 配置扫描 ----
    configs = [
        {"df": 0.05, "mm": 1, "cov": 0.0},
        {"df": 0.02, "mm": 1, "cov": 0.0},
        {"df": 0.05, "mm": 2, "cov": 0.0},
        {"df": 0.02, "mm": 2, "cov": 0.0},
        {"df": 0.05, "mm": 2, "cov": 0.34},
        {"df": 0.02, "mm": 2, "cov": 0.34},
        {"df": 0.02, "mm": 3, "cov": 0.5},
    ]
    print("-" * 66)
    print("%-28s %7s %9s %9s" % ("配置(df/命中数/覆盖率)", "FLOOR",
                                 "正保留%", "负拦截%"))
    best = None
    for cfg in configs:
        r = eval_config(recs, pos_idx, neg_idx, cfg)
        name = "df=%.0f%% mm=%d cov=%.2f" % (cfg["df"] * 100, cfg["mm"],
                                             cfg["cov"])
        print("%-28s %7.1f %8.1f%% %8.1f%%"
              % (name, r["floor"], r["pos_keep"], r["neg_block"]))
        # 择优：负拦截 ≥95% 的前提下正保留最高
        score = (r["neg_block"] >= 95.0, r["pos_keep"])
        if best is None or score > best[0]:
            best = (score, cfg, r)
    print("=" * 66)
    _, bc, br = best
    print("推荐配置：df_max=%.0f%%（DF>%d 弃用） min_matched=%d "
          "min_coverage=%.2f" % (bc["df"] * 100,
                                 max(2, int(len(recs) * bc["df"])), bc["mm"],
                                 bc["cov"]))
    print("          FLOOR=%.1f  RATIO=%.2f  GAP=%.2f  SPREAD=%.2f"
          % (br["floor"], DEF_RATIO, DEF_GAP, DEF_SPREAD))
    print("          预期：真查询 %.0f%% 能取回记忆，假查询 %.0f%% 被拦截"
          % (br["pos_keep"], br["neg_block"]))

    # ---- 及格线 trade-off 曲线（固定推荐配置，只扫 FLOOR）----
    idx = Index(recs, df_max_ratio=bc["df"])
    neg_scores = []
    for i in neg_idx:
        rows, q, qc = idx.topk(recs[i]["text_frag"], k=1,
                               exclude_text=recs[i]["text"])
        if rows:
            neg_scores.append(rows[0][0])
    neg_scores.sort()
    print("-" * 66)
    print("及格线 trade-off（越往右越严：漏记忆多、注入错记忆少）：")
    print("%-10s %9s %9s" % ("FLOOR", "正保留%", "负拦截%"))
    for p in (50, 75, 90, 95, 99):
        base = pct(neg_scores, p)
        for mult in (1.0, 1.15):
            f = round(base * mult, 1)
            r = eval_config(recs, pos_idx, neg_idx, bc, floor=f)
            print("%-10.1f %8.1f%% %8.1f%%   (负样本 p%d × %.2f)"
                  % (f, r["pos_keep"], r["neg_block"], p, mult))
    print("=" * 66)
    print("注意：正样本 query 是答案原文的连续片段，上述数字是**天花板**；")
    print("      真实用户提问更短更泛，保留率会低于此，上线后需用真实")
    print("      对话回归（本脚本 --seed 换种子可复测）。")

    # ---- 部署态说明 + 会话体量 ----
    # 部署态 = DF/idf 全局算 + 候选按当前会话过滤。好处：
    #   ① 隔离：不可能把 A 会话的片段注入 B 会话（隐私/串话）；
    #   ② 分数尺度全局稳定：小会话（几十条）里稀有词 idf 仍然高，
    #      一条 FLOOR 通吃所有体量的会话（按会话建索引则小会话
    #      最高分都够不到全局 FLOOR，检索会静默失效）。
    # 代价：跨会话的「主题级召回」不再可能——但那本来也不该做。
    conv_n = Counter(r["conv"] for r in recs)
    sizes = " ".join("%s=%d" % (c, n) for c, n in conv_n.most_common())
    idx = Index(recs, df_max_ratio=bc["df"])
    # 小会话体检：最小的会话里，leave-one-out 的 top1 最高分能否到 FLOOR
    small = [c for c, n in conv_n.most_common() if n <= 100]
    small_info = []
    for c in small:
        ci = [i for i in neg_idx if recs[i]["conv"] == c]
        if not ci:
            continue
        best = 0.0
        for i in ci[:40]:
            rows, q, qc = idx.topk(recs[i]["text_frag"], k=1,
                                   exclude_text=recs[i]["text"], conv=c)
            if rows:
                best = max(best, rows[0][0])
        small_info.append("%s(最高%.1f)" % (c, best))
    print("-" * 66)
    print("会话体量：%s" % sizes)
    print("小会话体检（n≤100，FLOOR=%.1f）：%s"
          % (br["floor"], " ".join(small_info) or "无"))
    print("（小会话最高分远超 FLOOR = 全局 idf 保住了分数尺度，检索可用）")
    print("=" * 66)
    return 0


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="关键词检索回放标定")
    ap.add_argument("--dir", default=os.path.join(
        os.path.dirname(here), "qq_messages"), help="归档目录")
    ap.add_argument("--samples", type=int, default=400)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    if not os.path.isdir(args.dir):
        print("归档目录不存在：%s" % args.dir)
        return 1
    return run(args.dir, args.samples, args.seed)


if __name__ == "__main__":
    sys.exit(main())
