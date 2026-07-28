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


# Phase 0 instrumentation (2026-07-28) — remove after the subagent-stop and
# compact-source verification for the Fable 5 integration plan. Logs one JSON
# line per hook invocation so we can see which events fire, with what payload
# fields (agent_id/agent_type on subagent stops, source on SessionStart).
# Disable with BRAIN_HOOK_DEBUG=0.
def debug_payload(hook_name: str, payload: dict) -> None:
    if os.environ.get("BRAIN_HOOK_DEBUG", "1") == "0":
        return
    try:
        log = Path.home() / ".cache" / "ai-brain" / "hook-payload-debug.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "hook": hook_name,
            "event": payload.get("hook_event_name"),
            "source": payload.get("source"),
            "agent_id": payload.get("agent_id"),
            "agent_type": payload.get("agent_type"),
            "stop_hook_active": payload.get("stop_hook_active"),
            "session_id": (payload.get("session_id") or "")[:8],
            "keys": sorted(payload.keys()),
        }
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # instrumentation must never break a hook


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


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
