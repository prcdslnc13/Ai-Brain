"""Console-encoding guard shared by every CLI entry point.

Memory bodies contain whatever the user wrote -- em dashes, arrows, curly
quotes, the occasional box-drawing character. Windows still hands a Python
process cp1252 streams when it is piped or run from a non-UTF-8 console, and
that breaks in both directions: stdout raises UnicodeEncodeError printing a
memory that contains an arrow, and stdin mangles a UTF-8 heredoc body into
mojibake on the way in (2026-07-28: em dashes saved as mojibake, and an
undecodable 0x9D byte from a curly quote killed a save mid-command).

`brain` has forced UTF-8 since that incident; `brain-prep`, `brain-compact`
and `brain-doctor` did not, and on 2026-08-19 `brain-prep --project X | ...`
was found dying on a single U+2192 in a memory -- in the one tool whose entire
job is handing a bundle to a local model. Hence one shared helper that every
entry point calls.

Deliberately NOT applied at package import: `brain_mcp.server` speaks JSON-RPC
over stdout and owns its own stream setup, and a library that rewrites the
caller's stdio on import is a nasty surprise.
"""

from __future__ import annotations

import sys


def force_utf8_stdio(include_stdin: bool = False) -> None:
    """Force UTF-8 on the console streams, degrading to replacement characters.

    A mangled glyph is cosmetic; a traceback instead of the memory bundle is
    not. `include_stdin` is for the entry points that read bodies from a pipe.
    """
    streams = [sys.stdout, sys.stderr]
    if include_stdin:
        streams.insert(0, sys.stdin)
    for stream in streams:
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
