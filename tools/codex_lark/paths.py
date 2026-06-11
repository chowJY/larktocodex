from __future__ import annotations

from pathlib import Path


BRIDGE_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = Path.cwd()
STATE_DIR = BRIDGE_ROOT / ".lark-events"
PROJECTS_CONFIG_PATH = STATE_DIR / "projects-config.json"
