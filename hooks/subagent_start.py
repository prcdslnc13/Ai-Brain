#!/usr/bin/env python3
"""SubagentStart hook — inject a slim Brain bundle into subagents.

The SessionStart preload reaches only the main session, and Claude 5-era models
delegate to subagents far more readily than their predecessors — so delegated
work would otherwise run without the user's profile and feedback rules. This
hook injects the slim bundle: index + user + feedback (global + the current
project's scoped feedback), but NO project overview or session checkpoint (the
delegating agent passes task context in the subagent prompt, and per-subagent
token cost matters).

No doctor run and no overview-stub logic here — those are once-per-session
jobs that belong to session_start.py.

Knobs: BRAIN_SUBAGENT_PRELOAD=0 disables the injection entirely;
BRAIN_SUBAGENT_BUDGET_KB (default vault.SUBAGENT_BUDGET_DEFAULT_KB, 56) caps the
bundle via the budget_kb parameter of vault.session_start_bundle.

The default was 12 KB until 2026-07-30, which was self-defeating: the bundle fills
with `user/` before it reaches `feedback/`, so a 12 KB cap delivered 11 user entries
and **zero** feedback — the behavioral rules this hook exists to propagate. 44 KB fit
the whole of user + feedback with headroom at the time — but the corpus grows, and by
2026-08-06 it had re-saturated (3 feedback rules silently dropped from every subagent).
`brain doctor` now sizes the bundle at this budget too (SUBAGENT_BUNDLE_SATURATED), so
the drop is at least visible. If you lower the budget, lower it knowing feedback is
what gets dropped first. The default lives in `vault.SUBAGENT_BUDGET_DEFAULT_KB` so the
hook and doctor can never disagree about it.
"""

from __future__ import annotations

import os
import sys

from _common import emit, project_basename, read_payload


def main() -> None:
    payload = read_payload()

    if os.environ.get("BRAIN_SUBAGENT_PRELOAD", "1") == "0":
        sys.exit(0)

    try:
        from brain_mcp import vault
        from brain_mcp.brain_prep import render
        bundle = vault.session_start_bundle(
            project_basename(payload), budget_kb=vault.subagent_budget_kb(), slim=True
        )
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
