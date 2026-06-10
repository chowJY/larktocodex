# LarkToCodex

Windows bridge for forwarding approved Lark/Feishu chat messages into a Codex `app-server` session, then replying back to Lark.

The bridge is designed for one or more configured projects. Each project owns its own Lark app credentials, allowed chat, Codex websocket port, runtime state, and logs.

## Structure

- `codex-lark.cmd` / `codex-lark.ps1`: root entrypoints.
- `lark-codex.cmd` / `lark-codex.ps1`: alias entrypoints.
- `tools/codex-lark.ps1`: operational PowerShell launcher for `init`, `start`, `start-all`, `stop`, `stop-all`, `restart`, and status commands.
- `tools/codex_lark_bridge.py`: thin Python CLI wrapper.
- `tools/codex_lark/`: Python implementation split by responsibility.
  - `config.py`: config loading and initialization.
  - `state.py`: bridge state, processed message ids, process state, and instance locks.
  - `processes.py`: Codex app-server and Lark event consumer subprocess management.
  - `lark_io.py`: Lark event parsing and replies.
  - `codex_client.py`: Codex websocket client and turn lifecycle.
  - `codex_events.py`: Codex event extraction and formatting.
  - `approvals.py`: permission request registration, dedupe, parsing, and response payloads.
  - `runner.py`: bridge coordinator, message queue, turn session state, and shutdown handling.
- `.env`: local single-project credentials. Not committed.
- `.lark-events/`: local runtime state, project config, websocket tokens, and logs. Not committed.

## Setup

Run from the repository root:

```powershell
.\codex-lark.ps1 init -ChatId oc_REPLACE_ME
```

Fill `.env` for single-project use, or edit `.lark-events/projects-config.json` for multi-project use.

Required project fields:

- `name`
- `app_id`
- `app_secret`
- `chat_id` or `chat_ids`
- `workspace_root`
- `codex_ws_url`, usually `auto`
- `codex_token_file`
- `lark_cli`
- `reply_mode`
- `event_reply_mode`
- `event_max_age_seconds`, default `120`
- `turn_timeout_seconds`

Do not commit `.env`, `.lark-events/`, chat ids, tokens, or logs.

## Commands

Start one bridge:

```powershell
.\codex-lark.ps1 start
```

Start a named project:

```powershell
.\codex-lark.ps1 start -ProjectName xunji
```

Start every configured project:

```powershell
.\codex-lark.ps1 start-all
```

Check status:

```powershell
.\codex-lark.ps1 status
.\codex-lark.ps1 status-all
```

Stop bridges:

```powershell
.\codex-lark.ps1 stop
.\codex-lark.ps1 stop-all
```

Before stopping, the launcher sends `<project> 链接断开` to the project's configured chat and clears that project's `processed-messages.json`.

If the current shell blocks PowerShell scripts, use:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\codex-lark.ps1 status-all
```

## Runtime Behavior

The Lark consumer reads `im.message.receive_v1` events as the configured bot. Messages are accepted only from configured chat ids and are ignored if sent by the bot itself.

Each accepted Lark message is forwarded to the Codex thread for the project's `workspace_root`. The bridge streams Codex messages back according to `reply_mode` and can emit summary event replies according to `event_reply_mode`.

On startup, the bridge clears `processed-messages.json` and ignores Lark events whose message creation time is older than the startup window configured by `event_max_age_seconds`. This prevents old events replayed by the Lark consumer after restart from being processed as new requests.

Processed message ids are written under a small file lock, so overlapping consumers cannot accept the same `message_id` at the same time.

Permission requests are assigned visible request numbers, for example:

```text
Codex 请求权限 #1
...
回复“同意 #1”允许，或回复“拒绝 #1”拒绝。
```

When multiple approvals are pending, unnumbered approval replies are rejected with a prompt to specify the request number. This prevents approving or rejecting the wrong request.

## Stability Notes

The bridge separates three states that previously conflicted:

- final answer sent to Lark
- Codex turn actually completed
- permission request pending or resolved

When Codex emits a final answer, the bridge releases the user-facing turn gate and can accept follow-up messages without replying that the previous request is still processing. The underlying Codex turn still finishes normally in the background.

If the Lark event consumer exits, the main bridge now shuts down and releases the project lock. `stop` and `stop-all` can also scan for project processes when pid state is missing, which helps clean old half-stopped bridges.

Each bridge start rotates previous runtime logs before opening new ones:

- `bridge.log` becomes `bridge_back_<last-modified-time>.log`.
- `lark-replies.log` becomes `lark-replies_backup_<last-modified-time>.log`.

The current `bridge.log` and `lark-replies.log` then contain only output from the new bridge run.

## Verification

Syntax check:

```powershell
$files = @('tools\codex_lark_bridge.py') + @(Get-ChildItem tools\codex_lark -Filter *.py | ForEach-Object { $_.FullName })
python -m py_compile @files
```

Lifecycle check:

```powershell
.\codex-lark.ps1 stop-all
.\codex-lark.ps1 start-all
.\codex-lark.ps1 status-all
```

Inspect logs under `.lark-events/projects/<project>/bridge.log` and `.lark-events/projects/<project>/lark-replies.log`.
