from __future__ import annotations

import asyncio
import logging
import signal
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .approvals import approval_summary, parse_approval_decision
from .codex_client import CodexClient
from .codex_events import format_summary_event, is_thread_not_found, wants_all_codex_messages, wants_summary_events
from .config import BridgeConfig, load_config
from .lark_io import normalize_event, reply_to_lark, send_startup_messages, should_handle
from .processes import log_task_done, pipe_process_output, start_codex_app_server, start_lark_consumer
from .state import (
    acquire_instance_lock,
    clear_process_state,
    load_processed_message_ids,
    load_state,
    release_instance_lock,
    save_processed_message_ids,
    save_state,
    update_process_state,
)


@dataclass
class QueuedMessage:
    event: dict[str, Any]
    prompt: str


@dataclass
class TurnSession:
    chat_id: str
    source_message_id: str
    task: asyncio.Task[None]
    state: str = "running"
    final_sent: bool = False
    reply_markers: set[str] = field(default_factory=set)

    def can_accept_followup(self) -> bool:
        return self.final_sent or self.task.done()


class BridgeRunner:
    def __init__(
        self,
        config: BridgeConfig,
        codex: CodexClient,
        lark_proc: subprocess.Popen[str],
        workspace_root: Path,
        initial_thread_id: str = "",
    ) -> None:
        self.config = config
        self.codex = codex
        self.lark_proc = lark_proc
        self.workspace_root = workspace_root
        self.state = load_state(config)
        self.workspace_key = str(workspace_root)
        self.thread_id = initial_thread_id or str((self.state.get("threads") or {}).get(self.workspace_key) or "")
        self.processed_message_ids = load_processed_message_ids(config)
        self.active_session: TurnSession | None = None
        self.queue: deque[QueuedMessage] = deque()

    async def consume(self) -> None:
        if self.lark_proc.stdout is None:
            raise RuntimeError("lark event consumer stdout is unavailable")
        logging.info(
            "bridge is consuming lark events allowed_chat_ids=%s workspace_root=%s",
            sorted(self.config.allowed_chat_ids),
            self.workspace_root,
        )
        while True:
            line = await asyncio.to_thread(self.lark_proc.stdout.readline)
            if not line:
                raise RuntimeError("lark event consumer exited")
            event = normalize_event(line)
            if not event:
                continue
            await self.handle_event(event)

    async def handle_event(self, event: dict[str, Any]) -> None:
        ok, prompt = should_handle(event, self.config)
        if not ok:
            return
        message_id = str(event.get("message_id") or event.get("id") or "")
        if message_id and message_id in self.processed_message_ids:
            logging.info("ignored duplicate message chat_id=%s message_id=%s", event.get("chat_id"), message_id)
            return
        if message_id:
            self.processed_message_ids.add(message_id)
            save_processed_message_ids(self.config, self.processed_message_ids)
        logging.info("accepted message chat_id=%s message_id=%s", event.get("chat_id"), message_id)

        decision = parse_approval_decision(prompt)
        if decision is not None:
            approval, error = self.codex.find_pending_approval(
                str(event.get("chat_id") or ""),
                self.thread_id,
                decision.request_id,
            )
            if approval:
                answer = await self.codex.resolve_approval(approval, decision.approved)
            else:
                answer = error
            await asyncio.to_thread(reply_to_lark, self.config, event, answer)
            return

        if self.active_session and not self.active_session.can_accept_followup():
            await asyncio.to_thread(
                reply_to_lark,
                self.config,
                event,
                "Codex 正在处理上一条需求；权限审批回复请使用“同意 #编号”或“拒绝 #编号”。",
            )
            return

        self.queue.append(QueuedMessage(event=event, prompt=prompt))
        self._start_next_if_idle()

    def _start_next_if_idle(self) -> None:
        if self.active_session and not self.active_session.task.done():
            return
        if not self.queue:
            return
        item = self.queue.popleft()
        chat_id = str(item.event.get("chat_id") or "")
        message_id = str(item.event.get("message_id") or item.event.get("id") or "")
        task = asyncio.create_task(self._run_message_turn(item.event, item.prompt))
        session = TurnSession(chat_id=chat_id, source_message_id=message_id, task=task)
        self.active_session = session
        task.add_done_callback(self._turn_done)

    def _turn_done(self, task: asyncio.Task[None]) -> None:
        log_task_done("codex turn task")(task)
        if self.active_session and self.active_session.task is task:
            if task.cancelled():
                self.active_session.state = "cancelled"
            elif task.exception():
                self.active_session.state = "failed"
            else:
                self.active_session.state = "completed"
            self.active_session = None
        self._start_next_if_idle()

    async def _reply_once(self, source_event: dict[str, Any], text: str, marker: str = "") -> None:
        if not text:
            return
        session = self.active_session
        reply_marker = marker or f"text:{text}"
        if session:
            if reply_marker in session.reply_markers:
                logging.info("skipping duplicate lark reply marker=%s", reply_marker[:200])
                return
            session.reply_markers.add(reply_marker)
        await asyncio.to_thread(reply_to_lark, self.config, source_event, text)

    async def _run_message_turn(self, source_event: dict[str, Any], prompt_text: str) -> None:
        answer = ""
        try:
            if not self.thread_id:
                self.thread_id = await self.codex.ensure_thread(self.state, self.workspace_root)
            stream_messages = wants_all_codex_messages(self.config)
            stream_events = wants_summary_events(self.config)

            async def reply_streamed_message(message: dict[str, Any]) -> None:
                text = str(message.get("text") or "").strip()
                if not text:
                    return
                phase = str(message.get("phase") or "")
                message_id = str(message.get("id") or "")
                if phase == "final_answer" and self.active_session:
                    self.active_session.final_sent = True
                    self.active_session.state = "final_sent"
                await self._reply_once(source_event, text, marker=f"agent:{phase}:{message_id or text}")

            async def reply_summary_event(summary_event: dict[str, Any], payload: dict[str, Any]) -> None:
                if summary_event.get("summary_type") == "approval":
                    approval = self.codex.register_approval(
                        self.thread_id,
                        str(source_event.get("chat_id") or ""),
                        "",
                        summary_event.get("payload") or payload,
                    )
                    if not approval:
                        return
                    if approval.announced:
                        logging.info(
                            "skipping duplicate codex approval announcement request_id=%s",
                            approval.request_id,
                        )
                        return
                    text = approval_summary(approval.payload, approval.request_id)
                    approval.summary = text
                    approval.announced = True
                    logging.info(
                        "replying codex approval request_id=%s message_id=%s reply_chars=%s",
                        approval.request_id,
                        source_event.get("message_id"),
                        len(text),
                    )
                    await self._reply_once(source_event, text, marker=f"approval:{approval.request_id}")
                    return

                if not stream_events:
                    return
                text = format_summary_event(summary_event)
                await self._reply_once(source_event, text, marker=f"event:{summary_event}")

            try:
                answer = await self.codex.run_turn(
                    self.thread_id,
                    prompt_text,
                    self.config.turn_timeout_seconds,
                    self.workspace_root,
                    on_message=reply_streamed_message if stream_messages else None,
                    on_event=reply_summary_event,
                )
            except Exception as exc:
                if not is_thread_not_found(exc):
                    raise
                logging.warning("saved codex thread was not found; creating a new thread and retrying")
                self.state.setdefault("threads", {}).pop(self.workspace_key, None)
                if self.state.get("cwd") == self.workspace_key:
                    self.state.pop("thread_id", None)
                save_state(self.config, self.state)
                self.thread_id = await self.codex.ensure_thread(self.state, self.workspace_root)
                answer = await self.codex.run_turn(
                    self.thread_id,
                    prompt_text,
                    self.config.turn_timeout_seconds,
                    self.workspace_root,
                    on_message=reply_streamed_message if stream_messages else None,
                    on_event=reply_summary_event,
                )
        except Exception as exc:
            logging.exception("codex turn failed")
            answer = f"Codex bridge error: {exc}"
        if answer:
            if self.active_session:
                self.active_session.final_sent = True
                self.active_session.state = "final_sent"
            await self._reply_once(source_event, answer, marker=f"final:{answer}")


