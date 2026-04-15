#!/usr/bin/env python3
"""Stop hook — append a one-line breadcrumb and detect save signal phrases.

Runs after every assistant turn. Cheap. No LLM calls.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from _common import (
    append_activity,
    drop_pending_marker,
    now_stamp,
    project_basename,
    read_payload,
    vault_brain,
)

SAVE_SIGNAL_PATTERNS = [
    r"\bremember\b",
    r"\bfrom now on\b",
    r"\bnext time\b",
    r"\bdon'?t forget\b",
    r"\bi prefer\b",
    r"\bi like\b.*\bbetter\b",
    r"\balways\b.*\bdo\b",
    r"\bnever\b.*\bdo\b",
    r"\bstop doing\b",
    r"\bgoing forward\b",
]


def last_user_message(transcript_path: str | None) -> str | None:
    if not transcript_path:
        return None
    p = Path(transcript_path)
    if not p.exists():
        return None
    last_user = None
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "user" or obj.get("role") == "user":
                    msg = obj.get("message") or obj.get("content") or obj
                    if isinstance(msg, dict):
                        content = msg.get("content")
                        if isinstance(content, list):
                            text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                            text = " ".join(p for p in text_parts if p)
                        elif isinstance(content, str):
                            text = content
                        else:
                            text = ""
                    elif isinstance(msg, str):
                        text = msg
                    else:
                        text = ""
                    if text.strip():
                        last_user = text.strip()
    except Exception:
        return None
    return last_user


def detect_save_signal(text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in SAVE_SIGNAL_PATTERNS)


def main() -> None:
    payload = read_payload()
    project = project_basename(payload) or "unknown"
    account = os.environ.get("BRAIN_ACCOUNT", "claude")
    transcript = payload.get("transcript_path")

    last_msg = last_user_message(transcript) or ""
    snippet = last_msg.replace("\n", " ")[:80]
    append_activity(f"{now_stamp()} {account} {project} — {snippet}")

    if last_msg and detect_save_signal(last_msg):
        drop_pending_marker(
            name=f"{project}-{account}",
            body=(
                "---\n"
                f"detected_at: {now_stamp()}\n"
                f"project: {project}\n"
                f"account: {account}\n"
                "type: save-signal\n"
                "---\n\n"
                f"User message contained a save signal:\n\n> {last_msg}\n"
            ),
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
