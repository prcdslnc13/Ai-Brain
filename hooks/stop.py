#!/usr/bin/env python3
"""Stop hook — gate unfulfilled save promises, then append an audit breadcrumb.

Two jobs, in order:

1. **Gate** (BRAIN_STOP_GATE, default on): if the assistant's final message
   contains a save-promise phrase ("I'll save this to brain", "checkpointing
   now", …) and no brain_save/brain_checkpoint tool call occurred in this turn,
   emit `{decision: "block", reason: …}` so Claude Code feeds the reason back
   to the model and it has to either fulfill the commitment or recant. The
   triggering incident (2026-04-22): a session said it was "recording
   verification steps to brain" then never did, and the window died before a
   safety-net checkpoint fired — ~70 minutes of migration work lost.

2. **Audit**: append a one-line breadcrumb to Brain/activity.md:
     timestamp account project [sig=Y|N sav=Y|N nud=Y|N pro=Y|N too=Y|N sys=Y|N re=Y|N] — snippet
   Columns:
     sig — did the user's last message match a save-signal pattern?
     sav — did a brain save happen this turn? Read from the save-event record
           (`vault.count_save_events`): every model-facing save surface — the
           `brain save` / `brain checkpoint` CLI however it is invoked, and the
           brain_save / brain_checkpoint MCP tools — appends a line to
           Brain/.state/save-events.jsonl when the write lands, and an event at
           or after this turn's user message, for this session, is a save. A
           brain_save/brain_checkpoint tool_use block in the transcript also
           counts (structured, no parsing). The hook does NOT inspect shell
           commands: the regex that used to was patched four times for the
           same predicate and still missed the `brain-agent.py` launcher every
           install has emitted since 2026-09-01, so every real save logged
           sav=N and the gate blocked turns that had saved (2026-09-13).
     nud — was the UserPromptSubmit nudge enabled (and would it have fired)?
     pro — did the assistant's final message contain a save-promise?
     too — was a brain save interface available this session (MCP server
           registered OR the `brain` CLI installed in the repo venv)? A
           `too=N` row means a save-promise was physically unsatisfiable (an
           infra failure, not a model bug), so the gap checks skip it. See the
           2026-06-03 PROMISE_GAP false positive: the very session
           troubleshooting an unregistered brain promised a save the gate then
           demanded, but there was no tool to call.
     sys — was the turn's "user message" system-generated (task notification,
           skill/command expansion, local-command output) rather than typed by
           the user? Such text can contain arbitrary phrases (skill bodies
           match save-signal patterns), so SAVE_GAP skips sys=Y rows — sig
           measured on them says nothing about the user. PROMISE_GAP still
           counts them: pro measures *assistant* text, which is genuinely
           model-authored whatever triggered the turn, and the gate applies
           on sys=Y turns too. Found 2026-07-28: ~9% of rows were
           notification turns, and a skill expansion scored a false sig=Y.
           The prefix list is `brain_mcp.transcript.SYSTEM_TURN_PREFIXES`,
           shared with the checkpoint renderer.
     re  — is this Stop a *re-entry* after the gate blocked (payload
           `stop_hook_active=true`)? The re-entry's assistant text still spans
           the whole turn, original promise included, so `pro` is Y whether the
           model then saved or recanted. Doctor treats a re=Y row as the
           outcome of the row before it, never as a fresh unfulfilled promise
           (2026-09-01: every gate block used to produce a PROMISE_GAP warning
           at the next SessionStart, for a promise the gate had already made
           the model resolve).
   `brain_doctor._check_save_gap` and `_check_promise_gap` read the tail of
   activity.md to surface long-run gaps.

No LLM calls. No pending-saves backlog — the save-event file is a record of
saves that already happened, written by the saver, and nothing here acts on it
beyond the audit column and the gate. `stop_hook_active` in the payload signals
we were re-entered after a previous block — skip the gate in that case to avoid
an infinite loop (the audit column still fires, tagged re=Y, so brain_doctor
can see the outcome).

The transcript is read from the END, not the start: the hook has a 5 s budget
(templates/settings.hooks*.json) and a session transcript grows without bound
— 13 MB of it took 0.11 s to parse whole, linearly, so a long session with big
tool outputs crossed the budget, after which there was no gate and no audit
row for the rest of the session. Only the last turn is ever parsed now.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from _common import (
    append_activity,
    emit,
    now_stamp,
    project_basename,
    read_payload,
)
from _savesig import (
    GATE_BLOCK_REASON,
    gate_enabled,
    is_save_promise,
    is_save_signal,
    nudge_enabled,
)

try:
    from brain_mcp.transcript import is_system_turn, user_authored_text
except Exception:  # pragma: no cover — venv broken; the audit still runs, untagged
    def is_system_turn(text: str) -> bool:  # type: ignore[misc]
        return False

    def user_authored_text(text: str) -> str:  # type: ignore[misc]
        return text

try:
    from brain_mcp.vault import count_save_events
except Exception:  # pragma: no cover — venv broken: nothing could have saved either
    def count_save_events(*, since: float, session_id: str | None = None) -> int:  # type: ignore[misc]
        return 0

BRAIN_SAVE_TOOL_NAMES = {
    "brain_save",
    "brain_checkpoint",
    "mcp__brain__brain_save",
    "mcp__brain__brain_checkpoint",
}

# ---- transcript --------------------------------------------------------------

def _message_text(msg) -> str:
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            return " ".join(p for p in parts if p)
        if isinstance(content, str):
            return content
    return ""


def _role(obj: dict) -> str | None:
    return obj.get("type") or (obj.get("message") or {}).get("role") or obj.get("role")


# Cheap byte-level pre-filters so a line is only json-parsed when it can matter.
# Claude Code writes compact JSON, but the `\s*` tolerates a pretty-printed
# transcript too. A file-history snapshot or a progress entry matches neither
# and costs one regex scan instead of a parse.
_USER_HINT_RE = re.compile(rb'"(?:type|role)"\s*:\s*"user"')
_ASSISTANT_HINT_RE = re.compile(rb'"(?:type|role)"\s*:\s*"assistant"')

_TAIL_BLOCK = 64 * 1024


def _iter_lines_backwards(path: Path, block: int = _TAIL_BLOCK):
    """Yield the file's lines as bytes, last line first, reading in blocks
    from the end so the cost of reaching the last turn is the size of that
    turn, not of the session."""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        buf = b""
        while pos > 0:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            lines = buf.split(b"\n")
            buf = lines[0]
            for line in reversed(lines[1:]):
                yield line
        if buf:
            yield buf


def _loads(raw: bytes) -> dict | None:
    try:
        obj = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _parse_timestamp(value) -> float | None:
    """Claude Code stamps every transcript entry with an ISO-8601 UTC time
    (`2026-09-13T21:52:01.131Z`). Epoch seconds, or None when absent/odd."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _analyze_last_turn(transcript_path: str | None) -> tuple[str, str, int, float | None]:
    """Return (last_user_text, assistant_text_since, brain_tool_calls_since, turn_start).

    A "turn" is everything after the most recent user message that carries
    text (a tool_result-only user entry is not a turn boundary):
      - assistant_text = concatenated text from every assistant message in the
        turn (there may be multiple if tool calls interleaved)
      - brain_tool_calls = count of tool_use blocks whose name is in
        BRAIN_SAVE_TOOL_NAMES (the MCP tools; structured, no parsing)
      - turn_start = that user message's timestamp as epoch seconds, or None
        when the entry carries none. Saves made through the CLI are found by
        `count_save_events(since=turn_start, …)`, not by reading commands.

    Walks the transcript backwards and stops at that user message; only the
    lines after it are ever parsed. An unreadable file is reported on stderr
    and treated as an empty turn — the old blanket `except Exception: return`
    silently evaluated a truncated, stale turn instead.
    """
    if not transcript_path:
        return "", "", 0, None
    p = Path(transcript_path)
    if not p.exists():
        return "", "", 0, None

    last_user_text = ""
    turn_start: float | None = None
    tail: list[bytes] = []  # lines after the last user turn, newest first
    try:
        for raw in _iter_lines_backwards(p):
            raw = raw.strip()
            if not raw:
                continue
            if _USER_HINT_RE.search(raw):
                obj = _loads(raw)
                if obj is not None and _role(obj) == "user":
                    text = _message_text(obj.get("message") or obj)
                    if text.strip():
                        last_user_text = text.strip()
                        turn_start = _parse_timestamp(obj.get("timestamp"))
                        break
            tail.append(raw)
    except OSError as e:
        sys.stderr.write(f"brain stop: cannot read transcript {p}: {e}\n")
        return "", "", 0, None

    assistant_texts: list[str] = []
    brain_tool_count = 0
    for raw in reversed(tail):
        if not _ASSISTANT_HINT_RE.search(raw):
            continue
        obj = _loads(raw)
        if obj is None or _role(obj) != "assistant":
            continue
        msg = obj.get("message") or obj
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    continue
                ctype = c.get("type")
                if ctype == "text":
                    t = c.get("text", "")
                    if t:
                        assistant_texts.append(t)
                elif ctype == "tool_use":
                    if c.get("name", "") in BRAIN_SAVE_TOOL_NAMES:
                        brain_tool_count += 1
        elif isinstance(content, str):
            assistant_texts.append(content)

    assistant_text = "\n".join(assistant_texts).strip()
    return last_user_text, assistant_text, brain_tool_count, turn_start


