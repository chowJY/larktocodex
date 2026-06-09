# Repository Guidelines

## Project Structure & Module Organization

This repository contains a small bridge for forwarding allowed Lark messages into a Codex app-server session.

- `codex-lark.cmd`: Windows CMD entrypoint that invokes PowerShell.
- `codex-lark.ps1`: root launcher; forwards `init`, `start`, and `stop` to the tool script.
- `tools/codex-lark.ps1`: operational PowerShell launcher for starting, stopping, and initializing the bridge.
- `tools/codex_lark_bridge.py`: main Python implementation for config loading, Lark event consumption, Codex websocket calls, and replies.
- `.env`: local Lark credentials and allowed chat IDs. Copy from `.env.example`.
- `.lark-events/`: local runtime state, token, and logs. Treat this as machine-local data.

## Build, Test, and Development Commands

There is no build step. Run commands from the repository root.

```powershell
.\codex-lark.ps1 init -ChatId oc_REPLACE_ME
```

Creates `.env` and `.lark-events/bridge-config.json`. Fill `LARK_APP_ID`, `LARK_APP_SECRET`, and `LARK_CHAT_IDS` in `.env` before starting.

```powershell
.\codex-lark.ps1 start
```

Starts the Codex app-server and Lark event consumer for the current workspace.

```powershell
.\codex-lark.ps1 stop
```

Stops bridge-related local processes.

For quick syntax checks:

```powershell
python -m py_compile tools\codex_lark_bridge.py
```

## Coding Style & Naming Conventions

Python code uses 4-space indentation, type hints, `dataclass` for structured config, and `pathlib.Path` for filesystem paths. Keep functions focused and prefer explicit error messages with actionable context. Use `snake_case` for Python functions and variables, and `PascalCase` only for classes.

PowerShell scripts use approved verbs where practical, `$PascalCase` variable names, and `$ErrorActionPreference = "Stop"` for predictable failures. Keep root scripts thin and place operational logic under `tools/`.

## Testing Guidelines

No formal test suite exists yet. At minimum, run `python -m py_compile tools\codex_lark_bridge.py` after Python edits. For behavior changes, test the lifecycle manually with `init`, `start`, and `stop`, and inspect `.lark-events/bridge.log`.

If adding tests, prefer `pytest` under `tests/`, with files named `test_*.py`. Mock subprocesses, websocket clients, and Lark event input instead of requiring real network services.

## Commit & Pull Request Guidelines

This directory currently has no Git history, so no local commit convention can be inferred. Use concise imperative commit messages such as `Add bridge config validation` or `Fix Codex turn fallback`.

Pull requests should include the purpose, commands run, manual verification notes, and any config or operational impact. Include logs or screenshots only when they clarify bridge startup, Lark replies, or failure handling.

## Security & Configuration Tips

Do not commit `.env`, `.lark-events/`, token files, logs, or chat IDs. Keep `LARK_CHAT_IDS` narrow, using comma-separated values for multiple chats. Avoid logging message contents beyond what is needed for debugging.
