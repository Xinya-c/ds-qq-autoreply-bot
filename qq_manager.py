#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ 账号与状态管理器
=====================================================
一个菜单解决两件事：
  1) 重启后自己选登哪个号（免扫码快速登录 / 扫码登录新号）
  2) 登录后切换在线状态（在线、离开、忙碌、请勿打扰、隐身、听歌中…）

用法：
  双击 启动QQ.bat          → 进入菜单
  python qq_manager.py --list
  python qq_manager.py --start <你的QQ号>
  python qq_manager.py --start qr
  python qq_manager.py --status 忙碌
"""

import os
import sys
import glob
import json
import time
import socket
import argparse
import subprocess
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(BASE, "NapCat.Shell.Windows.Node")
CONF = os.path.join(PKG, "napcat", "config")
WEBUI = os.path.join(CONF, "webui.json")
CACHE = os.path.join(BASE, "qq_accounts.json")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 在线状态：名称 -> (status, ext_status, battery_status)
STATUS_MAP = {
    "在线": (10, 0, 0),
    "Q我吧": (60, 0, 0),
    "离开": (30, 0, 0),
    "忙碌": (50, 0, 0),
    "请勿打扰": (70, 0, 0),
    "隐身": (40, 0, 0),
    "听歌中": (10, 1028, 0),
}


# ------------------------------------------------------------------ 基础
def load_cache():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(d):
    try:
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def list_accounts():
    """扫描本地配置，列出曾登录过的账号。"""
    cache = load_cache()
    uins = []
    for p in glob.glob(os.path.join(CONF, "napcat_[0-9]*.json")):
        name = os.path.basename(p)
        uin = name[len("napcat_"):-len(".json")]
        if uin.isdigit() and uin not in uins:
            uins.append(uin)
    out = []
    for u in uins:
        out.append({
            "uin": u,
            "nick": cache.get(u, ""),
            "onebot": os.path.exists(os.path.join(CONF, "onebot11_%s.json" % u)),
        })
    return out


def find_pids():
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
             "Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return []
    if not out:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    target = os.path.normcase(os.path.join(PKG, "node.exe"))
    return [p["ProcessId"] for p in data
            if p.get("ExecutablePath") and os.path.normcase(p["ExecutablePath"]) == target]


def stop_napcat():
    pids = find_pids()
    for pid in pids:
        subprocess.run(["taskkill", "/f", "/pid", str(pid)],
                       capture_output=True, text=True)
    if pids:
        time.sleep(3)
    return len(pids)


def set_auto_login(uin):
    """写 webui.json 的 autoLoginAccount；空字符串表示不自动登录（弹扫码）。"""
    with open(WEBUI, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["autoLoginAccount"] = str(uin or "")
    with open(WEBUI, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=4)


def clean_env():
    """给 NapCat 的 node 进程一个干净环境。

    WorkBuddy 会给子进程注入 NODE_OPTIONS（--require 语言注入模块）、SERVER__PORT 等，
    这些被带进 NapCat 自带的 node.exe 会导致 libuv 断言崩溃
    （Assertion failed: process_title / 退出码 3221226505）。
    用户双击 napcat.bat 时环境是干净的，所以能正常启动。
    """
    keep = ["SystemRoot", "windir", "TEMP", "TMP", "USERPROFILE", "APPDATA",
            "LOCALAPPDATA", "COMSPEC", "USERNAME", "USERDOMAIN",
            "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS"]
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.setdefault("SystemRoot", r"C:\Windows")
    env.setdefault("windir", r"C:\Windows")
    env.setdefault("COMSPEC", r"C:\Windows\system32\cmd.exe")
    env["PATH"] = r"C:\Windows\system32;C:\Windows;C:\Windows\System32\Wbem"
    return env


def start_napcat():
    bat = os.path.join(PKG, "napcat.bat")
    subprocess.Popen(["cmd", "/c", bat], cwd=PKG, env=clean_env(),
                     creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


def port_open(port, host="127.0.0.1", timeout=1.0):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def wait_port(port, seconds=90):
    for _ in range(int(seconds)):
        if port_open(port):
            return True
        time.sleep(1)
    return False


# ------------------------------------------------------------------ OneBot
def ob_call(action, params=None, timeout=15):
    from qq_mcp_server import OneBotWS
    ob = OneBotWS("ws://127.0.0.1:3001")
    try:
        return ob.call(action, params or {}, timeout=timeout)
    finally:
        ob.close()


def login_info():
    r = ob_call("get_login_info")
    if r.get("ok"):
        return r["data"]
    return None


def apply_onebot():
    r = subprocess.run([sys.executable, os.path.join(BASE, "apply_onebot.py")],
                       cwd=BASE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    return (r.stdout or "").strip()


def set_status(name):
    if name not in STATUS_MAP:
        return False, "未知状态：%s（可选：%s）" % (name, "、".join(STATUS_MAP))
    s, e, b = STATUS_MAP[name]
    r = ob_call("set_online_status",
                {"status": s, "ext_status": e, "battery_status": b})
    if r.get("ok"):
        cache = load_cache()
        cache["_last_status"] = name
        cache["_status_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_cache(cache)
    return bool(r.get("ok")), r


# ------------------------------------------------------------------ 动作
def do_start(uin):
    """uin 为 QQ 号（免扫码）或 'qr'（扫码登录新号）。"""
    accounts = {a["uin"] for a in list_accounts()}
    auto = "" if uin == "qr" else str(uin)
    if auto and auto not in accounts:
        print("  [!] 本地没有 %s 的登录记录，仍会尝试自动登录；失败则需要扫码。" % auto)

    print("  [1/4] 停止现有 NapCat …")
    n = stop_napcat()
    print("        已停止 %d 个进程" % n)

    print("  [2/4] 写入启动账号：%s" % (auto or "扫码登录（不自动）"))
    set_auto_login(auto)

    print("  [3/4] 启动 NapCat …")
    start_napcat()

    print("  [4/4] 等待登录（最多 120 秒）…")
    ok_port = wait_port(3001, 120)
    if not ok_port:
        # 可能还在扫码界面
        if port_open(6099):
            print("        6099 已起，但 3001 未就绪 —— 请在 NapCat 窗口完成扫码登录，")
            print("        登录后运行：python apply_onebot.py")
        else:
            print("        启动超时，请看 NapCat 窗口的报错。")
        return False

    info = login_info()
    if info:
        cache = load_cache()
        cache[str(info["user_id"])] = info.get("nickname") or ""
        save_cache(cache)
        print("        已登录：%s（%s）" % (info.get("nickname"), info["user_id"]))
    else:
        print("        3001 已监听，但取不到账号信息")
    print("        为当前账号配置 OneBot 3001 …")
    print("        " + (apply_onebot() or "(无输出)").replace("\n", "\n        "))
    return True


def show_status():
    if not port_open(3001):
        print("  NapCat 未运行（3001 未监听）")
        return
    info = login_info()
    if info:
        print("  当前账号：%s（%s）" % (info.get("nickname"), info["user_id"]))
        cache = load_cache()
        cache[str(info["user_id"])] = info.get("nickname") or ""
        save_cache(cache)
    else:
        print("  取不到账号信息")
    cache = load_cache()
    if cache.get("_last_status"):
        print("  最近设置的状态：%s（%s 设置）" % (cache["_last_status"], cache.get("_status_at", "-")))
    else:
        print("  最近设置的状态：未记录（菜单选 t 设置）")


def menu():
    while True:
        print()
        print("=" * 56)
        print(" QQ 管理")
        print("=" * 56)
        print(" 已登录过的账号：")
        accs = list_accounts()
        if not accs:
            print("   (无)")
        for i, a in enumerate(accs, 1):
            print("  %d) %s %s%s" % (i, a["uin"], a["nick"] or "",
                                     "" if a["onebot"] else "（无 OneBot 配置）"))
        print("  s) 扫码登录新账号")
        print("  t) 设置在线状态")
        print("  i) 查看当前账号")
        print("  q) 退出")
        print("-" * 56)
        ch = input(" 请选择：").strip().lower()

        if ch == "q":
            return
        if ch == "s":
            do_start("qr")
            continue
        if ch == "i":
            show_status()
            continue
        if ch == "t":
            print(" 可选状态：" + "、".join(STATUS_MAP))
            name = input(" 输入状态名：").strip()
            ok, raw = set_status(name)
            print(" " + ("✅ 已设置为 %s" % name if ok else "❌ 失败：%s" % raw))
            continue
        if ch.isdigit() and 1 <= int(ch) <= len(accs):
            do_start(accs[int(ch) - 1]["uin"])
            continue
        print(" 无效选择")


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--start")
    ap.add_argument("--status")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    if args.list:
        for a in list_accounts():
            print(a["uin"], a["nick"], "onebot" if a["onebot"] else "-")
        return
    if args.start:
        do_start(args.start)
        return
    if args.status:
        ok, raw = set_status(args.status)
        print("ok" if ok else "failed", raw)
        return
    if args.show:
        show_status()
        return
    menu()


if __name__ == "__main__":
    main()