def saved_this_turn(brain_tool_count: int, turn_start: float | None,
                    session_id: str | None) -> bool:
    """A brain_save/brain_checkpoint tool_use in the turn, or a save event
    recorded at or after the turn began for this session. With no turn start
    (an entry with no timestamp) only the structured tool_use count applies —
    a guess from "recent" events could credit the previous turn's save."""
    if brain_tool_count > 0:
        return True
    if turn_start is None:
        return False
    return count_save_events(since=turn_start, session_id=session_id) > 0


# ---- audit -------------------------------------------------------------------

def _yn(flag: bool) -> str:
    return "Y" if flag else "N"


def _active_config_files() -> list[Path]:
    """Candidate `.claude.json` files for the config dir this session runs under.

    Claude Code reads user-scope MCP registrations from a `.claude.json` keyed
    to its config dir. The default dir (~/.claude) keeps that file at
    ~/.claude.json (home), NOT inside the dir; a custom CLAUDE_CONFIG_DIR keeps a
    sibling `.claude.json` inside it. We check whichever applies plus the home
    file, and treat "brain registered in any of them" as callable.
    """
    files: list[Path] = []
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg_dir:
        files.append(Path(cfg_dir).expanduser() / ".claude.json")
    files.append(Path.home() / ".claude.json")
    return files


