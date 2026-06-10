from __future__ import annotations

import json
import logging
import msvcrt
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import BridgeConfig


def load_state(config: BridgeConfig) -> dict[str, Any]:
    if not config.state_path.exists():
        return {}
    return json.loads(config.state_path.read_text(encoding="utf-8-sig"))


def save_state(config: BridgeConfig, state: dict[str, Any]) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def processed_messages_path(config: BridgeConfig) -> Path:
    return config.state_dir / "processed-messages.json"


def clear_processed_message_ids(config: BridgeConfig) -> None:
    path = processed_messages_path(config)
    lock_path = config.state_dir / "processed-messages.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            path.write_text("[]\n", encoding="utf-8")
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def load_processed_message_ids(config: BridgeConfig) -> set[str]:
    path = processed_messages_path(config)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        logging.exception("failed to read processed message ids path=%s", path)
        return set()
    if isinstance(data, list):
        return {str(item) for item in data if item}
    return set()


def save_processed_message_ids(config: BridgeConfig, message_ids: set[str]) -> None:
    path = processed_messages_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    recent = sorted(message_ids)[-1000:]
    path.write_text(json.dumps(recent, ensure_ascii=False, indent=2), encoding="utf-8")


def _backup_path(path: Path, backup_stem: str) -> Path:
    modified_at = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d_%H%M%S")
    candidate = path.with_name(f"{backup_stem}_{modified_at}{path.suffix}")
    index = 1
    while candidate.exists():
        candidate = path.with_name(f"{backup_stem}_{modified_at}_{index}{path.suffix}")
        index += 1
    return candidate


def backup_and_clear_runtime_logs(config: BridgeConfig) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    for path, backup_stem in (
        (config.log_path, "bridge_back"),
        (config.reply_log_path, "lark-replies_backup"),
    ):
        if not path.exists():
            continue
        path.replace(_backup_path(path, backup_stem))


def try_mark_message_processed(config: BridgeConfig, message_id: str) -> bool:
    if not message_id:
        return True
    path = processed_messages_path(config)
    lock_path = config.state_dir / "processed-messages.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            records: list[str] = []
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8-sig"))
                    if isinstance(data, list):
                        records = [str(item) for item in data if item]
                except Exception:
                    logging.exception("failed to read processed message ids path=%s", path)
                    records = []
            if message_id in set(records):
                return False
            records.append(message_id)
            path.write_text(
                json.dumps(records[-1000:], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return True
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def acquire_instance_lock(config: BridgeConfig) -> Any:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = config.state_dir / "bridge.lock"
    handle = lock_path.open("a+b")
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        raise RuntimeError(f"another codex-lark bridge is already running for project {config.name}")
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()).encode("ascii", errors="ignore"))
    handle.flush()
    logging.info("acquired bridge instance lock path=%s", lock_path)
    return handle


def release_instance_lock(handle: Any | None) -> None:
    if not handle:
        return
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def update_process_state(
    config: BridgeConfig,
    workspace_root: Path,
    codex_pid: int | None,
    lark_pid: int | None,
) -> None:
    state = load_state(config)
    state["project_name"] = config.name
    state["workspace_root"] = str(workspace_root)
    state["codex_ws_url"] = config.codex_ws_url
    state["allowed_chat_ids"] = sorted(config.allowed_chat_ids)
    state["processes"] = {
        "bridge": os.getpid(),
        "codex": codex_pid,
        "lark": lark_pid,
    }
    save_state(config, state)


def clear_process_state(config: BridgeConfig) -> None:
    state = load_state(config)
    if "processes" in state or "workspace_root" in state:
        state.pop("processes", None)
        state.pop("workspace_root", None)
        save_state(config, state)
