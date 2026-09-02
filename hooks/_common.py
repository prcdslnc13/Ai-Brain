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

# activity.md is rewritten on every turn on every machine and then propagated by
# Obsidian Sync, and nothing else bounded it (243 KB / 1,860 lines on 2026-09-01).
# doctor's tail reader only ever needs the last SAVE_GAP_WINDOW (30) rows, so once
# the file passes the high-water mark it is cut back to the newest rows.
ACTIVITY_MAX_LINES = 2000
ACTIVITY_KEEP_LINES = 1500


def force_utf8_stdio() -> None:
    """Reconfigure stdin/stdout/stderr to UTF-8 before any hook I/O.

    Claude Code hands the hook a UTF-8 JSON payload, but a Python subprocess on
    Windows decodes a pipe with the locale codepage (cp1252) unless told
    otherwise, and the launcher sets only BRAIN_VAULT. The 2026-07-29 UTF-8 fix
    covered `cli.py` and never reached the hooks, so a cwd of `D:/tmp/Café—x`
    arrived as `CafÃ©â€”x` — a name `validate_project_name` accepts (it is a
    blacklist by design), so the overview stub, every checkpoint and every
    project-scoped feedback landed in a mojibake project directory while the
    real project's memories never preloaded. A Cyrillic cwd was worse: cp1252
    has undefined bytes, so `sys.stdin.read()` raised and the hook died before
    it could emit anything, dropping the whole preload.

    Reuses the package helper when it is importable and degrades to the same
    reconfigure inline when it is not: this must run *before* the payload is
    read, and a hook whose venv is broken still has to read its payload.
    """
    try:
        from brain_mcp._console import force_utf8_stdio as _force
    except Exception:
        _force = None
    if _force is not None:
        _force(include_stdin=True)
        return
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def read_payload() -> dict:
    force_utf8_stdio()
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
    """Return the Brain/ directory inside $BRAIN_VAULT, without creating it.

    The hook command in settings.json must export BRAIN_VAULT before exec'ing the
    script. This used to `mkdir(parents=True)`, which meant a mistyped or
    not-yet-synced BRAIN_VAULT materialised a phantom vault on the first Stop —
    and once `<vault>/Brain/` exists, doctor's `BRAIN_DIR_MISSING` can no longer
    fire, so the misconfiguration that produced the phantom went unreported
    forever. Only the vault's own writers (`brain save`, checkpoints) may create
    directories, and they create them *inside* a Brain/ that already exists.
    """
    raw = os.environ.get("BRAIN_VAULT")
    if not raw:
        raise RuntimeError("BRAIN_VAULT is not set; the hook command must export it before launching python.")
    return Path(raw).expanduser().resolve() / "Brain"


def append_activity(line: str) -> None:
    """Append one audit row to Brain/activity.md, rotating when it grows too long.

    Writes nothing — and says so on stderr — when the Brain directory does not
    exist: creating it here would hide a wrong BRAIN_VAULT from doctor (see
    `vault_brain`).
    """
    brain = vault_brain()
    if not brain.is_dir():
        sys.stderr.write(f"brain hook: {brain} does not exist; not writing activity.md "
                         f"(check BRAIN_VAULT, or wait for the vault to sync)\n")
        return
    activity = brain / "activity.md"
    with activity.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")
    _rotate_activity(activity)


def _rotate_activity(activity: Path) -> None:
    """Keep activity.md to its newest ACTIVITY_KEEP_LINES once it exceeds
    ACTIVITY_MAX_LINES. Same-directory temp file + os.replace, so a crash mid-
    rotation leaves either the old file or the new one, never a truncated one."""
    try:
        data = activity.read_bytes()
    except OSError:
        return
    if data.count(b"\n") <= ACTIVITY_MAX_LINES:
        return
    lines = data.splitlines(keepends=True)
    kept = b"".join(lines[-ACTIVITY_KEEP_LINES:])
    tmp = activity.with_name(f"{activity.name}.{os.getpid()}.rotating")
    try:
        tmp.write_bytes(kept)
        os.replace(tmp, activity)
    except OSError as e:
        sys.stderr.write(f"brain hook: could not rotate {activity}: {e}\n")
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
