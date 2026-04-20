#!/usr/bin/env python3
"""Stop hook — append an audited one-line breadcrumb to Brain/activity.md.

Each line records:
  - timestamp, account, project, user-message snippet
  - `sig=Y|N` — did the user's last message match a save-signal pattern?
  - `sav=Y|N` — did the assistant call brain_save/brain_checkpoint this turn?
  - `nud=Y|N` — was the UserPromptSubmit nudge enabled and would it have fired?

The doctor `_check_save_gap` check reads recent lines to spot "signal with no
save" patterns. No LLM calls. No marker files. No pending-saves backlog.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from _common import (
    append_activity,
    now_stamp,
    project_basename,
    read_payload,
)
from _savesig import is_save_signal, nudge_enabled

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


def _analyze_last_turn(transcript_path: str | None) -> tuple[str, int]:
    """Return (last_user_message, brain_tool_calls_since_last_user).

    A "turn" is everything after the most recent user message. We count
    assistant tool_use blocks whose name is in BRAIN_SAVE_TOOL_NAMES.
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

    brain_tool_count = 0
    for obj in entries[last_user_idx + 1:]:
        role = obj.get("type") or (obj.get("message") or {}).get("role") or obj.get("role")
        if role != "assistant":
            continue
        msg = obj.get("message") or obj
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                name = c.get("name", "")
                if name in BRAIN_SAVE_TOOL_NAMES:
                    brain_tool_count += 1

    return last_user_text, brain_tool_count


def _yn(flag: bool) -> str:
    return "Y" if flag else "N"


def main() -> None:
    payload = read_payload()
    project = project_basename(payload) or "unknown"
    account = os.environ.get("BRAIN_ACCOUNT", "claude")
    transcript = payload.get("transcript_path")

    last_msg, brain_tool_count = _analyze_last_turn(transcript)
    signal = is_save_signal(last_msg)
    saved = brain_tool_count > 0
    nudged = signal and nudge_enabled()

    snippet = last_msg.replace("\n", " ")[:80]
    columns = f"[sig={_yn(signal)} sav={_yn(saved)} nud={_yn(nudged)}]"

    try:
        append_activity(f"{now_stamp()} {account} {project} {columns} — {snippet}")
    except Exception as e:
        sys.stderr.write(f"brain stop: {e}\n")

    sys.exit(0)


if __name__ == "__main__":
    main()
