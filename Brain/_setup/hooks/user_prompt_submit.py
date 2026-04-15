#!/usr/bin/env python3
"""UserPromptSubmit hook — surface pending save markers to the next model turn."""

from __future__ import annotations

import sys

from _common import emit, list_pending_markers, read_payload


def main() -> None:
    _ = read_payload()
    markers = list_pending_markers()
    if not markers:
        sys.exit(0)

    lines = [
        "**Pending memory save signals detected by Stop hook:**",
        "",
    ]
    for m in markers:
        lines.append(f"- {m.name}")
    lines.append("")
    lines.append(
        "Read these marker files, decide what (if anything) is worth a brain_save call, "
        "save the worthwhile ones, and then delete the marker files."
    )

    emit({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(lines),
        }
    })


if __name__ == "__main__":
    main()