def _cli_available() -> bool:
    """Is the `brain` console script installed in the repo venv?

    The hooks live in the repo, so the venv is a fixed relative location. With
    the CLI installed, a save-promise is always satisfiable via the Bash tool
    even when no MCP server is registered.
    """
    venv = Path(__file__).resolve().parent.parent / "mcp-server" / ".venv"
    return (venv / "Scripts" / "brain.exe").exists() or (venv / "bin" / "brain").exists()


def brain_tools_callable() -> bool:
    """Best-effort: could this session actually perform a brain save?

    True when the brain MCP server is registered for this session OR the
    `brain` CLI is installed (callable through the Bash tool regardless of MCP
    registration). Used to annotate the audit breadcrumb so the promise-/
    save-gap checks can ignore sessions where no save interface existed — a
    save-promise there is physically unsatisfiable, an infra failure rather
    than a model bug. Conservative on uncertainty: any unreadable or
    unparseable config returns True so a real unfulfilled promise is never
    hidden.
    """
    if _cli_available():
        return True
    for f in _active_config_files():
        try:
            if not f.exists():
                continue
            cfg = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True  # can't tell → assume available, don't suppress
        if "brain" in (cfg.get("mcpServers") or {}):
            return True
    return False


def main() -> None:
    payload = read_payload()
    project = project_basename(payload) or "unknown"
    account = os.environ.get("BRAIN_ACCOUNT", "claude")
    transcript = payload.get("transcript_path")
    stop_active = bool(payload.get("stop_hook_active"))

    last_user, assistant_text, brain_tool_count, turn_start = _analyze_last_turn(transcript)
    # Only what the user typed: a system-reminder block prepended to the prompt,
    # or a whole system-generated turn, is not the user stating a preference.
    signal = is_save_signal(user_authored_text(last_user))
    saved = saved_this_turn(brain_tool_count, turn_start, payload.get("session_id"))
    promised = is_save_promise(assistant_text)
    nudged = signal and nudge_enabled()

    tools_ok = brain_tools_callable()
    system_turn = is_system_turn(last_user)

    snippet = last_user.replace("\n", " ")[:80]
    columns = (
        f"[sig={_yn(signal)} sav={_yn(saved)} nud={_yn(nudged)} "
        f"pro={_yn(promised)} too={_yn(tools_ok)} sys={_yn(system_turn)} "
        f"re={_yn(stop_active)}]"
    )
    try:
        append_activity(f"{now_stamp()} {account} {project} {columns} — {snippet}")
    except Exception as e:
        sys.stderr.write(f"brain stop: {e}\n")

    if promised and not saved and gate_enabled() and not stop_active:
        emit({"decision": "block", "reason": GATE_BLOCK_REASON})
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
