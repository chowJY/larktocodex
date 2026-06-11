# LarkToCodex 中文说明

`LarkToCodex` 是一个运行在 Windows 上的桥接工具，用于把指定飞书 / Lark 群聊中的消息转发到 Codex `app-server` 会话，并把 Codex 的回复发送回飞书。

它通过 `.lark-events/projects-config.json` 配置项目。每个项目都有独立的飞书应用凭据、允许的聊天、Codex websocket 端口、运行状态和日志。

## 目录结构

- `codex-lark.cmd` / `codex-lark.ps1`：仓库根目录入口。
- `lark-codex.cmd` / `lark-codex.ps1`：等价别名入口。
- `tools/codex-lark.ps1`：主要 PowerShell 管理脚本，支持 `init`、`start`、`start-all`、`stop`、`stop-all`、`restart`、`status` 等命令。
- `tools/codex_lark_bridge.py`：稳定的 Python CLI 入口，仅负责转发到包内实现。
- `tools/codex_lark/`：按职责拆分后的 Python 实现。
  - `config.py`：读取和初始化配置。
  - `state.py`：运行状态、已处理消息、进程状态和实例锁。
  - `processes.py`：启动 Codex app-server 和飞书事件消费者。
  - `lark_io.py`：解析飞书事件并发送回复。
  - `codex_client.py`：Codex websocket 客户端、turn 生命周期和审批响应。
  - `codex_events.py`：提取 Codex 消息、命令事件、摘要事件和审批事件。
  - `approvals.py`：审批请求注册、去重、编号、回复解析和响应载荷构造。
  - `runner.py`：主协调器，负责消息队列、turn session、审批回复和关闭流程。
- `.lark-events/`：本地运行状态、项目配置、websocket token 和日志目录，不提交。
  - `projects-config.json`：唯一支持的配置文件。
  - `projects/<project>/`：项目级运行状态、日志和 websocket token。

## 初始化

在仓库根目录运行：

```powershell
.\codex-lark.ps1 init -ChatId oc_REPLACE_ME
```

初始化后，编辑 `.lark-events/projects-config.json`。

旧的单项目 `.env` 和 `.lark-events/bridge-config.json` 已不再支持。

项目配置通常包含：

- `projects` 是字典，键为项目 `workspace_root`
- 每个项目值内包含 `name`
- `app_id`
- `app_secret`
- `chat_id` 或 `chat_ids`
- `codex_ws_url`，通常使用 `auto`
- `codex_token_file`
- `lark_cli`
- `reply_mode`
- `event_reply_mode`
- `event_max_age_seconds`，默认 `120`
- `turn_timeout_seconds`

不要提交 `.lark-events/`、真实 chat id、token、日志或应用密钥。

多项目配置示例：

```json
{
  "projects": {
    "<project-path>": {
      "name": "xunji",
      "app_id": "cli_REPLACE_ME",
      "app_secret": "REPLACE_ME",
      "chat_id": "oc_REPLACE_ME",
      "codex_ws_url": "auto",
      "codex_token_file": ".lark-events/projects/xunji/codex-ws-token.txt",
      "lark_cli": "lark-cli.cmd",
      "reply_mode": "all",
      "event_reply_mode": "all",
      "event_max_age_seconds": 120,
      "turn_timeout_seconds": 900
    }
  }
}
```

## 常用命令

启动默认桥接：

```powershell
.\codex-lark.ps1 start
```

如果当前目录匹配某个配置的 `workspace_root`，`start` 会自动选择该项目启动。

启动指定项目：

```powershell
.\codex-lark.ps1 start -ProjectName xunji
.\codex-lark.ps1 start xunji
```

启动所有项目：

```powershell
.\codex-lark.ps1 start-all
```

查看状态：

```powershell
.\codex-lark.ps1 status
.\codex-lark.ps1 status xunji
.\codex-lark.ps1 status-all
```

停止桥接：

