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
     timestamp account project [sig=Y|N sav=Y|N nud=Y|N pro=Y|N] — snippet
   Columns:
     sig — did the user's last message match a save-signal pattern?
     sav — did the assistant call brain_save/brain_checkpoint this turn?
     nud — was the UserPromptSubmit nudge enabled (and would it have fired)?
     pro — did the assistant's final message contain a save-promise?
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
        elif isinstance(content, str):
            assistant_texts.append(content)

    assistant_text = "\n".join(assistant_texts).strip()
    return last_user_text, assistant_text, brain_tool_count


def _yn(flag: bool) -> str:
    return "Y" if flag else "N"


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

    snippet = last_user.replace("\n", " ")[:80]
    columns = f"[sig={_yn(signal)} sav={_yn(saved)} nud={_yn(nudged)} pro={_yn(promised)}]"
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
