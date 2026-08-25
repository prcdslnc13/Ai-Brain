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
    except json.JSONDecodeError as e:
        # Degrade to an empty payload, but say so. An unparseable payload means no
        # `cwd`, which means no project, which means the preload silently drops the
        # project overview and latest checkpoint — a bundle that looks complete and
        # is missing exactly the context the session needed. Costs nothing to say.
        sys.stderr.write(f"brain hook: ignoring unparseable payload ({e}); "
                         f"project context will be missing from this preload\n")
        return {}


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def project_basename(payload: dict) -> str | None:
    """The project key for this session, or None.

    Delegates the sanity rules to `vault.project_basename` rather than keeping a
    second `Path(cwd).name` here: the value is about to be joined into
    `Brain/projects/`, and one predicate deciding what a project name may be is the
    same invariant `vault.is_memory_path` enforces for memories.

    Returns None instead of raising, on every path. A hook that raises loses the
    whole preload — every behavioural rule for that session — which is far worse
    than a session with no project scope.
    """
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR")
    if not cwd:
        return None
    try:
        from brain_mcp import vault
    except Exception:
        # brain_mcp itself is unimportable, so nothing downstream can use the name
        # anyway; session_start.py renders BRAIN_MCP_IMPORT_FAILED for this.
        return None
    return vault.project_basename(cwd)


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
