"""Transcript readers and the structural checkpoint renderer.

This lives in the package rather than in `hooks/` because two very different
callers need the same output format:

* Claude Code's PreCompact / SessionEnd hooks, which read a transcript JSONL
  (`hooks/_checkpoint.py` is now a thin wrapper over this module);
* harnesses with no hook system at all -- `brain checkpoint --from-cherryd`
  reads cherryd's SQLite event log directly, and `brain checkpoint --from-pi`
  reads a pi (pi.dev) session JSONL. The pi extension in `pi/extensions/`
  decides *when* to checkpoint and shells out to that flag rather than
  rendering anything itself, so there is still exactly one renderer.

The second case is why this module exists. A harness that cannot call us back
loses everything when the model hits its context ceiling, and the local-model
sessions where that happens most are exactly the ones least likely to have
remembered to checkpoint themselves. Reading the harness's own on-disk log
after the fact needs no cooperation from the model and no code in the harness.

No LLM call is made here: a checkpoint is a structural extract (user turns,
tool-call histogram, final assistant message). The next session's model
summarizes it when the preload surfaces it.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from . import vault as _vault

_COMMAND_TAG_PREFIXES = ("<local-command-", "<command-")

# How many newest sessions to look at when picking a default one to checkpoint.
_DEFAULT_SCAN_LIMIT = 25


# ---------- Claude Code transcript JSONL ----------

def _is_command_wrapper(text: str) -> bool:
    """Claude Code wraps slash-command input/output in synthetic XML-ish tags
    that arrive as 'user' role entries. A turn made only of these is not a real
    user turn."""
    stripped = text.strip()
    return bool(stripped) and stripped.startswith(_COMMAND_TAG_PREFIXES)


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text" and c.get("text"):
                    parts.append(c["text"])
                elif c.get("type") == "tool_use":
                    parts.append(f"[tool_use: {c.get('name', '?')}]")
                elif c.get("type") == "tool_result":
                    parts.append("[tool_result]")
        return " ".join(parts)
    return ""


def parse_claude_transcript(path: Path) -> dict:
    user_msgs: list[str] = []
    assistant_msgs: list[str] = []
    tool_calls: list[str] = []

    if not path.exists():
        return {"user_msgs": [], "assistant_msgs": [], "tool_calls": []}

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg = obj.get("message") or obj
            role = obj.get("type") or msg.get("role") or obj.get("role")

            if role == "user":
                text = _extract_text(msg.get("content") if isinstance(msg, dict) else msg)
                if text and not text.startswith("[tool_result") and not _is_command_wrapper(text):
                    user_msgs.append(text.strip())
            elif role == "assistant":
                content = msg.get("content") if isinstance(msg, dict) else msg
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict):
                            if c.get("type") == "text" and c.get("text"):
                                assistant_msgs.append(c["text"].strip())
                            elif c.get("type") == "tool_use":
                                tool_calls.append(c.get("name", "?"))
                elif isinstance(content, str):
                    assistant_msgs.append(content.strip())

    return {
        "user_msgs": user_msgs,
        "assistant_msgs": assistant_msgs,
        "tool_calls": tool_calls,
    }


# ---------- cherryd SQLite event log ----------

class CherrydError(RuntimeError):
    """The database is missing, unreadable, or not a cherryd event log."""


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise CherrydError(f"no such database: {db_path}")
    # Read-only URI: the daemon may be mid-write, and a checkpoint run must
    # never be the thing that damages the operator's session history. The path
    # is percent-encoded — a `#`, `?` or `%` in it would otherwise truncate the
    # URI and open some other file (same bug class as embed.index_uri).
    conn = sqlite3.connect(
        "file:" + quote(db_path.as_posix(), safe="/:") + "?mode=ro", uri=True
    )
    conn.row_factory = sqlite3.Row
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.DatabaseError as e:
        raise CherrydError(f"{db_path} is not a readable SQLite database: {e}") from e
    missing = {"sessions", "events", "projects"} - names
    if missing:
        conn.close()
        raise CherrydError(
            f"{db_path} has no {', '.join(sorted(missing))} table(s) -- not a cherryd event log")
    return conn


def cherryd_sessions(db_path: Path) -> list[dict]:
    """Every session in the log, most recently active first."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT s.id AS id,
                   s.created_at AS created_at,
                   p.cwd AS cwd,
                   COUNT(e.id) AS event_count,
                   MAX(e.id) AS last_event_id,
                   MAX(e.ts) AS last_ts
              FROM sessions s
              JOIN projects p ON p.id = s.project_id
              LEFT JOIN events e ON e.session_id = s.id
             GROUP BY s.id
             ORDER BY last_event_id IS NULL, last_event_id DESC, s.id DESC
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def parse_cherryd_session(db_path: Path, session_id: int) -> dict:
    """Same shape parse_claude_transcript returns, plus cherryd metadata.

    Tool *starts* drive the histogram, not tool results: a result row exists
    only when the call completed, and a session killed by a context overflow is
    precisely one where the last calls did not.
    """
    conn = _connect(db_path)
    try:
        meta = conn.execute(
            """
            SELECT s.id AS id, s.created_at AS created_at, p.cwd AS cwd
              FROM sessions s JOIN projects p ON p.id = s.project_id
             WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        if meta is None:
            raise CherrydError(f"no session {session_id} in {db_path}")
        rows = conn.execute(
            "SELECT id, kind, payload, ts FROM events WHERE session_id = ? ORDER BY seq ASC",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()

    user_msgs: list[str] = []
    assistant_msgs: list[str] = []
    tool_calls: list[str] = []
    turns = 0
    last_event_id = 0
    last_ts = None
    for r in rows:
        last_event_id = max(last_event_id, r["id"])
        last_ts = r["ts"] or last_ts
        try:
            payload = json.loads(r["payload"])
        except (json.JSONDecodeError, TypeError):
            continue
        kind = r["kind"] or payload.get("kind")
        if kind == "user_input":
            text = (payload.get("content") or "").strip()
            if text:
                user_msgs.append(text)
        elif kind == "assistant_text":
            text = (payload.get("content") or "").strip()
            if text:
                assistant_msgs.append(text)
        elif kind == "tool_start":
            tool_calls.append(payload.get("tool_name") or "?")
        elif kind == "turn_complete":
            turns += 1

    cwd = meta["cwd"] or ""
    return {
        "user_msgs": user_msgs,
        "assistant_msgs": assistant_msgs,
        "tool_calls": tool_calls,
        "session_id": meta["id"],
        "created_at": meta["created_at"],
        "cwd": cwd,
        "project": Path(cwd).name if cwd else None,
        "turns_completed": turns,
        "last_event_id": last_event_id,
        "last_ts": last_ts,
    }


# ---------- pi (pi.dev) session JSONL ----------

class PiSessionError(RuntimeError):
    """The session file is missing, unreadable, or not a pi session log."""


def _pi_text(content) -> str:
    """pi content is either a bare string or a list of typed blocks."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content
                 if isinstance(c, dict) and c.get("type") == "text" and c.get("text")]
        return " ".join(parts).strip()
    return ""


