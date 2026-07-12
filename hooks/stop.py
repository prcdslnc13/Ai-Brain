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
     timestamp account project [sig=Y|N sav=Y|N nud=Y|N pro=Y|N too=Y|N] — snippet
   Columns:
     sig — did the user's last message match a save-signal pattern?
     sav — did a brain save happen this turn? Counts both the MCP tools
           (brain_save/brain_checkpoint) and a Bash/PowerShell invocation of
           the `brain save` / `brain checkpoint` CLI.
     nud — was the UserPromptSubmit nudge enabled (and would it have fired)?
     pro — did the assistant's final message contain a save-promise?
     too — was a brain save interface available this session (MCP server
           registered OR the `brain` CLI installed in the repo venv)? A
           `too=N` row means a save-promise was physically unsatisfiable (an
           infra failure, not a model bug), so the gap checks skip it. See the
           2026-06-03 PROMISE_GAP false positive: the very session
           troubleshooting an unregistered brain promised a save the gate then
           demanded, but there was no tool to call.
   `brain_doctor._check_save_gap` and `_check_promise_gap` read the tail of
   activity.md to surface long-run gaps.

No LLM calls. No marker files. No pending-saves backlog. `stop_hook_active` in
the payload signals we were re-entered after a previous block — skip the gate
in that case to avoid an infinite loop (the audit column still fires, so
brain_doctor can see the miss).
"""

from __future__ import annotations

import json
import os
import re
import sys
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

BRAIN_SAVE_TOOL_NAMES = {
    "brain_save",
    "brain_checkpoint",
    "mcp__brain__brain_save",
    "mcp__brain__brain_checkpoint",
}

# Shell tools whose commands can invoke the `brain` CLI. Since the CLI became
# the primary interface (MCP registration is opt-in), a save can be fulfilled
# by running `<...>brain save ...` / `<...>brain checkpoint ...` through
# Bash/PowerShell instead of calling an MCP tool.
SHELL_TOOL_NAMES = {"Bash", "PowerShell"}

# Matches the brain CLI (bare, path-prefixed, .exe/.cmd, optionally the whole
# path in quotes) followed by a save/checkpoint subcommand. Deliberately does
# NOT match the phrase inside quoted arguments (e.g. `git commit -m "brain
# save fix"`): the quoted alternative requires the closing quote immediately
# after the executable name, and the bare alternative rejects a quote directly
# before `brain`.
_CLI_SAVE_RE = re.compile(
    r"""(?:^|[;&|]\s*|\s)                                  # command start or separator
        (?:
            ["'](?:[A-Za-z]:)?[^"'\n]*brain(?:\.exe|\.cmd)?["']  # quoted path
          | (?:[A-Za-z]:)?[\w~./\\-]*brain(?:\.exe|\.cmd)?       # bare path
        )
        \s+(?:save|checkpoint)\b""",
    re.IGNORECASE | re.VERBOSE,
)


def is_cli_save_command(command: str) -> bool:
    return bool(command) and bool(_CLI_SAVE_RE.search(command))


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


def _iter_transcript(transcript_path: str | None):
    if not transcript_path:
        return
    p = Path(transcript_path)
    if not p.exists():
        return
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except Exception:
        return


def _analyze_last_turn(transcript_path: str | None) -> tuple[str, str, int]:
    """Return (last_user_text, assistant_text_since, brain_tool_calls_since).

    A "turn" is everything after the most recent user message:
      - assistant_text = concatenated text from every assistant message in the
        turn (there may be multiple if tool calls interleaved)
      - brain_tool_calls = count of tool_use blocks whose name is in
        BRAIN_SAVE_TOOL_NAMES
    """
    entries = list(_iter_transcript(transcript_path))
    last_user_idx = -1
    last_user_text = ""
    for i, obj in enumerate(entries):
        role = obj.get("type") or (obj.get("message") or {}).get("role") or obj.get("role")
        if role == "user":
            msg = obj.get("message") or obj
            text = _message_text(msg)
            if text.strip():
                last_user_idx = i
                last_user_text = text.strip()

    assistant_texts: list[str] = []
    brain_tool_count = 0
    for obj in entries[last_user_idx + 1:]:
        role = obj.get("type") or (obj.get("message") or {}).get("role") or obj.get("role")
        if role != "assistant":
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
                    name = c.get("name", "")
                    if name in BRAIN_SAVE_TOOL_NAMES:
                        brain_tool_count += 1
                    elif name in SHELL_TOOL_NAMES:
                        cmd = (c.get("input") or {}).get("command", "")
                        if is_cli_save_command(cmd):
                            brain_tool_count += 1
        elif isinstance(content, str):
            assistant_texts.append(content)

    assistant_text = "\n".join(assistant_texts).strip()
    return last_user_text, assistant_text, brain_tool_count


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

    last_user, assistant_text, brain_tool_count = _analyze_last_turn(transcript)
    signal = is_save_signal(last_user)
    saved = brain_tool_count > 0
    promised = is_save_promise(assistant_text)
    nudged = signal and nudge_enabled()

    tools_ok = brain_tools_callable()

    snippet = last_user.replace("\n", " ")[:80]
    columns = (
        f"[sig={_yn(signal)} sav={_yn(saved)} nud={_yn(nudged)} "
        f"pro={_yn(promised)} too={_yn(tools_ok)}]"
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
