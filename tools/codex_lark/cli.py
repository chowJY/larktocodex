from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .config import init_config, load_config
from .lark_io import send_disconnect_messages
from .runner import run_bridge
from .state import clear_processed_message_ids


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
    notify_stop = sub.add_parser("notify-stop")
    notify_stop.add_argument("--project-name", default="")
    notify_stop.add_argument("--use-first-project", action="store_true")
    args = parser.parse_args()
    if args.command == "init":
        init_config(args.chat_id)
    elif args.command == "notify-stop":
        config = load_config(
            project_name=args.project_name,
            use_first_project=args.use_first_project,
        )
        send_disconnect_messages(config)
        clear_processed_message_ids(config)
    elif args.command == "run":
        asyncio.run(
            run_bridge(
                Path(args.workspace_root),
                project_name=args.project_name,
                use_first_project=args.use_first_project,
                codex_ws_url=args.codex_ws_url,
            )
        )
