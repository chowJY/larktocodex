from __future__ import annotations

import json
from typing import Any

from .config import BridgeConfig


def is_thread_not_found(exc: Exception) -> bool:
    return "thread not found" in str(exc).lower()


def extract_final_answer(turn: dict[str, Any]) -> str:
    messages = [
        str(item.get("text") or "").strip()
        for item in turn.get("items", [])
        if item.get("type") == "agentMessage"
    ]
    final_messages = [
        str(item.get("text") or "").strip()
        for item in turn.get("items", [])
        if item.get("type") == "agentMessage" and item.get("phase") == "final_answer"
    ]
    selected = [message for message in (final_messages or messages) if message]
    return "\n\n".join(selected).strip()


def extract_reply_answer(turn: dict[str, Any], all_messages: bool) -> str:
    if not all_messages:
        return extract_final_answer(turn)
    messages = [
        str(item.get("text") or "").strip()
        for item in turn.get("items", [])
        if item.get("type") == "agentMessage"
    ]
    return "\n\n".join(message for message in messages if message).strip()


def wants_all_codex_messages(config: BridgeConfig) -> bool:
    return config.reply_mode.strip().lower() in {"all", "stream", "messages", "agent_messages"}


def wants_summary_events(config: BridgeConfig) -> bool:
    return config.event_reply_mode.strip().lower() in {"summary", "summaries"}


def extract_thread_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("threadId", "thread_id"):
            value = payload.get(key)
            if value:
                return str(value)
        for value in payload.values():
            thread_id = extract_thread_id(value)
            if thread_id:
                return thread_id
    elif isinstance(payload, list):
        for value in payload:
            thread_id = extract_thread_id(value)
            if thread_id:
                return thread_id
    return ""


def extract_agent_messages(payload: Any) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        if payload.get("type") == "agentMessage" and str(payload.get("text") or "").strip():
            messages.append(payload)
        for value in payload.values():
            messages.extend(extract_agent_messages(value))
    elif isinstance(payload, list):
        for value in payload:
            messages.extend(extract_agent_messages(value))
    return messages


def message_markers(message: dict[str, Any]) -> set[str]:
    markers: set[str] = set()
    message_id = message.get("id")
    if message_id:
        markers.add(f"id:{message_id}")
    text = str(message.get("text") or "").strip()
    phase = str(message.get("phase") or "")
    if text:
        markers.add(f"text:{text}")
        markers.add(f"text:{phase}:{text}")
    return markers


def event_markers(event: dict[str, Any]) -> set[str]:
    markers: set[str] = set()
    for key in ("id", "callId", "call_id", "approval_id", "approvalId", "itemId", "item_id"):
        value = event.get(key)
        if value:
            markers.add(f"{event.get('summary_type')}:{key}:{value}")
    text = str(event.get("text") or "").strip()
    if text:
        markers.add(f"{event.get('summary_type')}:text:{text}")
    return markers or {json.dumps(event, ensure_ascii=False, sort_keys=True)[:1000]}


def walk_dicts(payload: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        items.append(payload)
        for value in payload.values():
            items.extend(walk_dicts(value))
    elif isinstance(payload, list):
        for value in payload:
            items.extend(walk_dicts(value))
    return items


def compact_text(value: Any, limit: int = 220) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        text = " ".join(str(item) for item in value)
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def first_value(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def format_summary_event(event: dict[str, Any]) -> str:
    kind = str(event.get("summary_type") or "")
    if kind == "approval":
        return str(event.get("text") or "")
    if kind == "command_start":
        return f"Running: {compact_text(event.get('command'))}"
    if kind == "command_finish":
        command = compact_text(event.get("command"))
        status = compact_text(event.get("status") or event.get("exit_code") or "")
        return f"Ran: {command}{(' -> ' + status) if status else ''}"
    if kind == "edit":
        path = compact_text(event.get("path") or event.get("file") or event.get("title"))
        return f"Edited: {path}" if path else "Edited files"
    if kind == "tool":
        title = compact_text(event.get("title") or event.get("tool") or event.get("type"))
        status = compact_text(event.get("status") or "")
        return f"{title}{(' -> ' + status) if status else ''}"
    return compact_text(event)


def is_approval_payload(item: dict[str, Any]) -> bool:
    method = str(item.get("method") or "")
    item_type = str(item.get("type") or item.get("kind") or item.get("name") or "")
    item_type_lower = item_type.lower()
    return (
        method in {
            "commandExecution/requestApproval",
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
        }
        or item_type in {"request_permissions", "RequestPermissions", "requestPermissions"}
        or item_type.endswith("RequestPermissions")
        or item_type.endswith("request_permissions")
        or item_type_lower in {
            "exec_approval_request",
            "apply_patch_approval_request",
            "mcp_tool_call_approval",
            "approval_request",
        }
        or item_type_lower.endswith("_approval_request")
        or item_type_lower.endswith("approvalrequest")
    )


def extract_summary_events(payload: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for item in walk_dicts(payload):
        if is_approval_payload(item):
            events.append({"summary_type": "approval", "payload": item})
            continue
        item_type = str(item.get("type") or item.get("kind") or item.get("method") or "")
        status = first_value(item, ("status", "state", "phase"))
        command = first_value(item, ("command", "cmd", "parsedCmd", "parsed_cmd", "argv"))
        tool_name = first_value(item, ("tool", "toolName", "tool_name", "name", "title"))
        path = first_value(item, ("path", "file", "absolute_file_path", "target_file"))
        if item_type == "agentMessage":
            continue
        item_type_lower = item_type.lower()
        if command and any(token in item_type_lower for token in ("exec", "command", "shell", "process", "tool")):
            events.append({
                "summary_type": "command_finish" if str(status).lower() in {"completed", "failed", "cancelled", "canceled", "success", "error"} else "command_start",
                "command": command,
                "status": status,
                "exit_code": first_value(item, ("exit_code", "exitCode")),
                "id": first_value(item, ("id", "callId", "call_id", "process_id")),
            })
        elif path and any(token in item_type_lower for token in ("file", "patch", "edit", "update")):
            events.append({
                "summary_type": "edit",
                "path": path,
                "status": status,
                "id": first_value(item, ("id", "itemId", "item_id")),
            })
        elif tool_name and any(token in item_type_lower for token in ("tool", "call", "mcp")):
            events.append({
                "summary_type": "tool",
                "tool": tool_name,
                "status": status,
                "type": item_type,
                "id": first_value(item, ("id", "callId", "call_id")),
            })
    return events
