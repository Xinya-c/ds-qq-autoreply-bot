#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键启动：NapCat + 自动回复
=====================================================
双击 一键启动.bat 后，本脚本会依次：
  1) 检查 NapCat 是否已在运行（在运行就跳过）
  2) 用记录的账号启动 NapCat（免扫码；没有记录则提示扫码）
  3) 等 3001 端口就绪，并自动配好 OneBot
  4) 启动自动回复守护进程（已有实例则跳过，避免重复回复）
"""

import os
import sys
import json
import time
import socket
import subprocess

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# 后台查询类子进程不弹窗（面板是 GUI 程序，继承不到控制台，
# 不设这个标志每查一次就闪一个窗口）
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

PKG = os.path.join(BASE, "NapCat.Shell.Windows.Node")
CNF = os.path.join(PKG, "napcat", "config")
WEBUI = os.path.join(CNF, "webui.json")
CACHE = os.path.join(BASE, "qq_accounts.json")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def load_cache():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def clean_env():
    """NapCat 必须用干净环境：WorkBuddy 注入的 NODE_OPTIONS 会让它崩溃。"""
    keep = ["SystemRoot", "windir", "TEMP", "TMP", "USERPROFILE", "APPDATA",
            "LOCALAPPDATA", "COMSPEC", "USERNAME", "USERDOMAIN",
            "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS"]
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.setdefault("SystemRoot", r"C:\Windows")
    env.setdefault("windir", r"C:\Windows")
    env.setdefault("COMSPEC", r"C:\Windows\system32\cmd.exe")
    env["PATH"] = r"C:\Windows\system32;C:\Windows;C:\Windows\System32\Wbem"
    return env


def port_open(port, timeout=1.0):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def procs_matching(keyword):
    """返回命令行里含 keyword 的进程 PID 列表。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | "
             "ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=30,
            creationflags=NO_WINDOW).stdout.strip()
        data = json.loads(out) if out else []
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    return [p["ProcessId"] for p in data
            if keyword.lower() in (p.get("CommandLine") or "").lower()]


def napcat_pids():
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
             "Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=30,
            creationflags=NO_WINDOW).stdout.strip()
        data = json.loads(out) if out else []
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    target = os.path.normcase(os.path.join(PKG, "node.exe"))
    return [p["ProcessId"] for p in data
            if p.get("ExecutablePath") and os.path.normcase(p["ExecutablePath"]) == target]


def choose_account():
    """优先用 webui.json 里记的账号；没有就用缓存里最近登录的；再没有就扫码。"""
    try:
        with open(WEBUI, encoding="utf-8") as f:
            auto = (json.load(f).get("autoLoginAccount") or "").strip()
        if auto:
            return auto
    except Exception:
        pass
    cache = load_cache()
    uins = [k for k in cache if k.isdigit()]
    if len(uins) == 1:
        return uins[0]
    return ""


def set_auto_login(uin):
    with open(WEBUI, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["autoLoginAccount"] = str(uin or "")
    with open(WEBUI, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=4)


def start_napcat():
    bat = os.path.join(PKG, "napcat.bat")
    subprocess.Popen(["cmd", "/c", bat], cwd=PKG, env=clean_env(),
                     creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


def daemon_pid():
    """通过 PID 文件判断守护进程是否在跑（比扫进程更可靠）。"""
    p = os.path.join(BASE, "qq_autoreply.pid")
    if not os.path.exists(p):
        return 0
    try:
        pid = int(open(p).read().strip() or 0)
    except Exception:
        return 0
    if not pid:
        return 0
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=15,
                             creationflags=NO_WINDOW).stdout
        return pid if str(pid) in (out or "") else 0
    except Exception:
        return 0


def start_daemon():
    bat = os.path.join(BASE, "start_autoreply.bat")
    subprocess.Popen(["cmd", "/c", bat], cwd=BASE,
                     creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


def main():
    log("=" * 52)
    log("一键启动：NapCat + 自动回复")
    log("=" * 52)

    # ---------- NapCat ----------
    # 双重判断：进程存在 或 端口在听，都算“已在运行”。
    # 只按端口判断会误判（未登录时 3001 还没起），进而重复启动两个实例互相搞挂。
    napcat_started = False
    running_pids = napcat_pids()
    if running_pids or port_open(3001):
        log("NapCat 已在运行（PID %s），跳过启动" % (", ".join(map(str, running_pids)) or "未知"))
    else:
        napcat_started = True
        uin = choose_account()
        log("启动账号：%s" % (uin or "扫码登录（未记录账号）"))
        try:
            set_auto_login(uin)
        except Exception as e:
            log("写入账号失败：%s" % e)

        log("启动 NapCat …（会弹出新窗口）")
        start_napcat()

        log("等待登录 / 3001 就绪（最多 180 秒，如需扫码请在 NapCat 窗口完成）…")
        ok = False
        for i in range(180):
            if port_open(3001):
                ok = True
                break
            if i and i % 20 == 0:
                log("  仍在等待… 已 %d 秒" % i)
            time.sleep(1)
        if not ok:
            log("3001 仍未就绪。可能还在扫码/登录中 —— 自动回复会持续重试，登录后自动接上。")
        else:
            log("NapCat 已就绪")

    # ---------- OneBot 配置（只在本次刚启动 NapCat 时重配，避免无谓的热重载掉线）----------
    if napcat_started and port_open(3001):
        try:
            r = subprocess.run([sys.executable, os.path.join(BASE, "apply_onebot.py")],
                               cwd=BASE, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120,
                               creationflags=NO_WINDOW)
            for line in (r.stdout or "").strip().splitlines():
                log("  " + line)
        except Exception as e:
            log("配置 OneBot 失败：%s" % e)

    # ---------- 自动回复 ----------
    existing = daemon_pid() or procs_matching("qq_autoreply.py")
    if existing:
        pids = existing if isinstance(existing, list) else [existing]
        log("自动回复已在运行（PID %s），跳过启动" % ", ".join(map(str, pids)))
    else:
        log("启动自动回复守护进程 …（会弹出新窗口）")
        start_daemon()
        time.sleep(3)
        log("已启动。两份日志：qq_autoreply.log")

    log("-" * 52)
    log("全部完成。关闭本窗口不影响后台两个程序。")
    log("要停止：分别关掉 NapCat 和 QQ Auto Reply 的窗口。")


if __name__ == "__main__":
    main()
