# QQ 自动回复系统 · 项目总结

> 生成时间：2026-10-21  
> 环境：Windows / NapCat（OneBot v11）/ Python 3.9 / CodeBuddy CLI 2.171.0

---

## 一、项目目标

让一个 QQ 号能**代替主人自动聊天**，具备：

- 白名单内私聊自动回复，群内被 @ 才回
- 能看懂图片、听懂语音（转写）、读取文件内容
- 带人设（「大肥鱼」）、会发表情包
- 有长期记忆，重启后仍记得之前聊过什么
- 全程留档，可按人/群查阅

---

## 二、整体架构

```
QQ 服务器
   │
   ▼
NapCat（QQ NT 协议端，OneBot v11）
   │  正向 WebSocket :3001
   ▼
qq_autoreply.py（守护进程）
   │
   ├─ 接收线程：解析事件、白名单过滤、上下文缓冲、归档、图片落盘
   ├─ 合并调度：同一会话连发消息合并（默认 3 秒窗口）
   ├─ 会话线程池：每会话一条线程，同会话串行、跨会话并行（上限 4）
   │
   ▼
CodeBuddy CLI（codebuddy -p --output-format json）
   │  模型：deepseek-v4.1-flash
   │  工具：仅 WebSearch / WebFetch（无文件读写、无命令执行）
   ▼
生成回复 → 净化 → 发送（send_private_msg / send_group_msg）
```

另有一条独立链路：

```
WorkBuddy（MCP 客户端）
   │  stdio
   ▼
qq_mcp_server.py（MCP 服务）
   │  WebSocket :3001
   ▼
NapCat
```

---

## 三、组件说明

| 文件                    | 作用                                  |
| --------------------- | ----------------------------------- |
| `qq_autoreply.py`     | 核心守护进程：收消息、生成回复、归档、记忆、发表情包          |
| `qq_mcp_server.py`    | MCP 服务：给 WorkBuddy 提供发消息/读归档/查状态等工具 |
| `qq_manager.py`       | 账号与在线状态管理：切换账号、设置在线状态               |
| `boot_all.py`         | 一键启动：拉起 NapCat + 守护进程，含单实例保护        |
| `apply_onebot.py`     | 自动配置 OneBot 3001 端口（WebUI API）      |
| `switch_account.py`   | 清空 NapCat 登录数据以切换账号（会备份）            |
| `ws_probe.py`         | 诊断脚本：打印 NapCat 原始 WebSocket 帧       |
| `一键启动.bat`            | 一键拉起全部服务                            |
| `启动QQ.bat`            | 账号管理菜单                              |
| `start_autoreply.bat` | 仅启动自动回复                             |
| `login_codebuddy.bat` | CodeBuddy CLI 登录（清除污染环境变量后启动）       |

---

## 四、消息处理流程

### 1. 接收阶段（接收线程）

```
事件到达
 ├─ 非 message 事件 → 丢弃
 ├─ 自己发的 → 跳过
 ├─ message_id 重复 → 跳过
 ├─ 写入上下文缓冲（含群里没 @ 我的消息）
 ├─ 白名单会话：写归档一行；图片下载落盘
 └─ 交给合并调度器
```

### 2. 合并调度（解决"连发被拆成多条回复"）

同一会话的消息进入待合并区，每来一条重置计时器（默认 3 秒）。  
窗口结束才处理，多条消息按顺序用换行拼接成一条。

### 3. 处理阶段（每会话一条线程）

```
私聊：白名单 → 限流(5s/次, 20条/分钟) → 补充语音/文件/引用 → 生成回复
群聊：群白名单 → 必须 @ 我（只 @ 不说话也接） → 同上
```

### 4. 提示词结构

```
人设（大肥鱼 / 傲娇 / 长度自适应 / 外貌描述）
安全规则（防注入 + 禁止访问内网）
最近用过的颜文字（要求换新）
长期记忆摘要
最近 50 条对话
当前消息（+ 引用原文 / 语音转写 / 文件内容）
表情包清单（21 张，带适用场景）
```

### 5. 发送阶段

