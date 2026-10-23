#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NapCat 换 QQ 号辅助脚本
-----------------------
作用：停掉 NapCat → 备份并清理「旧号的登录态」→ 提示你重新启动并扫码新号。

只删除以下内容（且全部先备份到 _backup_login_<时间戳>/ 里）：
  - guild1.db / guild1.db-shm / guild1.db-wal   （QQ 本地登录缓存）
  - napcat/config/napcat_<旧号>.json
  - napcat/config/napcat_protocol_<旧号>.json
  - napcat/config/onebot11_<旧号>.json
不会碰：webui.json、napcat.json、node_modules、程序本体、聊天记录备份等。

用法：
    python switch_account.py
"""

import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(ROOT, "NapCat.Shell.Windows.Node")
CONF = os.path.join(PKG, "napcat", "config")


def find_napcat_pids():
    """按可执行文件路径精确匹配，避免误杀用户其它 node 进程。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
             "Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except Exception as e:
        print("  [!] 无法枚举进程:", e)
        return []

    import json
    if not out:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]

    target = os.path.normcase(os.path.join(PKG, "node.exe"))
    pids = []
    for p in data:
        exe = p.get("ExecutablePath") or ""
        if exe and os.path.normcase(exe) == target:
            pids.append(p.get("ProcessId"))
    return pids


def collect_targets():
    files = []
    for name in ("guild1.db", "guild1.db-shm", "guild1.db-wal"):
        p = os.path.join(PKG, name)
        if os.path.exists(p):
            files.append(p)

    if os.path.isdir(CONF):
        for name in os.listdir(CONF):
            if not name.endswith(".json"):
                continue
            if name.startswith(("napcat_", "napcat_protocol_", "onebot11_")):
                # 排除无账号后缀的全局配置
                stem = name[:-5]
                if stem.count("_") >= 1 and stem.split("_")[-1].isdigit():
                    files.append(os.path.join(CONF, name))
    return files


def clear_auto_login():
    """把 webui.json 的 autoLoginAccount 置空，避免启动时自动登回旧号。"""
    path = os.path.join(CONF, "webui.json")
    if not os.path.exists(path):
        return
    import json
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        old = cfg.get("autoLoginAccount", "")
        if not old:
            print("  [+] autoLoginAccount 已为空，无需处理")
            return
        cfg["autoLoginAccount"] = ""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=4)
        print(f"  [+] 已清除「快速登录QQ」({old})")
    except Exception as e:
        print("  [!] 清除 autoLoginAccount 失败:", e)


def main():
    print("=" * 56)
    print(" NapCat 换号助手")
    print("=" * 56)

    pids = find_napcat_pids()
    if pids:
        print(f"\n[1/3] 正在停止 NapCat（PID: {', '.join(map(str, pids))}）...")
        for pid in pids:
            subprocess.run(["taskkill", "/f", "/pid", str(pid)],
                           capture_output=True, text=True)
        time.sleep(3)
        left = find_napcat_pids()
        if left:
            print("  [!] 还有进程没退出：", left, "——请手动在任务管理器结束。")
            return 1
        print("  [+] NapCat 已停止")
    else:
        print("\n[1/3] NapCat 当前没在运行，跳过")

    targets = collect_targets()
    if not targets:
        print("\n[2/3] 没找到需要清理的登录态文件，直接启动扫码即可。")
        return 0

    print("\n[2/3] 准备清理以下登录态（会先备份）：")
    for t in targets:
        print("   -", os.path.relpath(t, ROOT))

    ans = input("\n确认清理？输入 y 继续，其它任意键取消: ").strip().lower()
    if ans != "y":
        print("已取消，未做任何修改。")
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(ROOT, f"_backup_login_{stamp}")
    os.makedirs(backup, exist_ok=True)
    for t in targets:
        rel = os.path.relpath(t, PKG).replace(os.sep, "_")
        shutil.copy2(t, os.path.join(backup, rel))
    print(f"  [+] 已备份到 {backup}")

    for t in targets:
        try:
            os.remove(t)
            print("  已删除", os.path.relpath(t, ROOT))
        except Exception as e:
            print("  [!] 删除失败", t, e)

    # 关键：清掉「快速登录QQ」(webui.json 的 autoLoginAccount)，
    # 否则 NapCat 启动时会直接用这个号自动登录，跳过扫码。
    clear_auto_login()

    print("\n[3/3] 完成。接下来：")
    print("   1) 双击 NapCat.Shell.Windows.Node\\napcat.bat")
    print("   2) 在弹出的窗口里用【新号】扫码登录")
    print("   3) 登录成功后，运行 apply_onebot.py 恢复 3001 端口：")
    print("      python apply_onebot.py")
    print("   （apply_onebot.py 跑完，WorkBuddy 的 qq-mcp 就能直接用，无需改任何配置）")
    print(f"\n   如需回滚：把 {backup} 里的文件拷回原位置即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