async def run_bridge(
    workspace_root: Path,
    project_name: str = "",
    use_first_project: bool = False,
    codex_ws_url: str = "",
) -> None:
    config = load_config(
        project_name=project_name,
        use_first_project=use_first_project,
        codex_ws_url=codex_ws_url,
    )
    setup_logging(config)
    workspace_root = workspace_root.resolve()
    logging.info("starting bridge project=%s workspace=%s state=%s", config.name, workspace_root, config.state_path)
    lock_handle = None
    codex_proc: subprocess.Popen[str] | None = None
    lark_proc: subprocess.Popen[str] | None = None
    stop = asyncio.Event()

    def request_stop() -> None:
        logging.info("bridge stop requested")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, request_stop)
        except NotImplementedError:
            pass

    tasks: list[asyncio.Task[Any]] = []
    codex = CodexClient(config)
    try:
        lock_handle = acquire_instance_lock(config)
        codex_proc = start_codex_app_server(config, workspace_root)
        lark_proc = start_lark_consumer(config)
        update_process_state(config, workspace_root, codex_proc.pid, lark_proc.pid)
        tasks.extend([
            asyncio.create_task(pipe_process_output("codex", codex_proc.stdout)),
            asyncio.create_task(pipe_process_output("lark-stderr", lark_proc.stderr)),
        ])
        await asyncio.sleep(2)
        await codex.connect()
        state = load_state(config)
        thread_id = await codex.ensure_thread(state, workspace_root)
        logging.info("codex thread is ready before consuming lark events thread_id=%s", thread_id)
        await asyncio.to_thread(send_startup_messages, config)
        runner = BridgeRunner(config, codex, lark_proc, workspace_root, thread_id)
        consumer_task = asyncio.create_task(runner.consume())

        def log_consumer_done(task: asyncio.Task[None]) -> None:
            if task.cancelled():
                logging.info("event consumer stopped")
                return
            exc = task.exception()
            if exc:
                logging.error("event consumer stopped: %s", exc)
            else:
                logging.info("event consumer stopped")
            stop.set()

        consumer_task.add_done_callback(log_consumer_done)
        tasks.append(consumer_task)
        await stop.wait()
    except Exception:
        logging.exception("bridge failed project=%s workspace=%s", config.name, workspace_root)
        raise
    finally:
        logging.info("bridge shutting down")
        await codex.close()
        if lark_proc and lark_proc.stdin:
            lark_proc.stdin.close()
        if lark_proc and lark_proc.poll() is None:
            lark_proc.terminate()
        if codex_proc and codex_proc.poll() is None:
            codex_proc.terminate()
        clear_process_state(config)
        release_instance_lock(lock_handle)
        for task in tasks:
            task.cancel()


def setup_logging(config: BridgeConfig) -> None:
    import sys

    config.state_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(config.log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
