#!/usr/bin/env python3
"""Stop hook — append a one-line breadcrumb to Brain/activity.md after every turn.

Cheap, no LLM calls, no regex-based save-signal detection. Proactive saves are driven
entirely by the directives in templates/global-CLAUDE.md.
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


def main() -> None:
    payload = read_payload()
    project = project_basename(payload) or "unknown"
    account = os.environ.get("BRAIN_ACCOUNT", "claude")
    transcript = payload.get("transcript_path")

    last_msg = last_user_message(transcript) or ""
    snippet = last_msg.replace("\n", " ")[:80]
    try:
        append_activity(f"{now_stamp()} {account} {project} — {snippet}")
    except Exception as e:
        sys.stderr.write(f"brain stop: {e}\n")

    sys.exit(0)


if __name__ == "__main__":
    main()
