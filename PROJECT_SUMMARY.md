# Project Summary

## Purpose

`LarkToCodex` bridges selected Lark/Feishu chats to Codex `app-server` sessions. It lets a Lark bot receive user messages, forward them to Codex in a configured workspace, stream replies back to Lark, and handle Codex permission approvals from chat.

## Current Architecture

The original single Python implementation has been split into a small package under `tools/codex_lark/`:

- `cli.py` keeps command parsing small.
- `config.py` handles `.env` and project config loading.
- `state.py` handles JSON state files, processed message ids, pid state, and bridge locks.
- `processes.py` starts Codex and Lark subprocesses and logs their output.
- `lark_io.py` parses Lark events and sends replies.
- `codex_client.py` owns the websocket connection, request/response futures, active turn routing, and approval resolution.
- `codex_events.py` extracts agent messages, summary events, command events, and approval payloads.
- `approvals.py` deduplicates approval requests, assigns visible request ids, parses approval replies, and builds Codex response payloads.
- `runner.py` coordinates message intake, turn sessions, queueing, approval replies, and shutdown.

`tools/codex_lark_bridge.py` remains as the stable script path used by PowerShell launchers, but it now only imports and calls `codex_lark.cli.main()`.

## Completed Fixes

- Fixed stale "Codex 正在处理上一条需求" responses by separating final-answer delivery from Codex turn completion.
- Added `finally` cleanup for active Codex turns.
- Added per-session reply dedupe.
- Added approval request registry with visible request numbers.
- Added approval alias dedupe across RPC id, item id, approval id, and fallback command keys.
- Required numbered replies when multiple approvals are pending.
- Fixed command approval response payloads to use the Codex app-server `decision` envelope.
- Fixed event-consumer shutdown so bridge locks are released when the Lark consumer exits.
- Improved `stop`, `stop-all`, and `status-all` behavior for multi-project operation and stale process cleanup.
- Split the implementation by feature area and removed duplicated logic from the old monolithic bridge file.

## Verification Performed

- Python syntax compilation for `tools/codex_lark_bridge.py` and all `tools/codex_lark/*.py` files.
- Local approval parser checks for approve and reject phrases with request numbers.
- Local approval response checks for command execution approvals:
  - approve with exec policy amendment returns a `decision.acceptWithExecpolicyAmendment` payload.
  - reject returns `decision: cancel`.
- Multi-project lifecycle check with:
  - `stop-all`
  - `start-all`
  - `status-all`
- User-side functional verification for Lark replies, approval flow, duplicate approval handling, and `start-all`.

## Operational Notes

Runtime files and credentials remain local:

- `.env`
- `.lark-events/`
- websocket token files
- bridge logs
- Lark chat ids

These are intentionally excluded by `.gitignore`.