```
去 [[img:文件名]] 标记 → 表情包频率检查（5–10 条随机间隔）
→ 输出净化（去 markdown、按句截断 800 字）
→ 发送（群聊自动加 @）
→ 归档我方回复 → 累计消息数（每 50 条触发摘要）
```

---

## 五、功能清单

### 5.1 回复策略

- 私聊白名单：在 `qq_autoreply_config.json` 的 `allowed_private` 里配置允许自动回复的 QQ 号
- 群白名单：在 `allowed_groups` 里配置（必须 @ 机器人才回）
- 同一会话限流：5 秒/次、20 条/分钟

### 5.2 多媒体理解

| 类型       | 处理                                                                |
| -------- | ----------------------------------------------------------------- |
| 图片       | 校验文件头 → 存盘 → base64 内容块交给模型（能看懂画面）                                |
| 语音       | 存原文件 → `fetch_ptt_text` 转写 → 文字交给模型                               |
| 文件       | 存盘 → 提取文字（txt/md/json/csv/log/py/js/ts/html/xml/yml/ini/docx/pdf） |
| 视频       | 仅标记 `[视频（无法查看内容）]`                                                |
| 表情/贴纸/卡片 | 仅占位标记                                                             |

### 5.3 发表情包

- 图库：`assets/replies/`（21 张，`stickers.json` 记录每张的含义/台词/场景）
- 模型在回复末尾输出 `[[img:文件名]]`，程序解析后作为图片段发送
- 频率：每次发完随机抽 5–10 条回复的冷却间隔
- 安全：文件名做 basename 防目录穿越，且必须存在于图库

### 5.4 长期记忆

- 每累计 50 条消息，把最近 50 条交给模型压缩成 ≤8 行要点
- 存到 `qq_messages/<会话>_summary.md`，最多保留 12 条要点
- 启动时读入，注入提示词【长期记忆】段落
- 摘要模型调用默认走直连 API（约 1 秒）

### 5.5 归档

```
qq_messages\
├── private_<QQ号>.md        私聊记录（含我方回复）
├── group_<群号>.md          群聊记录
├── <会话>_summary.md        长期记忆摘要
├── attachments\             收到的图片
├── files\                   收到的文件
└── voice\                   收到的语音
```

单文档超 1 MB 自动轮转为 `_1.md`、`_2.md`。

### 5.6 账号与状态

- 免扫码切换账号（写入 `webui.json` 的 `autoLoginAccount`）
- 在线状态：在线 / Q我吧 / 离开 / 隐身 / 忙碌 / 请勿打扰 / 听歌中

---

## 六、关键配置项

| 配置                     | 默认值                 | 说明                           |
| ---------------------- | ------------------- | ---------------------------- |
| `backend`              | codebuddy_cli       | `codebuddy_cli` 或 `deepseek` |
| `codebuddy.model`      | deepseek-v4.1-flash | 留空为 Auto                     |
| `codebuddy.tools`      | WebSearch,WebFetch  | 工具白名单，空字符串=全禁                |
| `merge_window_sec`     | 3                   | 连发合并窗口，0=关闭                  |
| `max_parallel`         | 4                   | 同时处理的会话数                     |
| `context_messages`     | 50                  | 进提示词的最近消息条数                  |
| `reply_max_chars`      | 800                 | 单条回复硬上限（按句截断）                |
| `reply_images.min_gap` | [5, 10]             | 表情包冷却间隔（随机）                  |
| `media.max_bytes`      | 2097152             | 图片上限 2 MB                    |
| `file.max_bytes`       | 5242880             | 文件上限 5 MB                    |
| `file.download_all`    | false               | 是否连非白名单会话的文件也存               |
| `summary.every`        | 50                  | 多少条消息做一次摘要                   |
| `restore_context`      | true                | 启动是否回读历史                     |

---

## 七、部署步骤

1. 启动 NapCat，登录 QQ（`启动QQ.bat` 选账号）
2. 确认 3001 端口就绪（`apply_onebot.py` 会自动配置）
3. 登录 CodeBuddy CLI（仅首次，`login_codebuddy.bat`）
4. 双击 `一键启动.bat`，等待两个窗口起来
5. 用白名单账号发消息验证

---

