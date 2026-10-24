#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/blind_test.py —— 盲测：机器人的回复混进真人聊天里，还认得出来吗？

为什么需要这个
==============
「去 AI 味」最容易变成凭感觉。改完提示词自己读一遍觉得"好多了"，
但放到真实对话里还是能被一眼看出——因为判断的人知道哪条是 AI 写的。

所以要盲测：把真人消息和机器人回复混在一起、打乱顺序、隐去来源，
再让另一个人（或者另一个模型）逐条判断"这条像真人说的还是 AI 说的"。

指标：**识别率低于 60% 才算过关**。真人自己互判也有误伤（人和人说话
本来就不一样），所以不是要求 0%，而是「跟瞎猜（50%）拉不开差距」。

怎么用
======
    python tools/blind_test.py                 # 抽 50 真 + 50 机器，生成题目
    python tools/blind_test.py --n 30          # 各抽 30 条
    python tools/blind_test.py --score ans.txt # 给你的答案打分

第一次运行会生成 盲测_题目.txt（只有编号和内容，不含来源）和
盲测_答案.txt（真实的来源，打分时才用——**别提前打开**）。

答题：在 盲测_题目.txt 里每条后面写 真人 / AI，存成 答案.txt。
打分：python tools/blind_test.py --score 答案.txt

机器人回复从哪来？
==================
从 qq_autoreply.log 里抓「私聊回复 / 群回复」的正文（去掉（1/3）这种分条
标记）。真人消息从 all/群聊_*.json 里抓。

