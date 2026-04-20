#!/usr/bin/env python3
"""UserPromptSubmit hook — soft nudge when the user's prompt contains a save signal.

Stateless. Emits `additionalContext` with a single-line reminder to call
`brain_save`. No marker files, no pending-saves dir, no cross-hook state.

Disabled per-install by setting `BRAIN_NUDGE=0`. The stop.py audit column still
records the signal detection either way — disabling the nudge only removes the
injected reminder, not the observability.
"""

from __future__ import annotations

import sys

from _common import emit, read_payload
from _savesig import NUDGE_TEXT, is_save_signal, nudge_enabled


def _prompt_text(payload: dict) -> str:
    # Claude Code sends the raw prompt under `prompt`. Fall back to `user_message`
    # / `message` defensively in case the payload shape shifts.
    for key in ("prompt", "user_message", "message"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def main() -> None:
    if not nudge_enabled():
        sys.exit(0)

    payload = read_payload()
    prompt = _prompt_text(payload)
    if not is_save_signal(prompt):
        sys.exit(0)

    emit({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": NUDGE_TEXT,
        }
    })


if __name__ == "__main__":
    main()
