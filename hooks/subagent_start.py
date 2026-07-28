#!/usr/bin/env python3
"""SubagentStart hook — inject a slim Brain bundle into subagents.

The SessionStart preload reaches only the main session, and Claude 5-era models
delegate to subagents far more readily than their predecessors — so delegated
work would otherwise run without the user's profile and feedback rules. This
hook injects the slim bundle: index + user + feedback, but NO project overview
or session checkpoint (the delegating agent passes task context in the subagent
prompt, and per-subagent token cost matters).

No doctor run and no overview-stub logic here — those are once-per-session
jobs that belong to session_start.py.

Knobs: BRAIN_SUBAGENT_PRELOAD=0 disables the injection entirely;
BRAIN_SUBAGENT_BUDGET_KB (default 12) caps the bundle, reusing the
BRAIN_BUNDLE_BUDGET_KB mechanism in vault.session_start_bundle.
"""

from __future__ import annotations

import os
import sys

from _common import debug_payload, emit, read_payload


def main() -> None:
    payload = read_payload()
    debug_payload("subagent_start", payload)

    if os.environ.get("BRAIN_SUBAGENT_PRELOAD", "1") == "0":
        sys.exit(0)

    budget = os.environ.get("BRAIN_SUBAGENT_BUDGET_KB", "12")
    os.environ["BRAIN_BUNDLE_BUDGET_KB"] = budget

    try:
        from brain_mcp import vault
        from brain_mcp.brain_prep import render
        bundle = vault.session_start_bundle(None)
        context = render(bundle)
    except Exception as e:
        # A subagent without Brain context is degraded, not broken — never
        # block or noise up the subagent over a preload failure.
        sys.stderr.write(f"brain subagent_start: {e}\n")
        sys.exit(0)

    if not context.strip():
        sys.exit(0)

    emit({
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": context,
        }
    })


if __name__ == "__main__":
    main()
