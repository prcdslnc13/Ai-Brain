#!/usr/bin/env python3
"""SessionStart hook — preload the Brain bundle into the session as additionalContext.

Also runs the doctor health checks and prepends any warn/error findings as a
banner. A fatal BRAIN_VAULT problem still emits a banner-only context so the
user sees the failure instead of a silently blank session.

Multi-part delivery (2026-09-01). Claude Code caps one hook command's
`additionalContext` at 10,000 chars and spills anything larger to a file the
model never reads — which for a 44 KB bundle meant no session since 2026-08-06
had received a single user memory or feedback rule. So settings.json registers
`vault.PRELOAD_PARTS` entries for this event, each invoking this script with
`--part I --parts N`, and each emits one part of `brain_prep.render_parts`. Only
part 1 runs the doctor, writes the overview stub and kicks the reindex — those
are once-per-session jobs, and the banner belongs at the top of part 1. With no
`--part` argument the whole bundle is emitted as one document, as before.
"""

from __future__ import annotations

import argparse
import sys

from _common import emit, project_basename, read_payload

FATAL_VAULT_CODES = ("BRAIN_VAULT_UNSET", "BRAIN_VAULT_MISSING", "BRAIN_DIR_MISSING")


def _import_failure_banner(component: str, err: Exception) -> str:
    """Synthetic banner for when brain_mcp itself fails to import. Without this the
    user sees an empty session and assumes the Brain silently forgot things."""
    return (
        "## Brain Health\n"
        "\n"
        f"- **[ERROR]** `BRAIN_MCP_IMPORT_FAILED` — {component} import failed: "
        f"{type(err).__name__}: {err}  \n"
        "  *Re-run brain-setup.py to reinstall the Brain venv. "
        "Brain tools will not work until this is fixed.*\n"
    )


def _doctor_failure_banner(err: Exception) -> str:
    """The doctor raised as a whole (its per-check isolation should make this rare).

    Distinct from an import failure on purpose: the vault is reachable and the
    bundle can still be built, so this banner is a warning on top of a full
    preload, not a substitute for one (F4).
    """
    return (
        "## Brain Health\n"
        "\n"
        f"- **[WARN]** `DOCTOR_FAILED` — health checks did not run: "
        f"{type(err).__name__}: {err}  \n"
        "  *The preload below is unaffected. Run `brain doctor` to see the traceback.*\n"
    )


def _emit(context: str) -> None:
    emit({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    })


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--part", type=int, default=None)
    parser.add_argument("--parts", type=int, default=None)
    args, _unknown = parser.parse_known_args(argv)
    if args.part is not None and args.parts is None:
        args.parts = None  # render_parts falls back to vault.PRELOAD_PARTS
    if args.part is not None and args.part < 1:
        args.part = 1
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    payload = read_payload()
    project = project_basename(payload)
    project_cwd = payload.get("cwd")
    first = args.part is None or args.part == 1

    # Import failure is its own failure: nothing downstream can run, so say so and
    # stop. Every other failure below leaves the bundle buildable and must not
    # be allowed to cost the session its memories (F4).
    try:
        from brain_mcp import doctor, vault
        from brain_mcp.brain_prep import render, render_parts
    except Exception as e:
        sys.stderr.write(f"brain session_start import: {e}\n")
        if first:
            _emit(_import_failure_banner("brain_mcp", e))
        sys.exit(0)

    banner = ""
    bundle_cache: dict = {}
    if first:
        if project:
            try:
                vault.ensure_project_overview_stub(project, project_cwd)
            except Exception as e:
                sys.stderr.write(f"brain session_start stub: {e}\n")
        try:
            findings = doctor.check(project, project_cwd, bundle_cache=bundle_cache)
            banner = doctor.render_banner(findings, min_severity="warn")
            vault_error = any(
                f.get("severity") == "error" and f.get("code") in FATAL_VAULT_CODES
                for f in findings
            )
        except Exception as e:
            sys.stderr.write(f"brain session_start doctor: {e}\n")
            banner = _doctor_failure_banner(e)
            vault_error = False
        if vault_error:
            if banner:
                _emit(banner)
            sys.exit(0)

        # Kick the vector-index catch-up before building the bundle. Detached, so
        # it runs through the session instead of making the first recall pay for it;
        # BRAIN_AUTO_REINDEX=0 disables.
        try:
            from brain_mcp import embed
            embed.spawn_background_reindex(min_backlog=embed.EmbedIndex.SYNC_CHUNK)
        except Exception as e:
            sys.stderr.write(f"brain session_start reindex: {e}\n")

    # Reuse the bundle doctor already built for its budget check (F12) rather
    # than reading the corpus a second time; later parts build their own.
    try:
        bundle = bundle_cache.get("session") or vault.session_start_bundle(project)
    except Exception as e:
        sys.stderr.write(f"brain session_start: {e}\n")
        if first:
            _emit(banner or _import_failure_banner("vault bundle", e))
        sys.exit(0)

    try:
        if args.part is None:
            context = render(bundle)
            combined = (banner + "\n" + context) if banner else context
        else:
            parts = render_parts(bundle, parts=args.parts, banner=banner)
            combined = parts[args.part - 1] if args.part <= len(parts) else ""
    except Exception as e:
        sys.stderr.write(f"brain session_start render: {e}\n")
        combined = banner if first else ""

    if not combined.strip():
        sys.exit(0)
    _emit(combined)


if __name__ == "__main__":
    main()
