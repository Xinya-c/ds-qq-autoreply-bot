#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/sync_cli_auth.py —— 把明文登录态同步给 CodeBuddy CLI。

为什么需要这个
==============
CodeBuddy CLI 找凭证时**只读** `workbuddy-desktop.info`，而 WorkBuddy 桌面端
用 DPAPI 把 token 加密存在那个文件里（`$wbEncrypted` envelope）。CLI 是独立
进程，拿不到解密密钥，日志里会写：

    [AtRestEncryption] unavailable adapter=credential-fields
    resource=auth/workbuddy-desktop.info category=missing-key role=cbc

于是 `hasSession=true` 但 `hasAccessToken=false` → 每条回复都报
"Authentication required. Please use /login command" → 机器人对所有消息失明。

而你用 login_codebuddy.bat 登录时，CLI 会把**明文 JWT** 写进
`Tencent-Cloud.coding-copilot.info`。但 CLI 运行时不读那个文件。

所以：把 Tencent-Cloud 里的明文 auth 段复制到 workbuddy-desktop.info，
并把它设成**只读**（`attrib +R`）——否则 WorkBuddy 桌面端几分钟后会重新
加密覆盖，CLI 又读不了。

用法
====
    python tools/sync_cli_auth.py          # 同步 + 上只读锁 + 自检
    python tools/sync_cli_auth.py --check  # 只看状态，不改动
    python tools/sync_cli_auth.py --unlock # 解除只读（WorkBuddy 要写回时用）

什么时候需要重跑
================
- token 过期（accessToken 的 exp 到期）→ 先 login_codebuddy.bat 重新登录，
  再跑一次本脚本
- 换了账号
- 发现又开始报 Authentication required

注意：上只读锁后 WorkBuddy 桌面端自己写不进这个文件。这是有意的——它的
加密格式 CLI 解不开，两边没法共存。CLI 能用更重要。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

AUTH_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or
    os.path.join(os.path.expanduser("~"), "AppData", "Local"),
    "CodeBuddyExtension", "Data", "Public", "auth")
SRC = os.path.join(AUTH_DIR, "Tencent-Cloud.coding-copilot.info")
DST = os.path.join(AUTH_DIR, "workbuddy-desktop.info")


def is_readonly(path):
    """文件是否带只读属性（Windows 的 R 位）。"""
    try:
        # attrib 的输出是系统 OEM 代码页（中文系统是 GBK），不能按 utf-8 解
        out = subprocess.run(["attrib", path], capture_output=True,
                             encoding="utf-8", errors="replace").stdout
        # 形如 "A    R     C:\path\to\file"
        flags = out.split(path)[0].strip()
        return "R" in flags
    except Exception:
        return False


def set_readonly(path, on):
    flag = "+R" if on else "-R"
    return subprocess.run(["attrib", flag, path],
                          capture_output=True).returncode == 0


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def describe(d, label, fname=None):
    a = (d or {}).get("auth") or {}
    tok = a.get("accessToken")
    kind = "加密($wbEncrypted)" if isinstance(tok, dict) else \
        ("明文JWT" if isinstance(tok, str) else "缺失")
    exp = a.get("expiresAt")
    exp_txt = ""
    if isinstance(exp, (int, float)):
        exp_txt = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp / 1000.0))
        left = exp / 1000.0 - time.time()
        exp_txt += "（%s）" % ("还剩 %.0f 天" % (left / 86400) if left > 0 else "已过期")
    ro = is_readonly(os.path.join(AUTH_DIR, fname)) if fname else False
    print("  %-16s accessToken=%-16s 到期 %s  %s"
          % (label, kind, exp_txt or "?", "[只读]" if ro else ""))
    return kind


def check():
    print("认证目录: %s" % AUTH_DIR)
    ok = False
    for fn, label in ((SRC, "Tencent-Cloud"), (DST, "workbuddy-desktop")):
        p = os.path.join(AUTH_DIR, fn)
        if not os.path.exists(p):
            print("  %-14s 不存在" % label)
            continue
        kind = describe(load(p), label, fn)
        if label == "workbuddy-desktop" and kind == "明文JWT":
            ok = True
    print()
    print("CLI 可用性判断: %s" % ("✓ workbuddy-desktop 是明文，CLI 能读"
                                 if ok else
                                 "✗ workbuddy-desktop 不是明文，CLI 会报"
                                 " Authentication required"))
    return 0 if ok else 1


def sync():
    if not os.path.exists(SRC):
        print("✗ 找不到 %s" % SRC)
        print("  先双击 login_codebuddy.bat 登录一次（浏览器里选 Chinese Site）")
        return 1
    if not os.path.exists(DST):
        print("✗ 找不到 %s" % DST)
        return 1

    src = load(SRC)
    src_kind = describe(src, "Tencent-Cloud", os.path.basename(SRC))
    if src_kind != "明文JWT":
        print("✗ Tencent-Cloud 里的 token 不是明文，没法同步")
        print("  说明还没用 login_codebuddy.bat 登录成功过，先登录")
        return 1

    # 先解锁才能写
    set_readonly(DST, False)
    # 备份（保留最近 3 个）
    bak = DST + ".bak-" + time.strftime("%Y%m%d_%H%M%S")
    shutil.copy2(DST, bak)
    olds = sorted([f for f in os.listdir(AUTH_DIR) if f.startswith(
        "workbuddy-desktop.info.bak-")])
    for f in olds[:-3]:
        try:
            os.remove(os.path.join(AUTH_DIR, f))
        except Exception:
            pass

    dst = load(DST)
    dst["auth"] = src["auth"]
    with open(DST, "w", encoding="utf-8") as f:
        json.dump(dst, f, ensure_ascii=False, indent=2)

    # 上只读锁：不然 WorkBuddy 桌面端几分钟后会加密覆盖
    locked = set_readonly(DST, True)
    print()
    print("✓ 已同步明文 token 并%s" % ("上只读锁" if locked else "（只读设置失败！）"))
    print("  备份: %s" % os.path.basename(bak))
    print()
    return check()


def unlock():
    if set_readonly(DST, False):
        print("✓ 已解除只读（WorkBuddy 桌面端可以写回了）")
        print("  注意：它写回的是加密格式，CLI 又会读不了，需要重新 sync")
        return 0
    print("✗ 解除失败")
    return 1


def main():
    ap = argparse.ArgumentParser(description="把明文登录态同步给 CodeBuddy CLI")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="只看状态，不改动")
    g.add_argument("--unlock", action="store_true", help="解除只读锁")
    a = ap.parse_args()
    if a.check:
        return check()
    if a.unlock:
        return unlock()
    return sync()


if __name__ == "__main__":
    sys.exit(main())