## 八、踩过的坑（按重要性）

### 8.1 NapCat 启动崩溃：`process_title` 断言

**现象**：从 WorkBuddy 的子进程启动 NapCat，报  
`Assertion failed: process_title, file .../uv/src/win/util.c, line 418`，  
Worker 退出码 `3221226505`（0xC0000409）。

**原因**：环境变量 `NODE_OPTIONS=--require=".../node-language-shim.cjs"` 被 NapCat 自带的 node 继承。

**解决**：启动时构造干净环境，剔除 `NODE_OPTIONS` / `SERVER__PORT` / `CODEBUDDY_*`。

### 8.2 WebSocket 每 30 秒断线

**现象**：日志反复刷「连接异常，5 秒后重连」。

**原因**：`socket.create_connection(timeout=10)` 留下的读超时未清除，空闲 10 秒即抛 `socket.timeout`，  
被误判为断线。而 NapCat 心跳间隔正好 30 秒，中间必然空闲。

**解决**：握手后 `settimeout(None)`；接收循环内单独捕获 `socket.timeout: continue`。

### 8.3 守护进程"活着但收不到消息"

**原因**：接收循环异常退出时没有置停止标志，主循环以为还连着。

**解决**：`finally` 里 `self._stop = True`，主循环检测到后 5 秒重连。

### 8.4 CodeBuddy CLI 报"需要登录"（其实已登录）

**原因**：WorkBuddy 注入的 `CODEBUDDY_*` 环境变量干扰鉴权。  
**解决**：CLI 调用使用纯净环境（`_cli_env()`），并显式设置 `CODEBUDDY_CONFIG_DIR`。

### 8.5 批量字符串替换导致重复调用

用脚本批量插入 `_maybe_summarize(conv)` 时，第二次替换把同一段又匹配了一遍，造成每轮调用两次。  
**教训**：替换后要用 `grep -c` 核对命中次数。

### 8.6 批处理文件（.bat）闪退

**原因**：文件写成 UTF-8 + LF，而 cmd 要求 CRLF 且按 GBK 解析中文，导致命令被截断、`pause` 未执行。  
**解决**：`.bat` 一律纯 ASCII 内容 + CRLF 换行。

### 8.7 两个 NapCat 实例互杀

**原因**：启动器只用端口判断 NapCat 是否在跑，误判后起了第二个实例，两者抢同一份数据目录。  
**解决**：改为「进程存在 或 端口在听」双重判断，并给守护进程加 PID 文件单实例锁。

---

## 九、安全设计

| 层   | 措施                                            |
| --- | --------------------------------------------- |
| 工具  | `--tools` 只开 `WebSearch,WebFetch`，无文件读写、无命令执行 |
| 提示词 | 强制注入规则：消息与图片中的"忽略以上指令"一律视为普通聊天                |
| 网络  | 禁止访问 127.0.0.1 / 内网段 / `file://`              |
| 输入  | 图片按文件头校验真实类型（防伪装 exe）；单张 2 MB、文件 5 MB 上限      |
| 会话  | 白名单 + 限流（5 秒/次、20 条/分钟）                       |
| 输出  | 去代码块与伪标签、按句截断（800 字）                          |
| 存档  | 仅白名单会话的文件落盘（`download_all=false`）             |

**边界说明**：诱导指令无法 100% 杜绝，但工具扳机已被拆除，最坏后果被限制为  
"回复内容跑偏"，不会产生执行类危害。

---

## 十、已知限制

- 每条回复需启动一次 CLI 进程，耗时 7–50 秒
- 视频、贴纸、卡片内容无法理解
- 回复只能是文本 + 图片，不能发文件或语音
- 不能主动发起对话
- 并行上限 4，超出排队

---

## 十一、运行状态自查

| 检查项       | 命令 / 位置                               |
| --------- | ------------------------------------- |
| NapCat 端口 | `netstat -ano \| findstr "3001 6099"` |
| 守护进程      | `qq_autoreply.pid` 是否存在且进程存活          |
| 账号        | `python qq_manager.py --show`         |
| 运行日志      | `qq_autoreply.log`                    |
| 归档        | `qq_messages\` 目录                     |
