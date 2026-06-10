from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets

from .approvals import (
    ApprovalRegistry,
    PendingApproval,
    build_approval_response,
)
from .codex_events import (
    event_markers,
    extract_agent_messages,
    extract_reply_answer,
    extract_summary_events,
    extract_thread_id,
    is_approval_payload,
    message_markers,
)
from .config import BridgeConfig
from .processes import ensure_token_file, log_task_done
from .state import save_state


@dataclass
class ActiveTurn:
    future: asyncio.Future[dict[str, Any]]
    on_message: Any | None = None
    on_event: Any | None = None
    seen_messages: set[str] | None = None
    seen_events: set[str] | None = None


class CodexClient:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.ws: websockets.WebSocketClientProtocol | None = None
        self.next_id = 1
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.active_turns: dict[str, ActiveTurn] = {}
        self.approvals = ApprovalRegistry()
        self.reader_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        token = ensure_token_file(self.config.codex_token_file)
        headers = {"Authorization": f"Bearer {token}"}
        connect_kwargs = {"additional_headers": headers}
        if "additional_headers" not in inspect.signature(websockets.connect).parameters:
            connect_kwargs = {"extra_headers": headers}
        self.ws = await websockets.connect(self.config.codex_ws_url, **connect_kwargs)
        self.reader_task = asyncio.create_task(self._reader())
        self.reader_task.add_done_callback(log_task_done("codex websocket reader"))
        await self.request(
            "initialize",
            {
                "clientInfo": {"name": "codex-lark-bridge", "version": "0.1.0"},
                "capabilities": {
                    "experimentalApi": True,
                    "requestPermissions": True,
                    "approvalId": True,
                    "parsedCmd": True,
                    "grantRoot": True,
                    "fileChanges": True,
                    "data": True,
                },
            },
        )
        logging.info("connected to codex app-server")

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()
        if self.reader_task:
            self.reader_task.cancel()

    async def _reader(self) -> None:
        assert self.ws is not None
        async for raw in self.ws:
            msg = json.loads(raw)
            if self.active_turns and ("id" not in msg or msg.get("method")):
                logging.info(
                    "codex websocket event method=%s type=%s keys=%s payload=%s",
                    msg.get("method"),
                    msg.get("type"),
                    sorted(str(key) for key in msg.keys()),
                    json.dumps(msg, ensure_ascii=False)[:4000],
                )
            if "id" in msg and msg["id"] in self.pending:
                future = self.pending.pop(msg["id"])
                if "error" in msg:
                    future.set_exception(RuntimeError(json.dumps(msg["error"], ensure_ascii=False)))
                else:
                    future.set_result(msg.get("result") or {})
                continue
            if is_approval_payload(msg):
                logging.info("codex approval payload matched payload=%s", json.dumps(msg, ensure_ascii=False)[:4000])
                await self._route_event_to_active_turn(msg, approval_only=True)
                continue
            if msg.get("method") == "turn/completed":
                params = msg.get("params") or {}
                thread_id = params.get("threadId")
                active = self.active_turns.get(thread_id)
                if active:
                    await self._emit_turn_events(active, msg)
                    if not active.future.done():
                        active.future.set_result(params)
                continue
            await self._route_event_to_active_turn(msg)

    async def _route_event_to_active_turn(self, msg: dict[str, Any], approval_only: bool = False) -> None:
        if not self.active_turns:
            if approval_only:
                logging.warning("received approval request with no active turn payload=%s", json.dumps(msg, ensure_ascii=False)[:4000])
            return
        thread_id = extract_thread_id(msg)
        active = self.active_turns.get(thread_id) if thread_id else None
        if active:
            await self._emit_turn_events(active, msg)
            return
        if approval_only:
            logging.warning(
                "approval request had no matching active turn thread_id=%s payload=%s",
                thread_id,
                json.dumps(msg, ensure_ascii=False)[:4000],
            )
            return
        logging.info(
            "broadcasting codex websocket event without active thread match method=%s type=%s payload=%s",
            msg.get("method"),
            msg.get("type"),
            json.dumps(msg, ensure_ascii=False)[:4000],
        )
        for active_turn in list(self.active_turns.values()):
            await self._emit_turn_events(active_turn, msg)

    async def _emit_turn_events(self, active: ActiveTurn, payload: dict[str, Any]) -> None:
        if active.seen_messages is None:
            active.seen_messages = set()
        if active.seen_events is None:
            active.seen_events = set()
        if active.on_message:
            for message in extract_agent_messages(payload):
                markers = message_markers(message)
                if markers & active.seen_messages:
                    continue
                active.seen_messages.update(markers)
                try:
                    result = active.on_message(message)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logging.exception("failed to emit streamed codex message")
                if message.get("phase") == "final_answer" and not active.future.done():
                    active.future.set_result({"turn": {"items": [message]}})
        if active.on_event:
            for event in extract_summary_events(payload):
                markers = event_markers(event)
                if markers & active.seen_events:
                    continue
                active.seen_events.update(markers)
                try:
                    result = active.on_event(event, payload)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logging.exception("failed to emit streamed codex event")

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self.ws is not None
        request_id = self.next_id
        self.next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self.ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
        return await future

    def register_approval(
        self,
        thread_id: str,
        chat_id: str,
        summary: str,
        payload: dict[str, Any],
    ) -> PendingApproval | None:
        approval = self.approvals.register(thread_id, chat_id, summary, payload)
        if not approval:
            logging.info("ignored already resolved codex approval payload=%s", json.dumps(payload, ensure_ascii=False)[:4000])
            return None
        logging.info(
            "registered codex approval request_id=%s rpc_id=%s key=%s payload=%s",
            approval.request_id,
            approval.rpc_id,
            approval.approval_key,
            json.dumps(payload, ensure_ascii=False)[:4000],
        )
        return approval

    def find_pending_approval(
        self,
        chat_id: str,
        thread_id: str = "",
        request_id: int | None = None,
    ) -> tuple[PendingApproval | None, str]:
        return self.approvals.find(chat_id, thread_id, request_id)

    async def resolve_approval(self, approval: PendingApproval, approved: bool) -> str:
        response = build_approval_response(approval.payload, approved)
        if approval.rpc_id is not None and self.ws is not None:
            await self.ws.send(json.dumps({"id": approval.rpc_id, "result": response}, ensure_ascii=False))
            self.approvals.resolve(approval)
            logging.info(
                "resolved codex approval request_id=%s rpc_id=%s approved=%s response=%s",
                approval.request_id,
                approval.rpc_id,
                approved,
                json.dumps(response, ensure_ascii=False),
            )
            return f"已{'批准' if approved else '拒绝'} Codex 权限请求 #{approval.request_id}。"
        methods = [
            "item/permissions/requestApproval",
            "item/permissions/resolveApproval",
            "permission/requestApproval/respond",
            "permissions/respond",
        ]
        errors: list[str] = []
        for method in methods:
            try:
                await self.request(method, response)
                self.approvals.resolve(approval)
                logging.info(
                    "resolved codex approval request_id=%s approved=%s method=%s",
                    approval.request_id,
                    approved,
                    method,
                )
                return f"已{'批准' if approved else '拒绝'} Codex 权限请求 #{approval.request_id}。"
            except Exception as exc:
                errors.append(f"{method}: {exc}")
        logging.error(
            "failed to resolve codex approval request_id=%s approved=%s errors=%s payload=%s",
            approval.request_id,
            approved,
            errors,
            json.dumps(approval.payload, ensure_ascii=False)[:4000],
        )
        return f"权限请求 #{approval.request_id} 的响应发送失败，已写入日志。"

    async def ensure_thread(self, state: dict[str, Any], workspace_root: Path) -> str:
        workspace_key = str(workspace_root)
        threads = state.setdefault("threads", {})
        thread_id = threads.get(workspace_key)
        if not thread_id and state.get("cwd") == workspace_key:
            thread_id = state.get("thread_id")
        if thread_id:
            return str(thread_id)
        logging.info("creating codex thread cwd=%s", workspace_root)
        result = await self.request(
            "thread/start",
            {
                "cwd": workspace_key,
                "approvalPolicy": "on-request",
                "sandbox": "workspace-write",
                "threadSource": "user",
            },
        )
        thread_id = result["thread"]["id"]
        threads[workspace_key] = thread_id
        state["thread_id"] = thread_id
        state["cwd"] = workspace_key
        save_state(self.config, state)
        logging.info("created codex thread thread_id=%s", thread_id)
        return thread_id

    async def run_turn(
        self,
        thread_id: str,
        text: str,
        timeout_seconds: int,
        workspace_root: Path,
        on_message: Any | None = None,
        on_event: Any | None = None,
    ) -> str:
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.active_turns[thread_id] = ActiveTurn(
            future=future,
            on_message=on_message,
            on_event=on_event,
            seen_messages=set(),
            seen_events=set(),
        )
        try:
            await self.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "cwd": str(workspace_root),
                    "input": [{"type": "text", "text": text}],
                },
            )
            params = await asyncio.wait_for(future, timeout=timeout_seconds)
            turn = params.get("turn") or {}
            if turn.get("status") == "failed":
                error = turn.get("error") or {}
                return f"Codex turn failed: {error.get('message') or 'unknown error'}"
            answer = extract_reply_answer(turn, all_messages=bool(on_message))
            if answer:
                return "" if on_message else answer
            turn_id = str(turn.get("id") or "")
            logging.info(
                "turn completion had no extractable text thread_id=%s turn_id=%s items_view=%s item_types=%s",
                thread_id,
                turn_id,
                turn.get("itemsView"),
                [item.get("type") for item in turn.get("items", [])],
            )
            if turn_id:
                full_turn = await self.find_turn(thread_id, turn_id)
                if on_message:
                    active = self.active_turns.get(thread_id) or ActiveTurn(
                        future=future,
                        on_message=on_message,
                        on_event=on_event,
                        seen_messages=set(),
                        seen_events=set(),
                    )
                    await self._emit_turn_events(active, full_turn)
                    return ""
                answer = extract_reply_answer(full_turn, all_messages=False)
                if answer:
                    return answer
            return "" if on_message else "Codex did not return a final answer."
        finally:
            self.active_turns.pop(thread_id, None)

    async def find_turn(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        cursor: str | None = None
        while True:
            result = await self.request(
                "thread/turns/list",
                {
                    "threadId": thread_id,
                    "cursor": cursor,
                    "itemsView": "full",
                    "limit": 20,
                    "sortDirection": "desc",
                },
            )
            for turn in result.get("data") or []:
                if str(turn.get("id") or "") == turn_id:
                    logging.info(
                        "loaded full turn thread_id=%s turn_id=%s items_view=%s item_types=%s",
                        thread_id,
                        turn_id,
                        turn.get("itemsView"),
                        [item.get("type") for item in turn.get("items", [])],
                    )
                    return turn
            cursor = result.get("nextCursor")
            if not cursor:
                break
        logging.warning("could not find turn in thread/turns/list thread_id=%s turn_id=%s", thread_id, turn_id)
        return {"items": []}
