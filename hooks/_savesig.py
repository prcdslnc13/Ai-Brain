"""Shared save-signal detection for the Brain hooks.

Used by both `stop.py` (for the activity.md audit column) and
`user_prompt_submit.py` (for the soft nudge). Keeping the patterns in one place
guarantees the audit metric and the nudge agree on what counts as a signal.

The nudge can be disabled per-install by setting `BRAIN_NUDGE=0` in the hook
env. The audit column still records `sig=Y` even when the nudge is off — that's
the whole point of the observability layer.
"""

from __future__ import annotations

import os
import re

SAVE_SIGNAL_PATTERNS = (
    r"\bremember\b",
    r"\bfrom now on\b",
    r"\bnext time\b",
    r"\bdon'?t forget\b",
    r"\bi prefer\b",
    r"\bi like\b.*\bbetter\b",
    r"\balways\b.*\bdo\b",
    r"\bnever\b.*\bdo\b",
    r"\bstop doing\b",
    r"\bgoing forward\b",
    r"\bi want\b",
    r"\bi'?m looking for\b",
    r"\bthe right\b.*\b(is|way|cadence|approach)\b",
)

_COMPILED = tuple(re.compile(p, re.IGNORECASE) for p in SAVE_SIGNAL_PATTERNS)


def is_save_signal(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _COMPILED)


def nudge_enabled() -> bool:
    return os.environ.get("BRAIN_NUDGE", "1").strip() not in ("0", "false", "no", "off", "")


NUDGE_TEXT = (
    "Brain nudge: your last message contained a potential save-signal "
    "(preference, correction, durable rule, deadline, or external reference). "
    "Call `brain_save` now if the content fits one of the four memory types "
    "(user / feedback / project / reference). If it does not, ignore this nudge."
)
