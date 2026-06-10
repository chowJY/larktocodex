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
PROJECTS_CONFIG_PATH = STATE_DIR / "projects-config.json"
STATE_PATH = STATE_DIR / "bridge-state.json"
LOG_PATH = STATE_DIR / "bridge.log"
ENV_PATH = BRIDGE_ROOT / ".env"


@dataclass
class BridgeConfig:
    name: str
    is_project: bool
    lark_profile: str
    allowed_chat_ids: set[str]
    command_prefix: str
    codex_ws_url: str
    codex_token_file: Path
    log_path: Path
    lark_app_id: str
    lark_app_secret: str
    lark_cli: str
    reply_mode: str
    state_dir: Path
    state_path: Path
    turn_timeout_seconds: int


@dataclass
class ActiveTurn:
    future: asyncio.Future[dict[str, Any]]
    on_message: Any | None = None
    seen_messages: set[str] | None = None


def setup_logging(config: BridgeConfig) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(config.log_path, encoding="utf-8"),
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


def configured_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text or "REPLACE_ME" in text:
        return ""
    return text


def lark_env(config: BridgeConfig) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("LARK_APP_ID", "FEISHU_APP_ID", "LARK_APP_SECRET", "FEISHU_APP_SECRET"):
        env.pop(key, None)
    if config.lark_app_id:
        env["LARK_APP_ID"] = config.lark_app_id
        env["FEISHU_APP_ID"] = config.lark_app_id
    if config.lark_app_secret:
        env["LARK_APP_SECRET"] = config.lark_app_secret
        env["FEISHU_APP_SECRET"] = config.lark_app_secret
    return env


def safe_project_name(name: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name.strip())
    return safe or "default"


def read_projects_config(path: Path = PROJECTS_CONFIG_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    projects = data.get("projects") or []
    if not isinstance(projects, list):
        raise SystemExit(f"Invalid projects config: {path} projects must be a list")
    return [project for project in projects if isinstance(project, dict)]


def select_project_config(project_name: str = "", use_first_project: bool = False) -> dict[str, Any] | None:
    projects = read_projects_config()
    if not projects:
        return None
    if project_name:
        for project in projects:
            if str(project.get("name") or "") == project_name:
                return project
        raise SystemExit(f"Project not found in {PROJECTS_CONFIG_PATH}: {project_name}")
    if use_first_project:
        return projects[0]
    return None


def project_chat_ids(project: dict[str, Any]) -> list[str]:
    values: list[str] = []
    chat_ids = project.get("chat_ids")
    if isinstance(chat_ids, list):
        values.extend(configured_value(item) for item in chat_ids)
    elif isinstance(chat_ids, str):
        values.extend(configured_value(item) for item in split_csv(chat_ids))
    chat_id = project.get("chat_id")
    if configured_value(chat_id):
        values.append(configured_value(chat_id))
    return [item.strip() for item in values if item and item.strip()]


def resolve_project_path(value: Any, default: Path) -> Path:
    path = Path(str(value)) if value else default
    if not path.is_absolute():
        path = BRIDGE_ROOT / path
    return path


def load_config(
    path: Path = CONFIG_PATH,
    project_name: str = "",
    use_first_project: bool = False,
    codex_ws_url: str = "",
) -> BridgeConfig:
    project = select_project_config(project_name, use_first_project)
    if project is not None:
        name = str(project.get("name") or project_name or "default")
        safe_name = safe_project_name(name)
        state_dir = STATE_DIR / "projects" / safe_name
        codex_token_file = resolve_project_path(
            project.get("codex_token_file") or f".lark-events/projects/{safe_name}/codex-ws-token.txt",
            STATE_DIR / "projects" / safe_name / "codex-ws-token.txt",
        )
        chat_ids = project_chat_ids(project)
        allowed_chat_ids = set(chat_ids)
        if not allowed_chat_ids:
            raise SystemExit(f"Project '{name}' must contain chat_id or chat_ids.")
        lark_app_id = configured_value(project.get("app_id") or project.get("lark_app_id"))
        lark_app_secret = configured_value(project.get("app_secret") or project.get("lark_app_secret"))
        if not lark_app_id or not lark_app_secret:
            raise SystemExit(f"Project '{name}' must contain app_id and app_secret.")
        return BridgeConfig(
            name=name,
            is_project=True,
            lark_profile=str(project.get("profile") or lark_app_id),
            allowed_chat_ids=allowed_chat_ids,
            command_prefix=str(project.get("command_prefix") or "").strip(),
            codex_ws_url=codex_ws_url or str(project.get("codex_ws_url") or "ws://127.0.0.1:17345"),
            codex_token_file=codex_token_file,
            log_path=state_dir / "bridge.log",
            lark_app_id=lark_app_id,
            lark_app_secret=lark_app_secret,
            lark_cli=str(project.get("lark_cli") or "lark-cli.cmd"),
            reply_mode=str(project.get("reply_mode") or "all"),
            state_dir=state_dir,
            state_path=state_dir / "bridge-state.json",
            turn_timeout_seconds=int(project.get("turn_timeout_seconds") or 900),
        )

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
        name="default",
        is_project=False,
        lark_profile="",
        allowed_chat_ids=allowed_chat_ids,
        command_prefix=str(data.get("command_prefix") or "").strip(),
        codex_ws_url=codex_ws_url or str(data.get("codex_ws_url") or "ws://127.0.0.1:17345"),
        codex_token_file=codex_token_file,
        log_path=LOG_PATH,
        lark_app_id=dotenv.get("LARK_APP_ID", ""),
        lark_app_secret=dotenv.get("LARK_APP_SECRET", ""),
        lark_cli=str(data.get("lark_cli") or "lark-cli.cmd"),
        reply_mode=str(data.get("reply_mode") or "all"),
        state_dir=STATE_DIR,
        state_path=STATE_PATH,
        turn_timeout_seconds=int(data.get("turn_timeout_seconds") or 900),
    )


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


