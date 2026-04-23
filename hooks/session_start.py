#!/usr/bin/env python3
"""SessionStart hook — preload the Brain bundle into the session as additionalContext.

Also runs the doctor health checks and prepends any warn/error findings as a
banner. A fatal BRAIN_VAULT problem still emits a banner-only context so the
user sees the failure instead of a silently blank session.
"""

from __future__ import annotations

import sys

from _common import emit, project_basename, read_payload


def _import_failure_banner(component: str, err: Exception) -> str:
    """Synthetic banner for when brain_mcp itself fails to import. Without this the
    user sees an empty session and assumes the Brain silently forgot things."""
    return (
        "## Brain Health\n"
        "\n"
        f"- **[ERROR]** `BRAIN_MCP_IMPORT_FAILED` — {component} import failed: "
        f"{type(err).__name__}: {err}  \n"
        "  *Re-run setup-mac.sh / setup-windows.ps1 to reinstall the MCP venv. "
        "Brain tools will not work until this is fixed.*\n"
    )


def main() -> None:
    payload = read_payload()
    project = project_basename(payload)
    project_cwd = payload.get("cwd")

    try:
        from brain_mcp import doctor, vault
        if project:
            try:
                vault.ensure_project_overview_stub(project, project_cwd)
            except Exception as e:
                sys.stderr.write(f"brain session_start stub: {e}\n")
        findings = doctor.check(project, project_cwd)
        banner = doctor.render_banner(findings, min_severity="warn")
        vault_error = any(
            f["severity"] == "error"
            and f["code"] in ("BRAIN_VAULT_UNSET", "BRAIN_VAULT_MISSING", "BRAIN_DIR_MISSING")
            for f in findings
        )
    except Exception as e:
        sys.stderr.write(f"brain session_start doctor: {e}\n")
        banner = _import_failure_banner("doctor", e)
        vault_error = True  # treat import failure as fatal — no bundle can load either

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
        fail_banner = banner or _import_failure_banner("vault bundle", e)
        emit({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": fail_banner,
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
