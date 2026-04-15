#!/usr/bin/env python3
"""SessionEnd hook — write a final checkpoint when a session terminates."""

from __future__ import annotations

import sys

from _checkpoint import write_session_checkpoint
from _common import project_basename, read_payload


def main() -> None:
    payload = read_payload()
    project = project_basename(payload)
    transcript = payload.get("transcript_path")
    reason = payload.get("matcher_value", "other")
    try:
        write_session_checkpoint(transcript, project, source=f"session-end:{reason}")
    except Exception as e:
        sys.stderr.write(f"brain session_end: {e}\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
