from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from .codex_events import compact_text, first_value


@dataclass
class PendingApproval:
    request_id: int
    approval_key: str
    approval_keys: set[str]
    rpc_id: Any | None
    thread_id: str
    chat_id: str
    summary: str
    created_at: float
    payload: dict[str, Any]
    announced: bool = False
    resolved: bool = False


@dataclass
class ApprovalDecision:
    approved: bool
    request_id: int | None = None


class ApprovalRegistry:
    def __init__(self) -> None:
        self.pending: dict[int, PendingApproval] = {}
        self.by_key: dict[str, int] = {}
        self.resolved_keys: set[str] = set()
        self.next_request_id = 1

    def register(self, thread_id: str, chat_id: str, summary: str, payload: dict[str, Any]) -> PendingApproval | None:
        approval_keys = approval_dedupe_keys(payload, thread_id)
        if approval_keys & self.resolved_keys:
            return None
        for approval_key in approval_keys:
            existing_request_id = self.by_key.get(approval_key)
            if existing_request_id is None:
                continue
            approval = self.pending.get(existing_request_id)
            if approval:
                approval.approval_keys.update(approval_keys)
                for key in approval_keys:
                    self.by_key[key] = approval.request_id
                return approval
            self.by_key.pop(approval_key, None)

        rpc_id = payload.get("id") if str(payload.get("method") or "").endswith("/requestApproval") else None
        request_id = self.next_request_id
        self.next_request_id += 1
        approval_key = sorted(approval_keys)[0]
        approval = PendingApproval(
            request_id=request_id,
            approval_key=approval_key,
            approval_keys=approval_keys,
            rpc_id=rpc_id,
            thread_id=thread_id,
            chat_id=chat_id,
            summary=summary,
            created_at=time.time(),
            payload=payload,
        )
        self.pending[request_id] = approval
        for key in approval_keys:
            self.by_key[key] = request_id
        return approval

    def resolve(self, approval: PendingApproval) -> None:
        approval.resolved = True
        self.pending.pop(approval.request_id, None)
        for key in approval.approval_keys:
            self.by_key.pop(key, None)
        self.resolved_keys.update(approval.approval_keys)
        if len(self.resolved_keys) > 1000:
            self.resolved_keys = set(list(self.resolved_keys)[-500:])

    def find(self, chat_id: str, thread_id: str = "", request_id: int | None = None) -> tuple[PendingApproval | None, str]:
        approvals = [
            approval for approval in self.pending.values()
            if approval.chat_id == chat_id and (not thread_id or approval.thread_id == thread_id)
        ]
        if request_id is not None:
            approval = self.pending.get(request_id)
            if approval and approval.chat_id == chat_id and (not thread_id or approval.thread_id == thread_id):
                return approval, ""
            return None, f"没有找到待处理的 Codex 权限请求 #{request_id}。"
        if not approvals:
            return None, "当前没有待处理的 Codex 权限请求。"
        if len(approvals) > 1:
            ids = ", ".join(f"#{approval.request_id}" for approval in sorted(approvals, key=lambda item: item.created_at))
            return None, f"当前有多个待处理权限请求：{ids}。请回复“同意 #编号”或“拒绝 #编号”。"
        return approvals[0], ""


def approval_summary(payload: dict[str, Any], request_id: int) -> str:
    request = first_value(payload, ("request", "params", "item", "event", "data")) or payload
    if not isinstance(request, dict):
        request = payload
    action = first_value(request, ("action", "program", "exec", "apply_patch"))
    if isinstance(action, dict):
        command = first_value(action, ("command", "parsedCmd", "parsed_cmd", "argv", "program"))
    else:
        command = None
    command = command or first_value(request, ("command", "parsedCmd", "parsed_cmd", "argv", "program"))
    permissions = first_value(request, ("permissions", "additional_permissions", "requested_additional_permissions"))
    reason = first_value(request, ("reason", "justification", "description", "title"))
    cwd = first_value(request, ("cwd", "working_directory", "workingDirectory"))
    lines = [f"Codex 请求权限 #{request_id}"]
    if command:
        lines.append(f"Command: {compact_text(command, 500)}")
    if permissions:
        lines.append(f"Permissions: {compact_text(permissions, 500)}")
    if cwd:
        lines.append(f"CWD: {compact_text(cwd, 500)}")
    if reason:
        lines.append(f"Reason: {compact_text(reason, 500)}")
    lines.append(f"回复“同意 #{request_id}”允许，或回复“拒绝 #{request_id}”拒绝。")
    return "\n".join(lines)


