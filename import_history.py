# -*- coding: utf-8 -*-
"""
把导出的聊天记录并入归档，让机器人「记得」这段历史。

用法：
    python import_history.py                      # 导入默认的两个目录
    python import_history.py daochu1 all          # 指定目录
    python import_history.py --dry-run            # 只看会做什么，不写文件

规则：
  - 导出文件里的「我」= 主人本人，归档里的「我」= 机器人代发的
    两者在归档里都写作「我」—— 因为对她开着「本人模式」，声音本来就该一致
  - 按时间戳与现有归档**归并排序**，不会盖掉已有的记录
  - 改之前自动备份为 <原名>.bak
"""
import os
import sys
import re
import glob
import shutil
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
ARCH_DIR = os.path.join(BASE, "qq_messages")
META_RE = re.compile(r"^会话号码：(\d+)")
LINE_RE = re.compile(r"^\[([\d\-]{10} [\d:]{8})\]\s*(.+?):\s?(.*)$")


def parse_export(path):
    """读导出文件，返回 (qq号, [(ts, 显示名, 内容), ...])。"""
    qq = None
    out = []
    try:
        raw = open(path, encoding="utf-8", errors="replace").read()
    except Exception as e:
        print("  读取失败 %s: %s" % (path, e))
        return None, []
    for ln in raw.splitlines():
        m = META_RE.match(ln.strip())
        if m:
            qq = m.group(1)
            continue
        m = LINE_RE.match(ln)
        if not m:
            continue
        ts, who, text = m.group(1), m.group(2), m.group(3).rstrip()
        if who == "我":
            disp = "我"
        else:
            disp = "%s(%s)" % (who, qq or "")
        out.append((ts, disp, text))
    return qq, out


def merge(conv_id, new_items, dry=False):
    """把 new_items 并入 qq_messages/private_<id>.md（按时间排序）。"""
    path = os.path.join(ARCH_DIR, "private_%s.md" % conv_id)
    header = "# QQ 私聊归档 - %s\n\n> 单文件超 1048576 字节自动轮转为 _N.md。\n\n" % conv_id
    old = []
    if os.path.exists(path):
        for ln in open(path, encoding="utf-8", errors="replace"):
            m = LINE_RE.match(ln.rstrip("\n"))
            if m:
                old.append((m.group(1), m.group(2), m.group(3)))
    merged = old + new_items
    merged.sort(key=lambda x: x[0])          # 时间字符串可直接字典序比较
    # 去重：同一秒同一人同一内容
    seen, final = set(), []
    for it in merged:
        k = (it[0], it[1], it[2])
        if k in seen:
            continue
        seen.add(k)
        final.append(it)
    body = "".join("[%s] %s: %s\n" % it for it in final)
    print("   现有 %d 条 + 导入 %d 条 -> 合并后 %d 条" % (len(old), len(new_items), len(final)))
    if dry:
        return
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
        print("   已备份 -> %s.bak" % os.path.basename(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + body)
    print("   已写入 %s（%.0f KB）" % (os.path.basename(path),
                                       os.path.getsize(path) / 1024.0))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    dirs = args or ["daochu1", "all"]
    print("=" * 56)
    print("导入聊天记录%s" % ("（演练，不写文件）" if dry else ""))
    print("=" * 56)
    for d in dirs:
        dp = d if os.path.isabs(d) else os.path.join(BASE, d)
        files = sorted(glob.glob(os.path.join(dp, "*.txt")))
        files = [f for f in files if not os.path.basename(f).startswith("会话清单")]
        if not files:
            print("目录为空或不存在：%s" % dp)
            continue
        print("\n[%s] %d 个文件" % (d, len(files)))
        for f in files:
            qq, items = parse_export(f)
            if not qq or not items:
                continue
            name = os.path.basename(f).replace("私聊_", "").rsplit(".", 1)[0]
            print("  %s (%s) %d 条" % (name, qq, len(items)))
            if "群聊" in os.path.basename(f):
                print("    群聊暂不导入（当前只支持私聊归档）")
                continue
            merge(qq, items, dry)
    print("\n完成。重启守护进程后会自动把积压的历史压缩成阶段回顾。")


if __name__ == "__main__":
    main()