def pi_session_file(path: Path) -> Path:
    """Resolve a session file, a session *directory*, or pi's sessions root.

    A directory argument picks its newest .jsonl by mtime (recursively), which
    is what a timer or an operator typing the sessions dir actually means.
    """
    path = path.expanduser()
    if path.is_dir():
        candidates = sorted(path.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                            reverse=True)
        if not candidates:
            raise PiSessionError(f"no .jsonl session files under {path}")
        return candidates[0]
    if not path.exists():
        raise PiSessionError(f"no such session file: {path}")
    return path


def parse_pi_session(path: Path) -> dict:
    """Same shape parse_claude_transcript returns, plus pi session metadata.

    pi stores entries as a *tree* (`id`/`parentId`), so the active conversation
    is the parent chain of the last entry in the file, not the file order --
    after a `/tree` navigation the file still holds the abandoned branch. Tool
    *calls* drive the histogram rather than toolResult entries, for the same
    reason as cherryd: a session killed mid-call has the call but no result.
    """
    path = pi_session_file(path)
    header: dict = {}
    entries: dict[str, dict] = {}
    last_id: str | None = None

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "session":
                header = obj
                continue
            eid = obj.get("id")
            if not isinstance(eid, str):
                continue
            entries[eid] = obj
            last_id = eid

    if not header and not entries:
        raise PiSessionError(f"{path} is not a pi session log (no entries)")

    # Walk the parent chain back from the leaf, then reverse into time order.
    branch: list[dict] = []
    seen: set[str] = set()
    cursor = last_id
    while cursor and cursor in entries and cursor not in seen:
        seen.add(cursor)
        entry = entries[cursor]
        branch.append(entry)
        parent = entry.get("parentId")
        cursor = parent if isinstance(parent, str) else None
    branch.reverse()

    user_msgs: list[str] = []
    assistant_msgs: list[str] = []
    tool_calls: list[str] = []
    for entry in branch:
        if entry.get("type") != "message":
            continue
        msg = entry.get("message") or {}
        role = msg.get("role")
        if role == "user":
            text = _pi_text(msg.get("content"))
            if text:
                user_msgs.append(text)
        elif role == "assistant":
            for c in msg.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and c.get("text"):
                    assistant_msgs.append(c["text"].strip())
                elif c.get("type") == "toolCall":
                    tool_calls.append(c.get("name") or "?")
        elif role == "bashExecution":
            # The operator's own `!command`, not a model tool call -- kept in
            # the histogram under a distinct name so the two never blur.
            tool_calls.append("bash!")

    cwd = header.get("cwd") or ""
    return {
        "user_msgs": user_msgs,
        "assistant_msgs": assistant_msgs,
        "tool_calls": tool_calls,
        "session_id": header.get("id"),
        "created_at": header.get("timestamp"),
        "cwd": cwd,
        "project": Path(cwd).name if cwd else None,
        "session_file": str(path),
        "last_entry_id": last_id,
        "entry_count": len(branch),
    }


