#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NapCat OneBot 配置一键修复工具
-------------------------------
作用：把当前登录的 QQ 账号的 OneBot 正向 WebSocket（127.0.0.1:3001）配置好并启用。
适用场景：换了 QQ 号之后，新账号没有 OneBot 配置，WorkBuddy 的 qq-mcp 连不上。

原理：读取 napcat/config/webui.json 里的 token，登录 WebUI，调用
      /api/OB11Config/GetConfig -> 修改 -> /api/OB11Config/SetConfig。
      NapCat 会热重载，无需重启。

用法（NapCat 运行中执行）：
    python apply_onebot.py
"""

import hashlib
import json
import os
import sys
import time
import urllib.request

CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "NapCat.Shell.Windows.Node", "napcat", "config",
)

WS_HOST = "127.0.0.1"
WS_PORT = 3001
WS_NAME = "workbuddy-ws"


def load_webui():
    path = os.path.join(CONFIG_DIR, "webui.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def api(path, payload, credential=None, port=6099):
    url = f"http://127.0.0.1:{port}/api{path}"
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["Authorization"] = "Bearer " + credential
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except Exception as e:
        body = ""
        if hasattr(e, "read"):
            try:
                body = e.read()[:300].decode("utf8", "ignore")
            except Exception:
                pass
        return {"code": -1, "message": f"{e} {body}".strip()}


def login_token():
    cfg = load_webui()
    token = cfg.get("token", "")
    port = int(cfg.get("port", 6099))
    if not token:
        return None, port
    # NapCat WebUI 的登录密码是 sha256(token + '.napcat') 的形式提交
    h = hashlib.sha256((token + ".napcat").encode()).hexdigest()
    r = api("/auth/login", {"hash": h}, port=port)
    cred = (r.get("data") or {}).get("Credential")
    return cred, port


def current_account(port):
    """/QQLogin/GetAllUsers 只能看到账号列表，实际登录号从 onebot 侧更准；这里尽力而为。"""
    return None


def build_server():
    return {
        "name": WS_NAME,
        "enable": True,
        "host": WS_HOST,
        "port": WS_PORT,
        "messagePostFormat": "array",
        "reportSelfMessage": False,
        "token": "",
        "debug": False,
        "heartInterval": 30000,
        "enableForcePushEvent": True,
    }


def main():
    cred, port = login_token()
    if not cred:
        print("[x] 拿不到 WebUI 凭据：请确认 NapCat 正在运行，且 napcat/config/webui.json 可读。")
        return 1
    print(f"[+] WebUI 登录成功（端口 {port}）")

    r = api("/OB11Config/GetConfig", {}, cred, port)
    cfg = r.get("data")
    if not cfg:
        print("[x] 读取 OneBot 配置失败：", json.dumps(r, ensure_ascii=False)[:300])
        return 1

    net = cfg.setdefault("network", {})
    servers = net.get("websocketServers") or []

    exists = False
    for s in servers:
        if s.get("port") == WS_PORT or s.get("name") == WS_NAME:
            s.update(build_server())
            exists = True
            break
    if not exists:
        servers.append(build_server())
    net["websocketServers"] = servers

    ok = False
    for payload in ({"config": json.dumps(cfg, ensure_ascii=False)}, cfg):
        resp = api("/OB11Config/SetConfig", payload, cred, port)
        if resp.get("code") == 0:
            ok = True
            break
    if not ok:
        print("[x] 写入失败")
        return 1

    print("[+] 配置已写入，等待热重载...")
    time.sleep(2)

    # 校验端口是否真的起来了
    import socket
    for _ in range(10):
        s = socket.socket()
        s.settimeout(2)
        try:
            s.connect((WS_HOST, WS_PORT))
            print(f"[+] {WS_HOST}:{WS_PORT} 已监听，OneBot 就绪 ✅")
            return 0
        except Exception:
            time.sleep(1)
        finally:
            s.close()

    print(f"[!] 配置已保存，但 {WS_PORT} 还没监听。试试在 WebUI 里点「重启进程」。")
    return 2


if __name__ == "__main__":
    sys.exit(main())
