from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import BRIDGE_ROOT, CONFIG_PATH, ENV_PATH, LOG_PATH, PROJECTS_CONFIG_PATH, STATE_DIR, STATE_PATH


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
    reply_log_path: Path
    lark_app_id: str
    lark_app_secret: str
    lark_cli: str
    reply_mode: str
    event_reply_mode: str
    event_max_age_seconds: int
    state_dir: Path
    state_path: Path
    turn_timeout_seconds: int


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
        allowed_chat_ids = set(project_chat_ids(project))
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
            reply_log_path=state_dir / "lark-replies.log",
            lark_app_id=lark_app_id,
            lark_app_secret=lark_app_secret,
            lark_cli=str(project.get("lark_cli") or "lark-cli.cmd"),
            reply_mode=str(project.get("reply_mode") or "all"),
            event_reply_mode=str(project.get("event_reply_mode") or project.get("reply_mode") or "all"),
            event_max_age_seconds=int(project.get("event_max_age_seconds") or 120),
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
        reply_log_path=STATE_DIR / "lark-replies.log",
        lark_app_id=dotenv.get("LARK_APP_ID", ""),
        lark_app_secret=dotenv.get("LARK_APP_SECRET", ""),
        lark_cli=str(data.get("lark_cli") or "lark-cli.cmd"),
        reply_mode=str(data.get("reply_mode") or "all"),
        event_reply_mode=str(data.get("event_reply_mode") or data.get("reply_mode") or "all"),
        event_max_age_seconds=int(data.get("event_max_age_seconds") or 120),
        state_dir=STATE_DIR,
        state_path=STATE_PATH,
        turn_timeout_seconds=int(data.get("turn_timeout_seconds") or 900),
    )


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
            "event_reply_mode": "all",
            "event_max_age_seconds": 120,
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
                    "event_reply_mode": "all",
                    "event_max_age_seconds": 120,
                    "turn_timeout_seconds": 900,
                }
            ]
        }
        PROJECTS_CONFIG_PATH.write_text(
            json.dumps(projects_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"created {PROJECTS_CONFIG_PATH}")
