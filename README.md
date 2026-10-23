# QQ 自动回复机器人

基于 [NapCat](https://github.com/NapNeko/NapCatQQ)（OneBot v11）+ 大模型的 QQ 自动回复机器人：私聊白名单自动回，群里被 @ 才回；带人设（system prompt）、三层记忆（滚动摘要 / 阶段回顾 / 说话风格档案）、关系档案、表情包自动配图、图片/语音/文件理解。

## 功能

- **两种触发**：私聊白名单内自动回复；群聊仅在被 @ 时回复（自动加 @ 前缀）
- **人设可定制**：全部集中在配置的 `system_prompt` 一个字段里，随改随生效
- **三层记忆**（按会话隔离，全部落盘 `qq_messages/`，重启不丢）：
  - 短期：最近 N 条原文（`context_messages`）
  - 中期：每 200 条压一段「阶段回顾」
  - 长期：每 50 条一次「事实摘要 + 关系档案」；全局一份「说话风格档案」
- **旧事检索**：拿当前消息去归档找相关旧记忆——装了可选依赖走**语义向量**
  （bge-small-zh 本地模型，容忍换说法），没装走关键词（二元组+DF 过滤）；
  两种都过「四路噪音过滤」，取不到就安静不注入；「前天说的那个」按日期直接筛
- **离线感知 + 启动补回**：NapCat/QQ 重启后补推的消息，以及开机扫归档发现的
  「对方发过、我们没回上」的消息（守护进程宕了/模型挂了/发送失败了），
  都会算出**距今多久**（`2 小时 15 分钟`／`3 天`）塞进 prompt，
  由模型自己判断该用什么口吻回——不用代码去猜「隔多久算久」。
  措辞还区分来源：离线补推说「当时不在线」，启动补回说「当时没回上」，
  免得机器人把漏回说成掉线。超时限的不补（`sweep.max_age_hours`，
  默认 24 小时，放宽后最老可补到 7 天前的旧消息——
  但越旧越容易「对着几天前的话题突然回一句」）
- **群聊主动搭话**：没人 @ 它也会偶尔插一句。从本次上线开始数群里的他人消息，
  攒够 `proactive.min_msgs~max_msgs`（默认 10~15，每次随机抽一个）就安排一次；
  过冷却（`cooldown_sec`，默认 10 分钟）和每小时上限（`max_per_hour`，默认 3）
  才真的说。**说不说、怎么说由模型自己判断**——它会拿到群里最近的聊天记录和
  群档案，可以选择接一句，也可以输出「跳过」安静待着（接不上嘴、群里在说正事/
  吵架时它就该闭嘴）。被 @ 回复过也会重新计数。频率太吵就调大 `min_msgs` 或
  设 `proactive.enabled: false`
- **多媒体**：图片存盘后交给模型看内容；语音转写；文件提取文字
- **表情包**：`assets/replies/` 图库 + `stickers.json` 场景标注，模型自行决定何时配图
- **模型链**：主力模型 429（日额度耗尽）时自动顺位切备选；CLI 挂了可降级直连 API
- **连发合并**：3 秒内的连发合并成一条一起回，回复过长自动拆多条
- **控制面板**：`qq_panel.py`，深色 HeroUI 风格，覆盖配置/日志/启动停止

## 环境要求

- Python 3.10+（核心纯标准库；可选依赖见下）
- [NapCat](https://github.com/NapNeko/NapCatQQ)：登录 QQ 后开启 OneBot v11，WebSocket 服务器监听 `127.0.0.1:3001`
- 可选 Node.js：仅 `backend: codebuddy_cli` 需要（走本机 CodeBuddy CLI）；用 `deepseek` 直连 API 则不需要

**可选依赖（本地向量检索，不装也能跑）**：

```bash
pip install onnxruntime tokenizers numpy
```

装了的收益：归档检索从「关键词匹配」升级为「语义匹配」（bge-small-zh-v1.5，
约 23MB，首次启动自动从 hf-mirror 下载，之后零联网、不调云端 API）。
**不装则自动降级为关键词检索**，其他功能完全不受影响。

## 快速开始

```bash
# 1. 复制示例配置并修改
cp qq_autoreply_config.example.json qq_autoreply_config.json
```

编辑 `qq_autoreply_config.json`：

| 字段 | 说明 |
| --- | --- |
| `allowed_private` | 允许自动回复的 QQ 号列表（私聊） |
| `allowed_groups` | 允许回复的群号列表（群里被 @ 才回） |
| `system_prompt` | 机器人设，示例配置里是一份 12 岁小女孩人设，随便改 |
| `codebuddy.cli` | CodeBuddy CLI 的 `bin/codebuddy` 路径（用 CLI 后端时必填） |
| `llm.api_key` | 直连 API 时的 Key（`api_key_source` 改成 `literal`） |

```bash
# 2. 启动守护进程（保持运行）
python qq_autoreply.py

# 3. 另开一个终端启动控制面板（可选）
python qq_panel.py
```

Windows 下也可以直接双击 `一键启动.bat`（会先拉起 NapCat 再启动守护进程）。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `qq_autoreply.py` | 主程序：OneBot 连接、事件调度、回复流程、模型链 |
| `qq_panel.py` | tkinter 控制面板（配置/日志/启停） |
| `boot_all.py` | 一键启动：NapCat + 守护进程 |
| `apply_onebot.py` | 自动给 NapCat 写入 OneBot v11 配置 |
| `qq_autoreply_config.json` | 本地配置（**已被 .gitignore 排除，不会上传**） |
| `qq_autoreply_config.example.json` | 配置模板 |
| `memory.py` | 记忆模块：三层记忆/关系档案/检索/writer 线程（主程序只经 recall/remember 两个钩子访问） |
| `memory_vector.py` | 本地向量检索（可选）：bge-small-zh ONNX，模型自动下载，缺依赖自动降级 |
| `tools/` | 回放标定与黄金回归测试（检索阈值标定、Prompt 拼装冻结、主动搭话回放） |
| `assets/replies/` | 表情包图库 + 场景标注 |
| `docs/` | 项目总结文档 |

## 隐私说明

- 聊天记录归档（`qq_messages/`）、登录数据、本地记忆、向量模型与缓存
  （`qq_messages/models/`、`vectors.npy`）一律**不入库**，已在 `.gitignore` 中排除
- 部署时请把仓库设为 **Private**，或至少确认配置文件里没有你不想公开的 QQ 号
- 记忆文件（摘要/档案）里会累积聊天内容，注意不要连带分享

## 已知边界

- 群归档是 append-only 的混合日志，无法按人删除单条记录
- 语音转写依赖 NapCat 的 `fetch_ptt_text`
- 表情包发送有冷却间隔（默认每 5–10 条回复才允许一张），防止刷屏
- **启动补回补不回「守护进程不在线时到达」的消息**——那些消息根本没进归档
  （NapCat 的 OneBot WS 不缓存离线事件）；它覆盖的是「到过守护进程但没回上」的
  （进程宕了/模型挂了/发送失败）。要救前者得让 QQ 本身重登录触发服务端同步，
  且守护进程已连上（详见 NapCat 行为，实测过）