# ---------- checkpoint rendering ----------

def render_checkpoint(parsed: dict, *, source: str, project: str | None) -> str:
    user_msgs = parsed["user_msgs"]
    assistant_msgs = parsed["assistant_msgs"]
    tool_counts = Counter(parsed["tool_calls"])

    lines: list[str] = []
    lines.append(f"# Session checkpoint ({source})")
    lines.append("")
    lines.append(f"- project: {project or 'unknown'}")
    lines.append(f"- machine: {_vault.machine_name()}")
    lines.append(f"- captured: {datetime.now().isoformat(timespec='seconds')}")
    if parsed.get("cwd"):
        lines.append(f"- cwd: {parsed['cwd']}")
    if parsed.get("created_at"):
        lines.append(f"- session started: {parsed['created_at']}")
    lines.append(f"- user turns: {len(user_msgs)}")
    lines.append(f"- assistant turns: {len(assistant_msgs)}")
    if tool_counts:
        top = ", ".join(f"{n}x{name}" for name, n in tool_counts.most_common(8))
        lines.append(f"- tool calls: {sum(tool_counts.values())} ({top})")
    lines.append("")

    lines.append("## What the user asked for")
    lines.append("")
    for i, msg in enumerate(user_msgs[:8], 1):
        snippet = msg.replace("\n", " ")[:300]
        lines.append(f"{i}. {snippet}")
    if len(user_msgs) > 8:
        lines.append(f"... and {len(user_msgs) - 8} more user turns")
    lines.append("")

    lines.append("## Final assistant message")
    lines.append("")
    if assistant_msgs:
        lines.append(assistant_msgs[-1][:2000])
    lines.append("")

    return "\n".join(lines)


def _worth_checkpointing(parsed: dict) -> bool:
    if len(parsed["user_msgs"]) < 1:
        return False  # nothing meaningful happened
    # user typed but the model never did anything -- not worth a checkpoint
    return bool(parsed["assistant_msgs"] or parsed["tool_calls"])


def write_session_checkpoint(transcript_path: str | None, project: str | None,
                             source: str) -> Path | None:
    """Claude Code hook path: parse a transcript JSONL and write a checkpoint."""
    if not project:
        project = "unknown"
    if not transcript_path:
        return None
    parsed = parse_claude_transcript(Path(transcript_path))
    if not _worth_checkpointing(parsed):
        return None
    body = render_checkpoint(parsed, source=source, project=project)
    return _vault.write_checkpoint(project, body)


