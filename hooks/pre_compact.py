#!/usr/bin/env python3
"""PreCompact hook — write a structural checkpoint to the vault before context compaction."""

from __future__ import annotations

import sys

from _checkpoint import write_session_checkpoint
from _common import project_basename, read_payload


def main() -> None:
    payload = read_payload()
    project = project_basename(payload)
    transcript = payload.get("transcript_path")
    # Claude Code sends `trigger` ("manual" | "auto"). This read a `matcher_value`
    # key no payload has ever carried, so every checkpoint said "auto" (until 2026-10-01).
    trigger = payload.get("trigger") or "auto"
    try:
        write_session_checkpoint(transcript, project, source=f"pre-compact:{trigger}")
    except Exception as e:
        sys.stderr.write(f"brain pre_compact: {e}\n")
    sys.exit(0)  # never block compaction


if __name__ == "__main__":
    main()
