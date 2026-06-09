from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets


BRIDGE_ROOT = Path(__file__).resolve().parent
if BRIDGE_ROOT.name.lower() == "tools":
    BRIDGE_ROOT = BRIDGE_ROOT.parent
WORKSPACE_ROOT = Path.cwd()
STATE_DIR = BRIDGE_ROOT / ".lark-events"
CONFIG_PATH = STATE_DIR / "bridge-config.json"
STATE_PATH = STATE_DIR / "bridge-state.json"
LOG_PATH = STATE_DIR / "bridge.log"
ENV_PATH = BRIDGE_ROOT / ".env"


@dataclass
class BridgeConfig:
    allowed_chat_ids: set[str]
    command_prefix: str
    codex_ws_url: str
    codex_token_file: Path
    lark_app_id: str
    lark_app_secret: str
    lark_cli: str
    reply_mode: str
    turn_timeout_seconds: int


@dataclass
class ActiveTurn:
    future: asyncio.Future[dict[str, Any]]
    on_message: Any | None = None
    seen_messages: set[str] | None = None


def setup_logging() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def load_dotenv(path: Path = ENV_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SystemExit(f"Invalid .env line {line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def lark_env(config: BridgeConfig) -> dict[str, str]:
    env = os.environ.copy()
    if config.lark_app_id:
        env["LARK_APP_ID"] = config.lark_app_id
        env["FEISHU_APP_ID"] = config.lark_app_id
    if config.lark_app_secret:
        env["LARK_APP_SECRET"] = config.lark_app_secret
        env["FEISHU_APP_SECRET"] = config.lark_app_secret
    return env


def load_config(path: Path = CONFIG_PATH) -> BridgeConfig:
    if not path.exists():
        raise SystemExit(f"Missing config: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    dotenv = load_dotenv()
    chat_ids = split_csv(dotenv.get("LARK_CHAT_IDS") or dotenv.get("LARK_CHAT_ID") or "")
    allowed_chat_ids = set(chat_ids or data.get("allowed_chat_ids") or [])
    if not allowed_chat_ids:
        raise SystemExit(".env must contain LARK_CHAT_IDS or LARK_CHAT_ID.")

    codex_token_file = Path(data.get("codex_token_file") or STATE_DIR / "codex-ws-token.txt")
    if not codex_token_file.is_absolute():
        codex_token_file = BRIDGE_ROOT / codex_token_file

    return BridgeConfig(
        allowed_chat_ids=allowed_chat_ids,
        command_prefix=str(data.get("command_prefix") or "").strip(),
        codex_ws_url=str(data.get("codex_ws_url") or "ws://127.0.0.1:17345"),
        codex_token_file=codex_token_file,
        lark_app_id=dotenv.get("LARK_APP_ID", ""),
        lark_app_secret=dotenv.get("LARK_APP_SECRET", ""),
        lark_cli=str(data.get("lark_cli") or "lark-cli.cmd"),
        reply_mode=str(data.get("reply_mode") or "all"),
        turn_timeout_seconds=int(data.get("turn_timeout_seconds") or 900),
    )


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))


def save_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def update_process_state(workspace_root: Path, codex_proc: subprocess.Popen[str], lark_proc: subprocess.Popen[str]) -> None:
    state = load_state()
    state["workspace_root"] = str(workspace_root)
    state["processes"] = {
        "bridge": os.getpid(),
        "codex": codex_proc.pid,
        "lark": lark_proc.pid,
    }
    save_state(state)


def clear_process_state() -> None:
    state = load_state()
    if "processes" in state or "workspace_root" in state:
        state.pop("processes", None)
        state.pop("workspace_root", None)
        save_state(state)


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
    args = [
        config.lark_cli,
        "event",
        "consume",
        "im.message.receive_v1",
        "--as",
        "bot",
    ]
    logging.info("starting lark event consumer")
    return subprocess.Popen(
        args,
        cwd=BRIDGE_ROOT,
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


class CodexClient:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.ws: websockets.WebSocketClientProtocol | None = None
        self.next_id = 1
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.active_turns: dict[str, ActiveTurn] = {}
        self.reader_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        token = ensure_token_file(self.config.codex_token_file)
        headers = {"Authorization": f"Bearer {token}"}
        connect_kwargs = {"additional_headers": headers}
        if "additional_headers" not in inspect.signature(websockets.connect).parameters:
            connect_kwargs = {"extra_headers": headers}
        self.ws = await websockets.connect(self.config.codex_ws_url, **connect_kwargs)
        self.reader_task = asyncio.create_task(self._reader())
        await self.request(
            "initialize",
            {
                "clientInfo": {"name": "codex-lark-bridge", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
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
            if "id" in msg and msg["id"] in self.pending:
                future = self.pending.pop(msg["id"])
                if "error" in msg:
                    future.set_exception(RuntimeError(json.dumps(msg["error"], ensure_ascii=False)))
                else:
                    future.set_result(msg.get("result") or {})
                continue
            if msg.get("method") == "turn/completed":
                params = msg.get("params") or {}
                thread_id = params.get("threadId")
                active = self.active_turns.get(thread_id)
                if active:
                    await self._emit_agent_messages(active, msg)
                    if not active.future.done():
                        active.future.set_result(params)
                continue
            thread_id = extract_thread_id(msg)
            active = self.active_turns.get(thread_id) if thread_id else None
            if active:
                await self._emit_agent_messages(active, msg)

    async def _emit_agent_messages(self, active: ActiveTurn, payload: dict[str, Any]) -> None:
        if not active.on_message:
            return
        if active.seen_messages is None:
            active.seen_messages = set()
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

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self.ws is not None
        request_id = self.next_id
        self.next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self.ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
        return await future

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
        save_state(state)
        logging.info("created codex thread thread_id=%s", thread_id)
        return thread_id

    async def run_turn(
        self,
        thread_id: str,
        text: str,
        timeout_seconds: int,
        workspace_root: Path,
        on_message: Any | None = None,
    ) -> str:
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.active_turns[thread_id] = ActiveTurn(future=future, on_message=on_message, seen_messages=set())
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
            self.active_turns.pop(thread_id, None)
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
                    seen_messages=set(),
                )
                await self._emit_agent_messages(active, full_turn)
                self.active_turns.pop(thread_id, None)
                return ""
            answer = extract_reply_answer(full_turn, all_messages=False)
            if answer:
                self.active_turns.pop(thread_id, None)
                return answer
        self.active_turns.pop(thread_id, None)
        return "" if on_message else "Codex did not return a final answer."

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
    content_json = json.dumps({"text": reply_text}, ensure_ascii=False)
    args = [
        config.lark_cli,
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
    ]
    result = subprocess.run(
        args,
        cwd=BRIDGE_ROOT,
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


async def consume_events(
    config: BridgeConfig,
    codex: CodexClient,
    lark_proc: subprocess.Popen[str],
    workspace_root: Path,
) -> None:
    if lark_proc.stdout is None:
        raise RuntimeError("lark event consumer stdout is unavailable")
    state = load_state()
    workspace_key = str(workspace_root)
    thread_id = str((state.get("threads") or {}).get(workspace_key) or "")
    logging.info(
        "bridge is consuming lark events allowed_chat_ids=%s workspace_root=%s bridge_root=%s",
        sorted(config.allowed_chat_ids),
        workspace_root,
        BRIDGE_ROOT,
    )
    while True:
        line = await asyncio.to_thread(lark_proc.stdout.readline)
        if not line:
            raise RuntimeError("lark event consumer exited")
        event = normalize_event(line)
        if not event:
            continue
        ok, prompt = should_handle(event, config)
        if not ok:
            continue
        logging.info("accepted message chat_id=%s message_id=%s", event.get("chat_id"), event.get("message_id"))
        try:
            if not thread_id:
                thread_id = await codex.ensure_thread(state, workspace_root)
            stream_messages = wants_all_codex_messages(config)

            async def reply_streamed_message(message: dict[str, Any]) -> None:
                text = str(message.get("text") or "").strip()
                if text:
                    await asyncio.to_thread(reply_to_lark, config, event, text)

            try:
                answer = await codex.run_turn(
                    thread_id,
                    prompt,
                    config.turn_timeout_seconds,
                    workspace_root,
                    on_message=reply_streamed_message if stream_messages else None,
                )
            except Exception as exc:
                if not is_thread_not_found(exc):
                    raise
                logging.warning("saved codex thread was not found; creating a new thread and retrying")
                state.setdefault("threads", {}).pop(workspace_key, None)
                if state.get("cwd") == workspace_key:
                    state.pop("thread_id", None)
                save_state(state)
                thread_id = await codex.ensure_thread(state, workspace_root)
                answer = await codex.run_turn(
                    thread_id,
                    prompt,
                    config.turn_timeout_seconds,
                    workspace_root,
                    on_message=reply_streamed_message if stream_messages else None,
                )
        except Exception as exc:
            logging.exception("codex turn failed")
            answer = f"Codex bridge error: {exc}"
        if answer:
            await asyncio.to_thread(reply_to_lark, config, event, answer)


async def run_bridge(workspace_root: Path) -> None:
    setup_logging()
    config = load_config()
    workspace_root = workspace_root.resolve()
    codex_proc = start_codex_app_server(config, workspace_root)
    lark_proc = start_lark_consumer(config)
    update_process_state(workspace_root, codex_proc, lark_proc)
    stop = asyncio.Event()

    def request_stop() -> None:
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, request_stop)
        except NotImplementedError:
            pass

    tasks = [
        asyncio.create_task(pipe_process_output("codex", codex_proc.stdout)),
        asyncio.create_task(pipe_process_output("lark-stderr", lark_proc.stderr)),
    ]
    codex = CodexClient(config)
    try:
        await asyncio.sleep(2)
        await codex.connect()
        consumer_task = asyncio.create_task(consume_events(config, codex, lark_proc, workspace_root))
        def log_consumer_done(task: asyncio.Task[None]) -> None:
            if task.cancelled():
                logging.info("event consumer stopped")
                return
            exc = task.exception()
            if exc:
                logging.error("event consumer stopped: %s", exc)
            else:
                logging.info("event consumer stopped")

        consumer_task.add_done_callback(log_consumer_done)
        tasks.append(consumer_task)
        await stop.wait()
    finally:
        await codex.close()
        if lark_proc.stdin:
            lark_proc.stdin.close()
        lark_proc.terminate()
        codex_proc.terminate()
        clear_process_state()
        for task in tasks:
            task.cancel()


def init_config(chat_id: str | None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        raise SystemExit(f"Config already exists: {CONFIG_PATH}")
    if not ENV_PATH.exists():
        env_data = [
            "LARK_APP_ID=",
            "LARK_APP_SECRET=",
            f"LARK_CHAT_IDS={chat_id or ''}",
            "",
        ]
        ENV_PATH.write_text("\n".join(env_data), encoding="utf-8")
        print(f"created {ENV_PATH}")
    data = {
        "command_prefix": "",
        "codex_ws_url": "ws://127.0.0.1:17345",
        "codex_token_file": ".lark-events/codex-ws-token.txt",
        "lark_cli": "lark-cli.cmd",
        "reply_mode": "all",
        "turn_timeout_seconds": 900,
    }
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"created {CONFIG_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bridge allowed Lark chats to a Codex app-server thread.")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--chat-id")
    sub.add_parser("run")
    run = sub.choices["run"]
    run.add_argument("--workspace-root", default=str(Path.cwd()))
    args = parser.parse_args()
    if args.command == "init":
        init_config(args.chat_id)
    elif args.command == "run":
        asyncio.run(run_bridge(Path(args.workspace_root)))


if __name__ == "__main__":
    main()