# ---------- cherryd checkpoint state (dedup across timer runs) ----------

def _state_path() -> Path:
    return _vault.vault_root() / ".state" / "harness-checkpoints.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Through vault's writer, not a hand-rolled `<name>.tmp`: two harness timers
    # firing at once (a cadence checkpoint and a shutdown checkpoint) shared that
    # fixed temp name and could interleave their JSON into it.
    _vault._atomic_write(p, json.dumps(state, indent=1, sort_keys=True))


def checkpoint_cherryd(db_path: Path, *, session_id: int | None = None,
                       project: str | None = None, all_sessions: bool = False,
                       force: bool = False) -> list[dict]:
    """Write a checkpoint for one or all cherryd sessions that have new activity.

    Returns one result dict per session considered, each carrying `written`
    (the checkpoint path, or None) and a `reason` when skipped -- a timer wants
    a quiet no-op, not an error, when nothing has happened since the last run.
    """
    sessions = cherryd_sessions(db_path)
    if not sessions:
        raise CherrydError(f"no sessions in {db_path}")
    parsed_cache: dict[int, dict] = {}
    if session_id is not None:
        sessions = [s for s in sessions if s["id"] == session_id]
        if not sessions:
            raise CherrydError(f"no session {session_id} in {db_path}")
    elif not all_sessions:
        # Newest-first, but skip past sessions with nothing in them yet: opening
        # a fresh session in the TUI must not shadow the long-running one that
        # actually holds the work. Bounded so a big log stays cheap to scan.
        chosen = sessions[0]
        for s in sessions[:_DEFAULT_SCAN_LIMIT]:
            parsed_cache[s["id"]] = parse_cherryd_session(db_path, s["id"])
            if _worth_checkpointing(parsed_cache[s["id"]]):
                chosen = s
                break
        sessions = [chosen]

    state = _load_state()
    db_key = str(db_path.resolve())
    results: list[dict] = []
    for s in sessions:
        key = f"{db_key}::{s['id']}"
        parsed = parsed_cache.get(s["id"]) or parse_cherryd_session(db_path, s["id"])
        result = {
            "session_id": s["id"],
            "project": project or parsed["project"] or "unknown",
            "last_event_id": parsed["last_event_id"],
            "written": None,
            "reason": None,
        }
        if not force and state.get(key) == parsed["last_event_id"]:
            result["reason"] = "no new events since last checkpoint"
        elif not _worth_checkpointing(parsed):
            result["reason"] = "no completed exchange yet"
        else:
            body = render_checkpoint(parsed, source=f"cherryd:session-{s['id']}",
                                     project=result["project"])
            result["written"] = _vault.write_checkpoint(result["project"], body)
            state[key] = parsed["last_event_id"]
        results.append(result)
    _save_state(state)
    return results


def checkpoint_pi(session_path: Path, *, project: str | None = None,
                  source: str = "pi", force: bool = False) -> dict:
    """Write a checkpoint for one pi session, skipping when nothing is new.

    Shares `harness-checkpoints.json` with the cherryd path: both are the same
    problem (a timer or a lifecycle event firing more often than work happens),
    and one state file keeps that answer in one place. The dedup key is the
    leaf entry id -- pi appends entries, so an unchanged leaf means an
    unchanged conversation.
    """
    parsed = parse_pi_session(session_path)
    state = _load_state()
    key = f"pi::{Path(parsed['session_file']).resolve()}"
    result = {
        "session_id": parsed["session_id"],
        "session_file": parsed["session_file"],
        "source": source,
        "project": project or parsed["project"] or "unknown",
        "last_entry_id": parsed["last_entry_id"],
        "written": None,
        "reason": None,
    }
    if not force and state.get(key) == parsed["last_entry_id"]:
        result["reason"] = "no new entries since last checkpoint"
        return result
    if not _worth_checkpointing(parsed):
        result["reason"] = "no completed exchange yet"
        return result
    body = render_checkpoint(parsed, source=source, project=result["project"])
    result["written"] = _vault.write_checkpoint(result["project"], body)
    state[key] = parsed["last_entry_id"]
    _save_state(state)
    return result
