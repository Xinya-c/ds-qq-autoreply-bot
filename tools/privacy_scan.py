#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/privacy_scan.py —— 隐私自查：仓库里有没有把私人信息带出去。

检查范围：
  1. git 追踪的文本文件里有没有真实 QQ 号 / 昵称 / 密钥 / 本机路径
  2. 打包好的 zip 里有没有
  3. git **历史**里有没有（历史泄漏了，改当前文件是不够的）

私人信息从哪来是活的：本地 qq_messages/ 归档里有真实聊天记录，里面出现过的
QQ 号就是「私人信息」。这个脚本自动从归档里提取，而不是维护一份手写名单
（手写名单迟早会漏）。

  python tools/privacy_scan.py            # 扫当前 + 历史
  python tools/privacy_scan.py --no-history
"""

import argparse
import os
import re
import subprocess
import sys
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHIVE_DIR = os.path.join(BASE, "qq_messages")

SKIP_EXT = (".zip", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".onnx", ".exe",
            ".pyc", ".db", ".bin")
# 明显不是 QQ 号的数字（1MB/2MB 这类阈值、退出码、日期）
ALLOW_NUMS = {"1048576", "2097152", "5242880", "3221226505", "10000000",
               "19000101", "20991231", "20241021", "20261021", "20260706",
               "2048576", "52428800"}
SECRET_PAT = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|"
    r"(?:api[_-]?key|secret|token|password)\s*[=:]\s*[\"'][^\"'\n]{12,}[\"'])",
    re.I)
PATH_PAT = re.compile(r"(?:[A-Z]:\\Users\\[a-z][\w]*|/c/Users/[a-z][\w]*)")


def sh(args, **kw):
    return subprocess.run(args, cwd=BASE, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw).stdout


def real_ids_from_archive():
    """从本地归档里提取真实出现过的 QQ 号和昵称。"""
    qids, nicks = set(), set()
    if not os.path.isdir(ARCHIVE_DIR):
        return qids, nicks
    for root, _d, files in os.walk(ARCHIVE_DIR):
        for f in files:
            if not f.endswith(".md"):
                continue
            try:
                t = open(os.path.join(root, f), encoding="utf-8",
                         errors="replace").read()
            except Exception:
                continue
            for m in re.finditer(r"^\[[^\]]+\]\s*(.+?)\((\d{6,12})\):\s?(.*)$",
                                 t, re.M):
                nick, qid = m.group(1), m.group(2)
                if qid not in ALLOW_NUMS:
                    qids.add(qid)
                if nick and nick != "我" and len(nick) <= 20:
                    nicks.add(nick)
    return qids, nicks


def scan_text(name, text, qids, nicks, findings):
    for q in qids:
        if q in text:
            findings.append((name, "QQ号", q))
    for n in nicks:
        if n and n in text:
            findings.append((name, "昵称", n))
    for m in SECRET_PAT.finditer(text):
        findings.append((name, "疑似密钥", m.group(0)[:40]))
    for m in PATH_PAT.finditer(text):
        findings.append((name, "本机路径", m.group(0)))


def scan_worktree(qids, nicks, findings):
    files = sh(["git", "ls-files"]).split()
    for f in files:
        if f.lower().endswith(SKIP_EXT):
            continue
        p = os.path.join(BASE, f)
        try:
            t = open(p, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        scan_text(f, t, qids, nicks, findings)
    # 打包成品
    dist = os.path.join(BASE, "dist")
    for f in sorted(os.listdir(dist) if os.path.isdir(dist) else []):
        if not f.lower().endswith(".zip"):
            continue
        zp = os.path.join(dist, f)
        try:
            z = zipfile.ZipFile(zp)
        except Exception:
            continue
        for n in z.namelist():
            if n.lower().endswith(SKIP_EXT):
                continue
            try:
                t = z.read(n).decode("utf-8")
            except Exception:
                continue
            scan_text("dist/%s!%s" % (f, n), t, qids, nicks, findings)


def scan_history(qids, nicks, findings):
    """历史里也查一遍（历史泄漏了，光改当前文件不够）。"""
    for q in sorted(qids):
        out = sh(["git", "log", "--all", "--oneline", "-S%s" % q,
                  "--", "*.py", "*.md", "*.json", "*.bat", "*.txt"])
        for line in out.splitlines():
            if line.strip():
                findings.append(("git 历史", "QQ号", "%s（%s）" % (q, line[:40])))


def main():
    ap = argparse.ArgumentParser(description="隐私自查")
    ap.add_argument("--no-history", action="store_true", help="不查 git 历史")
    a = ap.parse_args()

    qids, nicks = real_ids_from_archive()
    print("从本地归档里提取到 %d 个 QQ 号、%d 个昵称（作为检查名单）"
          % (len(qids), len(nicks)))

    findings = []
    scan_worktree(qids, nicks, findings)
    if not a.no_history:
        scan_history(qids, nicks, findings)

    # 归档目录本身、配置文件、日志是被 gitignore 的，不在扫描范围
    if not findings:
        print("✓ 没发现私人信息泄漏")
        return 0
    print("✗ 发现 %d 处可疑：" % len(findings))
    for name, kind, val in findings:
        print("  %-46s %-8s %s" % (name, kind, val))
    return 1


if __name__ == "__main__":
    sys.exit(main())
