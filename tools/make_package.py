#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/make_package.py —— 打包成可直接发给别人用的 zip。

之前这个包是手工打的（10-21 那版），结果：
- 里面**没有 memory.py**（记忆模块是后来才有的），拿到手根本跑不起来
- 再打一次就得手工重来一遍，很容易漏文件

所以写了这个脚本：文件清单写死在这里，重复执行结果一致。

  python tools/make_package.py
  python tools/make_package.py --out dist/别的名字.zip
"""

import argparse
import os
import shutil
import sys
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(BASE, "dist")
DEFAULT_OUT = os.path.join(DIST, "QQ自动回复工具包.zip")

# 打包进 zip 的文件（相对仓库根）。顺序无所谓，脚本会按名字排序。
FILES = [
    "qq_autoreply.py",          # 守护进程主程序
    "qq_mcp_server.py",         # OneBot 客户端 / MCP 服务
    "qq_manager.py",            # 账号管理
    "boot_all.py",              # 一键启动
    "apply_onebot.py",          # 写 NapCat 的 OneBot 配置
    "switch_account.py",        # 切号
    "ws_probe.py",              # 连通性探测
    "memory.py",                # 记忆模块（核心，别漏）
    "memory_vector.py",         # 可选：语义向量检索
    "login_codebuddy.bat",
    "start_autoreply.bat",
    "一键启动.bat",
    "启动QQ.bat",
    "打开控制面板.bat",
    "qq_panel.py",              # 控制面板
    "docs/QQ自动回复系统-项目总结.md",
    "assets/replies/stickers.json",
    "assets/replies/SOURCE.txt",
]

# 目录（整个拷进去）
DIRS = [
    "tools",                    # 工具与测试脚本
]

# 配置文件模板：单独改名成 sample 放进去
CONFIG_SAMPLE = {
    "qq_autoreply_config.example.json": "tools/qq_autoreply_config.sample.json",
}

README = """QQ 自动回复工具包
================================================

包含：守护进程、记忆模块、MCP 服务、账号管理、控制面板、一键启动脚本、
      表情包清单、项目总结文档、回归测试脚本。

使用方法
1) 安装 NapCat（本包不含），把 OneBot 正向 WebSocket 开在 127.0.0.1:3001
2) 复制 tools/qq_autoreply_config.sample.json 为 qq_autoreply_config.json，
   填入白名单与 API Key
3) 安装依赖：python-docx、Pillow、pypdf
4)（可选）安装 CodeBuddy CLI 并登录，用于 codebuddy_cli 后端；
   也可装 sentence-transformers 启用语义检索（memory_vector）
5) 双击 一键启动.bat

回归测试（可选，跑一下确认环境正常）
  python tools/e2e_smoke.py        # 端到端冒烟，127 项
  python tools/golden_prompt.py    # 提示词拼装基线
  python tools/proactive_replay.py # 群聊主动接话回放

注意
- 本包不含聊天记录、图片、日志、API Key。
- 表情包图片未包含（体积与授权原因），仅含 stickers.json 清单；
  需要时把图片放进 assets/replies/ 即可，文件名与清单对应。
- .bat 文件必须是 ASCII + CRLF，否则 cmd 会解析失败（这是踩过的坑）。
- config 里的 owner_qq 填主人的 QQ 号，机器人据此判断「谁说话才算数」；
  留空则不启用这个判断。
"""


def _add(z, src, arc, base):
    """把文件加进 zip；.bat 强制转成 CRLF + ASCII。

    .bat 必须是 CRLF，否则 cmd 解析会出问题（README 里记着这个坑，
    实际也踩过：打开控制面板.bat 一度是裸 LF）。
    """
    if arc.lower().endswith(".bat"):
        raw = open(src, "rb").read()
        raw = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        # 非 ASCII 字符在 cmd 里会乱码
        raw = bytes(c if c < 128 else 63 for c in raw)
        z.writestr(arc, raw)
    else:
        z.write(src, arc)
    return 1


def build(out_path=DEFAULT_OUT, base=BASE):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    n = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in sorted(FILES):
            src = os.path.join(base, rel)
            if not os.path.exists(src):
                print("  跳过（不存在）：%s" % rel)
                continue
            n += _add(z, src, rel.replace("\\", "/"), base)
        for rel in sorted(DIRS):
            src = os.path.join(base, rel)
            if not os.path.isdir(src):
                continue
            for root, _dirs, files in os.walk(src):
                if "__pycache__" in root:
                    continue
                for f in sorted(files):
                    if f.endswith((".pyc", ".pyo")):
                        continue
                    p = os.path.join(root, f)
                    arc = os.path.relpath(p, base).replace("\\", "/")
                    n += _add(z, p, arc, base)
        for src_rel, arc in sorted(CONFIG_SAMPLE.items()):
            src = os.path.join(base, src_rel)
            if os.path.exists(src):
                n += _add(z, src, arc, base)
        z.writestr("README.txt", README)
        n += 1
    shutil.move(tmp, out_path)
    size = os.path.getsize(out_path)
    print("打包完成：%s（%d 个文件，%.1f KB）" % (out_path, n, size / 1024.0))
    return out_path


def main():
    ap = argparse.ArgumentParser(description="打包 QQ 自动回复工具包")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出 zip 路径")
    ap.add_argument("--base", default=BASE, help="仓库根目录")
    a = ap.parse_args()
    build(a.out, a.base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
