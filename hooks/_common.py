"""Shared helpers for Brain hook scripts.

Each hook script (session_start.py, pre_compact.py, etc.) imports from here.
All hooks read a JSON payload from stdin and may write a JSON object to stdout.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# brain_mcp is installed in the sibling mcp-server/.venv (non-editable). Hooks are launched
# with that venv's python, so brain_mcp imports without any sys.path tricks.


def read_payload() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def project_basename(payload: dict) -> str | None:
    cwd = payload.get("cwd")
    if cwd:
        return Path(cwd).name
    cwd = os.environ.get("CLAUDE_PROJECT_DIR")
    if cwd:
        return Path(cwd).name
    return None


def vault_brain() -> Path:
    """Return the Brain/ directory inside $BRAIN_VAULT.

    The hook command in settings.json must export BRAIN_VAULT before exec'ing the script.
    """
    raw = os.environ.get("BRAIN_VAULT")
    if not raw:
        raise RuntimeError("BRAIN_VAULT is not set; the hook command must export it before launching python.")
    brain = Path(raw).expanduser().resolve() / "Brain"
    brain.mkdir(parents=True, exist_ok=True)
    return brain


def append_activity(line: str) -> None:
    brain = vault_brain()
    activity = brain / "activity.md"
    activity.parent.mkdir(parents=True, exist_ok=True)
    with activity.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


def drop_pending_marker(name: str, body: str) -> Path:
    brain = vault_brain()
    pending = brain / ".pending-saves"
    pending.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    p = pending / f"{stamp}-{name}.md"
    p.write_text(body, encoding="utf-8")
    return p


def list_pending_markers() -> list[Path]:
    pending = vault_brain() / ".pending-saves"
    if not pending.exists():
        return []
    return sorted(pending.glob("*.md"))


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