def update_process_state(
    config: BridgeConfig,
    workspace_root: Path,
    codex_proc: subprocess.Popen[str],
    lark_proc: subprocess.Popen[str],
) -> None:
    state = load_state(config)
    state["project_name"] = config.name
    state["workspace_root"] = str(workspace_root)
    state["codex_ws_url"] = config.codex_ws_url
    state["allowed_chat_ids"] = sorted(config.allowed_chat_ids)
    state["processes"] = {
        "bridge": os.getpid(),
        "codex": codex_proc.pid,
        "lark": lark_proc.pid,
    }
    save_state(config, state)


def clear_process_state(config: BridgeConfig) -> None:
    state = load_state(config)
    if "processes" in state or "workspace_root" in state:
        state.pop("processes", None)
        state.pop("workspace_root", None)
        save_state(config, state)


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
    ]
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
    ]
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
    args = [
        config.lark_cli,
    ]
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


async def consume_events(
    config: BridgeConfig,
    codex: CodexClient,
    lark_proc: subprocess.Popen[str],
    workspace_root: Path,
    initial_thread_id: str = "",
) -> None:
    if lark_proc.stdout is None:
        raise RuntimeError("lark event consumer stdout is unavailable")
    state = load_state(config)
    workspace_key = str(workspace_root)
    thread_id = initial_thread_id or str((state.get("threads") or {}).get(workspace_key) or "")
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
                save_state(config, state)
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
    codex_proc = start_codex_app_server(config, workspace_root)
    lark_proc = start_lark_consumer(config)
    update_process_state(config, workspace_root, codex_proc, lark_proc)
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
        state = load_state(config)
        thread_id = await codex.ensure_thread(state, workspace_root)
        logging.info("codex thread is ready before consuming lark events thread_id=%s", thread_id)
        await asyncio.to_thread(send_startup_messages, config)
        consumer_task = asyncio.create_task(consume_events(config, codex, lark_proc, workspace_root, thread_id))
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
        clear_process_state(config)
        for task in tasks:
            task.cancel()


def init_config(chat_id: str | None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not ENV_PATH.exists():
        env_data = [
            "LARK_APP_ID=",
            "LARK_APP_SECRET=",
            f"LARK_CHAT_IDS={chat_id or ''}",
            "",
        ]
        ENV_PATH.write_text("\n".join(env_data), encoding="utf-8")
        print(f"created {ENV_PATH}")
    if not CONFIG_PATH.exists():
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
    if not PROJECTS_CONFIG_PATH.exists():
        projects_data = {
            "projects": [
                {
                    "name": "default",
                    "app_id": "",
                    "app_secret": "",
                    "chat_id": chat_id or "oc_REPLACE_ME",
                    "workspace_root": str(Path.cwd()),
                    "codex_ws_url": "auto",
                    "codex_token_file": ".lark-events/projects/default/codex-ws-token.txt",
                    "lark_cli": "lark-cli.cmd",
                    "reply_mode": "all",
                    "turn_timeout_seconds": 900,
                }
            ]
        }
        PROJECTS_CONFIG_PATH.write_text(
            json.dumps(projects_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"created {PROJECTS_CONFIG_PATH}")


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


if __name__ == "__main__":
    main()
