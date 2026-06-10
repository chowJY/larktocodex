from __future__ import annotations

import asyncio
import logging
import secrets
import subprocess
from pathlib import Path
from typing import Any

from .config import BridgeConfig, lark_env
from .paths import BRIDGE_ROOT


def ensure_token_file(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def start_codex_app_server(config: BridgeConfig, workspace_root: Path) -> subprocess.Popen[str]:
    ensure_token_file(config.codex_token_file)
    args = [
        "codex.cmd",
        "app-server",
        "--listen",
        config.codex_ws_url,
        "--ws-auth",
        "capability-token",
        "--ws-token-file",
        str(config.codex_token_file),
    ]
    logging.info("starting codex app-server cwd=%s", workspace_root)
    return subprocess.Popen(
        args,
        cwd=workspace_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def start_lark_consumer(config: BridgeConfig) -> subprocess.Popen[str]:
    args = [config.lark_cli]
    if config.lark_profile:
        args.extend(["--profile", config.lark_profile])
    args.extend([
        "event",
        "consume",
        "im.message.receive_v1",
        "--as",
        "bot",
    ])
    logging.info("starting lark event consumer")
    return subprocess.Popen(
        args,
        cwd=config.state_dir if config.is_project else BRIDGE_ROOT,
        env=lark_env(config),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )


async def pipe_process_output(name: str, stream: Any) -> None:
    if stream is None:
        return
    while True:
        line = await asyncio.to_thread(stream.readline)
        if not line:
            return
        logging.info("[%s] %s", name, line.rstrip())


def log_task_done(name: str) -> Any:
    def _log_done(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            logging.info("%s stopped", name)
            return
        exc = task.exception()
        if exc:
            logging.error("%s failed", name, exc_info=(type(exc), exc, exc.__traceback__))
        else:
            logging.info("%s stopped", name)

    return _log_done