两边都会做同样的清洗和长度过滤，只比「说话方式」，不比长短、不比话题。
"""

import argparse
import glob
import json
import os
import random
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(BASE, "qq_autoreply.log")


def bot_replies():
    """从日志里抓机器人发出去的正文。"""
    out = []
    if not os.path.exists(LOG):
        return out
    for ln in open(LOG, encoding="utf-8", errors="replace"):
        m = re.search(r"\] (?:私聊|群)回复 .*? -> (.*?) \| (?:ok|split)", ln)
        if not m:
            continue
        t = m.group(1).strip()
        if not t or t in ("（图片）", "（语音）", "（文件）"):
            continue
        # 去掉分条尾巴「（2/5）」
        t = re.sub(r"（\d+/\d+）\s*$", "", t).strip()
        # 去掉表情包标记
        t = re.sub(r"\[img:[^\]]*\]", "", t).strip()
        if t:
            out.append(t)
    return out


def human_msgs():
    """从真实群聊记录里抓真人消息（同一套过滤）。"""
    out = []
    for f in glob.glob(os.path.join(BASE, "all", "群聊_*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for m in (d.get("messages") or []):
            c = (m.get("content") or "").strip()
            if not c:
                continue
            # 系统/媒体占位、@ 和引用带的信息量不是「说话方式」，剔掉
            if any(x in c for x in ("[表情]", "[图片]", "[视频]", "[语音]",
                                    "[文件]", "[合并转发", "[其他消息]",
                                    "[回复]", "@", "http")):
                continue
            if re.search(r"\d{5,}", c):        # 手机号 / QQ 号
                continue
            out.append(c)
    return out


def normalize(pool, lo=2, hi=28):
    """两边用同一套标准筛：长度区间、去掉纯符号。"""
    res = []
    for t in pool:
        t = re.sub(r"\s+", " ", str(t)).strip()
        if not (lo <= len(t) <= hi):
            continue
        if not re.search(r"[\u4e00-\u9fff a-zA-Z]", t):   # 至少有个字
            continue
        res.append(t)
    return res


def make(nb, nh, seed=None):
    bots = normalize(bot_replies())
    human = normalize(human_msgs())
    if not bots:
        print("日志里没抓到机器人回复（qq_autoreply.log 为空或格式不符）")
        return None
    if not human:
        print("all/群聊_*.json 里没抓到真人消息")
        return None
    if seed is not None:
        random.seed(seed)
    b = random.sample(bots, min(nb, len(bots)))
    h = random.sample(human, min(nh, len(human)))
    rows = [(t, "AI") for t in b] + [(t, "真人") for t in h]
    random.shuffle(rows)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50, help="各抽多少条（默认 50）")
    ap.add_argument("--seed", type=int, default=None, help="固定随机种子，便于复现")
    ap.add_argument("--score", metavar="答案文件", help="给答案打分")
    ap.add_argument("--out", default="盲测", help="输出文件前缀")
    args = ap.parse_args()

    if args.score:
        score(args.score, args.out)
        return

    rows = make(args.n, args.n, args.seed)
    if not rows:
        sys.exit(1)

    qpath = os.path.join(BASE, args.out + "_题目.txt")
    apath = os.path.join(BASE, args.out + "_答案.txt")
    with open(qpath, "w", encoding="utf-8") as f:
        f.write("# 盲测：%d 条，混了真人和 AI 的回复。\n" % len(rows))
        f.write("# 逐条在「判断：」后面写 真人 或 AI，然后：\n")
        f.write("#   python tools/blind_test.py --score %s\n" % os.path.basename(qpath))
        f.write("# 真实答案在 %s —— 打完分之前别看。\n\n"
                % os.path.basename(apath))
        for i, (t, _) in enumerate(rows, 1):
            f.write("%02d. %s\n    判断：\n\n" % (i, t))
    with open(apath, "w", encoding="utf-8") as f:
        for i, (t, src) in enumerate(rows, 1):
            f.write("%02d\t%s\n" % (i, src))

    print("题目：%s（%d 条）" % (qpath, len(rows)))
    print("答案：%s（打分用，别提前看）" % apath)
    print("\n填完判断后：python tools/blind_test.py --score %s" % args.out + "_题目.txt")


def score(ans_path, prefix):
    """ans_path 是用户填好的题目文件；答案是 <prefix>_答案.txt。"""
    tpath = ans_path if os.path.isabs(ans_path) else os.path.join(BASE, ans_path)
    apath = os.path.join(BASE, prefix + "_答案.txt")
    if not os.path.exists(tpath):
        print("找不到答案文件：%s" % tpath); sys.exit(1)
    truth = {}
    for ln in open(apath, encoding="utf-8"):
        p = ln.rstrip("\n").split("\t")
        if len(p) == 2:
            truth[p[0]] = p[1]
    got = {}
    cur = None
    for ln in open(tpath, encoding="utf-8"):
        m = re.match(r"^(\d+)\.\s", ln)
        if m:
            cur = m.group(1).zfill(2); continue
        m = re.search(r"判断：\s*(真人|AI|ai)", ln)
        if m and cur:
            got[cur] = "AI" if m.group(1).lower() == "ai" else "真人"
    judged = [k for k in truth if k in got]
    if not judged:
        print("没读到任何「判断：」行。格式：判断：真人  或  判断：AI"); sys.exit(1)
    right = sum(1 for k in judged if got[k] == truth[k])
    rate = right * 100.0 / len(judged)
    print("=" * 52)
    print("判了 %d 条，猜对 %d 条" % (len(judged), right))
    print("识别率：%.1f%%   （瞎猜是 50%%）" % rate)
    if rate < 60:
        print("✓ 过关：跟瞎猜拉不开差距，说明 AI 味已经不明显了")
    elif rate < 75:
        print("△ 还能认出来：再收一收长度和套话")
    else:
        print("✗ 太明显：对方一眼一个准，回去看 prompt 的长度约束")
    # 分类看：它是把 AI 认成真人（漏判），还是把真人认成 AI（严判）
    fake_human = sum(1 for k in judged if truth[k] == "AI" and got[k] == "真人")
    fake_ai = sum(1 for k in judged if truth[k] == "真人" and got[k] == "AI")
    print("\n漏判（AI 被当成真人）：%d 条" % fake_human)
    print("严判（真人被当成 AI）：%d 条" % fake_ai)
    if fake_ai > fake_human * 2:
        print("→ 严判偏多：机器人可能太短太冷，缺真人那种随口的热乎劲")
    elif fake_human > fake_ai * 2:
        print("→ 漏判偏多：机器人还是太长/太完整，套话没清干净")


if __name__ == "__main__":
    main()
