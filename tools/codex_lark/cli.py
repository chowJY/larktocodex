from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .config import init_config
from .runner import run_bridge


def main() -> None:
    parser = argparse.ArgumentParser(description="Bridge allowed Lark chats to a Codex app-server thread.")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--chat-id")
    sub.add_parser("run")
    run = sub.choices["run"]
    run.add_argument("--workspace-root", default=str(Path.cwd()))
    run.add_argument("--project-name", default="")
    run.add_argument("--use-first-project", action="store_true")
    run.add_argument("--codex-ws-url", default="")
    args = parser.parse_args()
    if args.command == "init":
        init_config(args.chat_id)
    elif args.command == "run":
        asyncio.run(
            run_bridge(
                Path(args.workspace_root),
                project_name=args.project_name,
                use_first_project=args.use_first_project,
                codex_ws_url=args.codex_ws_url,
            )
        )
