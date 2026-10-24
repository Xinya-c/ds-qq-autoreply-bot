#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/poke_probe.py —— 临时监听：看 NapCat 到底怎么推「戳一戳」。

用法：python tools/poke_probe.py     然后在 QQ 里戳一下机器人，等 30 秒。

它会打印收到的所有**非消息**事件（notice 等），以及消息里带 poke 段的消息。
之前 on_event 直接按 post_type != "message" 丢掉 notice，所以戳一戳没回应；
这个脚本用来确认 NapCat 的实际格式，好把处理写对。
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qq_mcp_server import OneBotWS   # noqa: E402

WS = os.environ.get("QQ_WS", "ws://127.0.0.1:3001")
DUR = int(os.environ.get("PROBE_SECONDS", "45"))


def main():
    print("连 %s，等 %d 秒——去 QQ 里戳一下机器人" % (WS, DUR))
    try:
        ob = OneBotWS(WS)
    except Exception as e:
        print("连不上: %s" % e)
        return 1
    seen = {"n": 0, "poke": 0}

    def on_msg(data):
        try:
            m = json.loads(data.decode("utf-8"))
        except Exception:
            return
        if m.get("post_type") == "message":
            segs = m.get("message")
            if isinstance(segs, list) and any(
                    isinstance(s, dict) and s.get("type") == "poke"
                    for s in segs):
                seen["poke"] += 1
                print("\n>>> 消息里带 poke 段：")
                print(json.dumps(m, ensure_ascii=False, indent=1)[:1200])
            return
        seen["n"] += 1
        print("\n>>> 非消息事件 #%d：" % seen["n"])
        print(json.dumps(m, ensure_ascii=False, indent=1)[:1200])
        if m.get("sub_type") == "poke":
            seen["poke"] += 1
            print("    ^^^ 这就是戳一戳！")

    ob.set_event_handler(on_msg)
    t0 = time.time()
    while time.time() - t0 < DUR:
        time.sleep(0.5)
    ob.close()
    print("\n结束：非消息事件 %d 个，其中戳一戳 %d 个" % (seen["n"], seen["poke"]))
    if not seen["poke"]:
        print("（没抓到戳一戳——可能你戳的那一下没传出去，或格式在别处）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
