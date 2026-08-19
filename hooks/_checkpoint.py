"""Shared checkpoint entry point for the PreCompact and SessionEnd hooks.

The parsing and rendering moved into `brain_mcp.transcript` so the `brain`
CLI can produce byte-identical checkpoints for harnesses that have no hooks
(`brain checkpoint --from-cherryd`). This module stays as the hook-facing
name so the hook scripts keep importing one stable symbol.

brain_mcp is installed in mcp-server/.venv (non-editable) and the hook command
launches us via that interpreter, so the import works without sys.path tricks.
BRAIN_VAULT must be set in env by the hook command.
"""

from __future__ import annotations

from brain_mcp.transcript import (  # noqa: F401  (re-exported for the hooks)
    parse_claude_transcript,
    render_checkpoint,
    write_session_checkpoint,
)

# Historical name, kept so anything still calling it does not break.
parse_transcript = parse_claude_transcript
