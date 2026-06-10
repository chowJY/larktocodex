from __future__ import annotations

import json
import logging
import subprocess
import time
from typing import Any

from .config import BridgeConfig, lark_env
from .paths import BRIDGE_ROOT


def append_lark_reply_log(
    config: BridgeConfig,
    kind: str,
    text: str,
    message_id: str = "",
    chat_id: str = "",
    truncated: bool = False,
) -> None:
    config.reply_log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "project": config.name,
        "kind": kind,
        "message_id": message_id,
        "chat_id": chat_id,
        "chars": len(text),
        "truncated": truncated,
        "text": text,
    }
    with config.reply_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def normalize_event(raw: str) -> dict[str, Any] | None:
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        logging.warning("failed to decode lark event: %s", raw.rstrip())
        return None
    return event


def extract_message_content(event: dict[str, Any]) -> str:
    content = event.get("content")
    if isinstance(content, str):
        raw_content = content.strip()
        if not raw_content:
            return ""
        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError:
            return raw_content
        content = parsed
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"].strip()
        post = content.get("post")
        if isinstance(post, dict):
            parts: list[str] = []
            for locale_data in post.values():
                if not isinstance(locale_data, dict):
                    continue
                for line in locale_data.get("content") or []:
                    if not isinstance(line, list):
                        continue
                    for item in line:
                        if isinstance(item, dict) and isinstance(item.get("text"), str):
                            parts.append(item["text"])
            return "".join(parts).strip()
    return str(content or "").strip()


def is_from_self_bot(event: dict[str, Any], config: BridgeConfig) -> bool:
    sender = event.get("sender")
    if not isinstance(sender, dict):
        return False
    sender_type = str(sender.get("sender_type") or sender.get("type") or "").lower()
    if sender_type in {"app", "bot"}:
        return True
    sender_id = sender.get("sender_id")
    if isinstance(sender_id, dict) and config.lark_app_id:
        app_id = str(sender_id.get("app_id") or "")
        if app_id == config.lark_app_id:
            return True
    return False


def should_handle(event: dict[str, Any], config: BridgeConfig) -> tuple[bool, str]:
    chat_id = str(event.get("chat_id") or "")
    if chat_id not in config.allowed_chat_ids:
        logging.info("ignored message from unallowed chat_id=%s message_id=%s", chat_id, event.get("message_id"))
        return False, ""
    if is_from_self_bot(event, config):
        logging.info("ignored self bot message chat_id=%s message_id=%s", chat_id, event.get("message_id"))
        return False, ""
    if str(event.get("message_type") or "") not in {"text", "post"}:
        logging.info(
            "ignored non-text message chat_id=%s message_id=%s message_type=%s",
            chat_id,
            event.get("message_id"),
            event.get("message_type"),
        )
        return False, ""
    content = extract_message_content(event)
    if not content:
        logging.info("ignored empty message chat_id=%s message_id=%s", chat_id, event.get("message_id"))
        return False, ""
    if config.command_prefix:
        if not content.startswith(config.command_prefix):
            logging.info("ignored message without prefix chat_id=%s message_id=%s", chat_id, event.get("message_id"))
            return False, ""
        content = content[len(config.command_prefix) :].strip()
    return bool(content), content


def reply_to_lark(config: BridgeConfig, event: dict[str, Any], text: str) -> None:
    message_id = str(event.get("message_id") or event.get("id") or "")
    if not message_id:
        logging.warning("skip reply: event has no message_id")
        return
    reply_text = text[:8000]
    append_lark_reply_log(
        config,
        "reply",
        reply_text,
        message_id=message_id,
        chat_id=str(event.get("chat_id") or ""),
        truncated=len(text) > len(reply_text),
    )
    content_json = json.dumps({"text": reply_text}, ensure_ascii=False)
    args = [config.lark_cli]
    if config.lark_profile:
        args.extend(["--profile", config.lark_profile])
    args.extend([
        "im",
        "+messages-reply",
        "--message-id",
        message_id,
        "--msg-type",
        "text",
        "--content",
        content_json,
        "--as",
        "bot",
    ])
    result = subprocess.run(
        args,
        cwd=config.state_dir if config.is_project else BRIDGE_ROOT,
        env=lark_env(config),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        logging.error("lark reply failed rc=%s stderr=%s stdout=%s", result.returncode, result.stderr, result.stdout)
    else:
        logging.info("replied to lark message_id=%s reply_chars=%s truncated=%s", message_id, len(reply_text), len(text) > len(reply_text))


def send_lark_text(config: BridgeConfig, chat_id: str, text: str) -> None:
    message_text = text[:8000]
    append_lark_reply_log(
        config,
        "send",
        message_text,
        chat_id=chat_id,
        truncated=len(text) > len(message_text),
    )
    args = [config.lark_cli]
    if config.lark_profile:
        args.extend(["--profile", config.lark_profile])
    args.extend([
        "im",
        "+messages-send",
        "--chat-id",
        chat_id,
        "--text",
        message_text,
        "--as",
        "bot",
    ])
    result = subprocess.run(
        args,
        cwd=config.state_dir if config.is_project else BRIDGE_ROOT,
        env=lark_env(config),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        logging.error("lark startup message failed chat_id=%s rc=%s stderr=%s stdout=%s", chat_id, result.returncode, result.stderr, result.stdout)
    else:
        logging.info("sent lark startup message chat_id=%s chars=%s truncated=%s", chat_id, len(message_text), len(text) > len(message_text))


def send_startup_messages(config: BridgeConfig) -> None:
    for chat_id in sorted(config.allowed_chat_ids):
        send_lark_text(config, chat_id, config.name)
