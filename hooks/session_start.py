#!/usr/bin/env python3
"""SessionStart hook — preload the Brain bundle into the session as additionalContext.

Also runs the doctor health checks and prepends any warn/error findings as a
banner. A fatal BRAIN_VAULT problem still emits a banner-only context so the
user sees the failure instead of a silently blank session.
"""

from __future__ import annotations

import sys

from _common import emit, project_basename, read_payload


def main() -> None:
    payload = read_payload()
    project = project_basename(payload)

    try:
        from brain_mcp import doctor
        findings = doctor.check(project)
        banner = doctor.render_banner(findings, min_severity="warn")
        vault_error = any(
            f["severity"] == "error"
            and f["code"] in ("BRAIN_VAULT_UNSET", "BRAIN_VAULT_MISSING", "BRAIN_DIR_MISSING")
            for f in findings
        )
    except Exception as e:
        sys.stderr.write(f"brain session_start doctor: {e}\n")
        banner = ""
        vault_error = False

    if vault_error:
        if banner:
            emit({
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": banner,
                }
            })
        sys.exit(0)

    try:
        from brain_mcp import vault
        from brain_mcp.brain_prep import render
        bundle = vault.session_start_bundle(project)
    except Exception as e:
        sys.stderr.write(f"brain session_start: {e}\n")
        if banner:
            emit({
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": banner,
                }
            })
        sys.exit(0)

    context = render(bundle)
    combined = (banner + "\n" + context) if banner else context
    if not combined.strip():
        sys.exit(0)

    emit({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": combined,
        }
    })


if __name__ == "__main__":
    main()
