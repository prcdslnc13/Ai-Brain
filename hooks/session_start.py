#!/usr/bin/env python3
"""SessionStart hook — preload the Brain bundle into the session as additionalContext."""

from __future__ import annotations

import sys

from _common import emit, project_basename, read_payload
from brain_mcp import vault
from brain_mcp.brain_prep import render


def main() -> None:
    payload = read_payload()
    project = project_basename(payload)
    try:
        bundle = vault.session_start_bundle(project)
    except Exception as e:
        sys.stderr.write(f"brain session_start: {e}\n")
        sys.exit(0)  # don't block session start

    context = render(bundle)
    if not context.strip():
        sys.exit(0)

    emit({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    })


if __name__ == "__main__":
    main()
