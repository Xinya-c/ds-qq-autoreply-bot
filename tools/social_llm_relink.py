#!/usr/bin/env python
"""离线用模型判「这句在回哪句」，结果写进 qq_messages/_social_links.json。

为什么离线：一次模型调用要十几秒。而社交图是在**生成回复时**被调的（认人），
放进那条路径就是把聊天卡住。所以拆开——能力在这里，社交图只读结果。

规则版（社交图自带的多信号打分）已经能吃下大部分情况，这个工具只补最难的那类：
**多人同时聊、没有 @、话题还相近**的窗口。

    python tools/social_llm_relink.py --group 663611925 --limit 4 --dry-run
    python tools/social_llm_relink.py --group 663611925 --limit 4     # 落盘
    python tools/social_llm_relink.py --stats                          # 只看现状

写完不用重启：social_map 的缓存键带了覆盖文件的时间戳，下次认人自动生效。
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qq_autoreply as qa  # noqa: E402
import memory as M  # noqa: E402

WIN = 16          # 每个窗口多少条消息（太短看不出上下文，太长模型会乱）
MAX_MSGS = 400    # 一次最多覆盖多少条（防手滑烧额度）


def build_prompt(rows):
    """把窗口拼成编号列表，要求只输出 JSON 连线。"""
    lines = [
        "下面是一个 QQ 群里的连续消息，按时间从早到晚编号。",
        "请判断**每条消息在回复上面哪一条**。是在开新话题、或者没人可回，就标 -1。",
        "只输出 JSON 数组，形如 [[2,0],[3,2]]，含义是「2 号在回 0 号」。",
        "不要解释，不要代码块。",
        "",
    ]
    for k, r in enumerate(rows):
        lines.append("%d %s: %s" % (k, r["uid"], r["text"][:80]))
    return "\n".join(lines)


def parse_links(text, n):
    """从模型输出里抠出 [[i, j], ...]。宁缺毋滥：格式不对就返回空。"""
    if not text:
        return []
    m = re.search(r"\[\[.*?\]\]", text, re.S)
    if not m:
        return []
    try:
        raw = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for it in raw:
        if not (isinstance(it, (list, tuple)) and len(it) == 2):
            continue
        try:
            i, j = int(it[0]), int(it[1])
        except (TypeError, ValueError):
            continue
        # 只能回上面那条，不能回自己/未来；跨度也别太大——正常回复都在
        # 几条以内，隔十几条还「在回它」多半是模型硬凑（窗口 16 条时）
        if 0 <= j < i < n and (i - j) <= 6:
            out.append((i, j))
    return out


def pick_windows(rows, limit):
    """挑最值得问模型的窗口：发言人多、没有 @、条数够。

    规则的强项是 @ 和字面相似；这里专挑它的弱项——纯靠语气的多人交叉对话。
    """
    # 只留「有话题信息」的消息：占位（（空消息）/【图片】）跳过；
    # 去标点后不到 2 个字也跳过——「？」「草」「666」判不出在回谁，
    # 模型只能瞎凑（实测真的凑出一堆「？回？」）。
    rows = [r for r in rows
            if not M.is_placeholder(r["text"])
            and len(re.sub(r"[^\w\u4e00-\u9fff]", "", r["text"])) >= 2]
    scored = []
    for st in range(0, len(rows), WIN):
        win = rows[st:st + WIN]
        if len(win) < 4:
            continue
        speakers = len({r["uid"] for r in win})
        ats = sum(1 for r in win if r["at"])
        score = speakers * 2 - ats * 3 + len(win) * 0.1
        scored.append((score, st, win))
    scored.sort(key=lambda x: -x[0])
    return scored[:limit]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", help="群号（不填=所有群）")
    ap.add_argument("--limit", type=int, default=3, help="最多问几个窗口")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    ap.add_argument("--stats", action="store_true", help="只看现有覆盖")
    ap.add_argument("--model", default="minimax-m3.1-flash",
                    help="用哪个模型（实测 deepseek-v4.1-flash 在这类 prompt 上"
                         "会一直想、>120 秒不出，别用）")
    args = ap.parse_args()

    cfg = qa.load_config()
    bot = qa.AutoReply(cfg)
    mem = bot.mem
    mem.load_all()

    path = os.path.join(mem.archive_dir(), "_social_links.json")
    store = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                store = json.load(f) or {}
        except Exception:
            store = {}

    if args.stats:
        print("覆盖文件：%s" % path)
        if not store:
            print("  （还没有）")
        for gid, pairs in store.items():
            print("  群 %s：%d 条连线" % (gid, len(pairs)))
        return

    by_group = mem._group_rows()
    targets = [args.group] if args.group else sorted(by_group)
    total = 0
    for gid in targets:
        rows = sorted(by_group.get(str(gid)) or [], key=lambda r: r["t"])
        if len(rows) < 4:
            print("群 %s 消息太少，跳过" % gid)
            continue
        print("群 %s：%d 条消息，%d 人" % (
            gid, len(rows), len({r["uid"] for r in rows})))
        added = 0
        for score, st, win in pick_windows(rows, args.limit):
            if added >= MAX_MSGS:
                break
            prompt = build_prompt(win)
            print("  窗口 %d（%d 条，%d 人，分 %.1f）…"
                  % (st, len(win), len({r["uid"] for r in win}), score),
                  end=" ", flush=True)
            try:
                # tools="" 显式关掉联网工具：CLI 启动时初始化 WebSearch 没有
                # 超时保护，网络一抖就卡到 90 秒（这个坑之前在回复链上踩过）。
                # 判 reply-to 用不到联网，关掉最稳。
                out = bot._cli_invoke(prompt, tools="", model=args.model,
                                      timeout=110)
            except Exception as e:
                print("调用失败：%s" % e)
                continue
            links = parse_links(out, len(win))
            print("得到 %d 条连线" % len(links))
            for i, j in links:
                ci, cj = win[i], win[j]
                store.setdefault(str(gid), []).append(
                    [ci["ts"], ci["text"][:120], cj["ts"], cj["text"][:120]])
                added += 1
        print("  本群新增 %d 条" % added)
        total += added

    if args.dry_run:
        print("\n[dry-run] 未写文件。示例：")
        for gid, pairs in store.items():
            for p in pairs[:3]:
                print("  %s: %r 回 %r" % (gid, p[1][:24], p[3][:24]))
        return
    if not total:
        print("没有新增，不写文件")
        return
    # 去重
    for gid in list(store):
        seen, keep = set(), []
        for p in store[gid]:
            k = (p[0], p[1], p[2], p[3])
            if k not in seen:
                seen.add(k)
                keep.append(p)
        store[gid] = keep
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)
    os.replace(path + ".tmp", path)
    print("\n已写入 %s（共 %d 条）" % (path, total))


if __name__ == "__main__":
    main()