```powershell
.\codex-lark.ps1 stop
.\codex-lark.ps1 stop xunji
.\codex-lark.ps1 stop-all
```

停止前，启动器会先向项目配置的群聊发送 `<project> 链接断开`，然后清空该项目的 `processed-messages.json`。

如果当前 PowerShell 执行策略阻止脚本运行，可使用：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\codex-lark.ps1 status-all
```

## 运行机制

桥接进程会以配置的飞书机器人身份消费 `im.message.receive_v1` 事件。只有配置中允许的聊天会被处理，机器人自己发出的消息会被忽略。

收到有效消息后，桥接会把文本发送到对应项目工作目录的 Codex thread。Codex 的回复会根据 `reply_mode` 流式或最终回复到飞书；命令、编辑和审批等事件可根据 `event_reply_mode` 发送摘要。

每次启动时，桥接会清空 `processed-messages.json`，并根据 `event_max_age_seconds` 忽略创建时间早于启动窗口的飞书消息事件，防止重启后事件消费者回放旧消息并被当作新需求处理。

已处理消息 ID 的写入带文件锁，即使存在短暂的重复消费者，也不会同时接受同一个 `message_id`。

审批请求会分配可见编号，例如：

```text
Codex 请求权限 #1
Command: ...
回复“同意 #1”允许，或回复“拒绝 #1”拒绝。
```

当同时存在多个待审批请求时，未带编号的“同意”或“拒绝”不会被猜测匹配，桥接会提示用户指定编号，避免处理错误请求。

## 稳定性设计

桥接层把下面几种状态分开处理：

- final answer 已发送到飞书
- Codex turn 实际完成
- 权限请求待处理或已处理

当 Codex 发出 final answer 后，桥接会释放用户侧输入门闩，后续消息不会再因为后台 turn 收尾而收到“Codex 正在处理上一条需求”的误提示。

如果飞书事件消费者退出，主 bridge 会跟随关闭并释放项目锁。`stop` 和 `stop-all` 在 pid state 丢失时也会尝试按项目名和工作目录扫描旧进程，清理半停止状态。

每个项目启动时，会在打开新日志前轮转旧日志：

- `.lark-events/projects/<project>/bridge.log` 会备份为 `bridge_back_<最后修改时间>.log`
- `.lark-events/projects/<project>/lark-replies.log` 会备份为 `lark-replies_backup_<最后修改时间>.log`

新的项目 `bridge.log` 和 `lark-replies.log` 只包含本次启动后的内容。

## 审批处理

审批模块会对同一个权限请求建立多个去重 key，包括：

- RPC request id
- item id
- approval id
- thread / turn / command fallback key

这样即使 Codex app-server 以不同事件形态重复发送同一审批请求，也会合并到同一个可见编号。

命令执行审批会按 Codex app-server 需要的 `decision` 结构响应。例如允许带执行策略修正的命令：

```json
{
  "decision": {
    "acceptWithExecpolicyAmendment": {
      "execpolicy_amendment": ["Get-Date"]
    }
  }
}
```

拒绝审批：

```json
{
  "decision": "cancel"
}
```

## 验证方式

Python 语法检查：

```powershell
$files = @('tools\codex_lark_bridge.py') + @(Get-ChildItem tools\codex_lark -Filter *.py | ForEach-Object { $_.FullName })
python -m py_compile @files
```

生命周期检查：

```powershell
.\codex-lark.ps1 stop-all
.\codex-lark.ps1 start-all
.\codex-lark.ps1 status-all
```

日志位置：

```text
.lark-events/projects/<project>/bridge.log
.lark-events/projects/<project>/lark-replies.log
```

这些日志属于本地运行数据，不应提交到 Git。

## 安全注意事项

以下内容必须只保存在本地：

- `.lark-events/`
- 飞书应用密钥
- 真实 chat id
- Codex websocket token
- bridge 日志
- Lark 回复日志