def build_approval_response(payload: dict[str, Any], approved: bool) -> Any:
    source = payload.get("params") if isinstance(payload.get("params"), dict) else payload
    method = str(payload.get("method") or "")
    if method in {"commandExecution/requestApproval", "item/commandExecution/requestApproval"}:
        proposed_execpolicy_amendment = first_value(
            source,
            ("proposedExecpolicyAmendment", "proposed_execpolicy_amendment", "execpolicy_amendment"),
        )
        if approved and proposed_execpolicy_amendment:
            decision: Any = {
                "acceptWithExecpolicyAmendment": {
                    "execpolicy_amendment": proposed_execpolicy_amendment,
                },
            }
        else:
            decision = "accept" if approved else "cancel"
        return {"decision": decision}

    approval_id = recursive_first(payload, ("approval_id", "approvalId", "itemId", "item_id")) or first_value(source, ("id",))
    proposed_execpolicy_amendment = first_value(
        source,
        ("proposedExecpolicyAmendment", "proposed_execpolicy_amendment", "execpolicy_amendment"),
    )
    if approved and proposed_execpolicy_amendment:
        decision: Any = {
            "acceptWithExecpolicyAmendment": {
                "execpolicy_amendment": proposed_execpolicy_amendment,
            },
        }
    else:
        decision = "accept" if approved else "cancel"
    response: dict[str, Any] = {
        "decision": decision,
        "approved": approved,
        "status": "Approved" if approved else "Denied",
        "permissions": "Approved" if approved else "Denied",
        "result": decision,
        "review_decision": decision,
    }
    for key in ("threadId", "thread_id", "turnId", "turn_id", "itemId", "item_id", "environmentId", "environment_id"):
        value = first_value(source, (key,))
        if value:
            response[key] = value
    if approval_id:
        response["approvalId"] = approval_id
        response["approval_id"] = approval_id
    return response


def approval_dedupe_keys(payload: dict[str, Any], fallback_thread_id: str = "") -> set[str]:
    source = payload.get("params") if isinstance(payload.get("params"), dict) else payload
    method = str(payload.get("method") or "")
    rpc_id = payload.get("id") if method.endswith("/requestApproval") else None
    approval_id = recursive_first(payload, ("approvalId", "approval_id"))
    item_id = recursive_first(payload, ("itemId", "item_id"))
    keys: set[str] = set()
    if approval_id:
        keys.add(f"approval:{approval_id}")
    if item_id:
        keys.add(f"item:{item_id}")
    if rpc_id:
        keys.add(f"rpc:{method}:{rpc_id}")
    thread_id = first_value(source, ("threadId", "thread_id")) or fallback_thread_id
    turn_id = first_value(source, ("turnId", "turn_id"))
    command = first_value(source, ("command", "parsedCmd", "parsed_cmd", "argv", "program", "path"))
    command_text = compact_text(command, 1000)
    started = first_value(source, ("startedAtMs", "started_at_ms"))
    if thread_id or turn_id or command_text:
        keys.add(f"fallback:{thread_id}:{turn_id}:{command_text}:{started}")
    if not keys:
        keys.add("payload:" + json.dumps(payload, ensure_ascii=False, sort_keys=True)[:1500])
    return keys


def recursive_first(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        direct = first_value(value, keys)
        if direct:
            return direct
        for child in value.values():
            found = recursive_first(child, keys)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = recursive_first(child, keys)
            if found:
                return found
    return None


def parse_approval_decision(text: str) -> ApprovalDecision | None:
    normalized = " ".join(text.strip().lower().split())
    if not normalized:
        return None
    request_id = None
    match = re.search(r"#\s*(\d+)|请求\s*(\d+)", normalized)
    if match:
        request_id = int(match.group(1) or match.group(2))

    deny_words = ("不同意", "不允许", "拒绝", "deny", "denied", "no", "n", "reject", "rejected", "cancel")
    approve_words = ("批准", "同意", "允许", "通过", "approve", "approved", "yes", "y", "ok", "allow", "accept")
    for word in deny_words:
        if normalized == word or normalized.startswith(word + " ") or normalized.startswith(word + "#"):
            return ApprovalDecision(False, request_id)
    for word in approve_words:
        if normalized == word or normalized.startswith(word + " ") or normalized.startswith(word + "#"):
            return ApprovalDecision(True, request_id)
    return None
