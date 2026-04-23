"""Shared save-signal detection for the Brain hooks.

Two detectors live here:

1. `is_save_signal(user_text)` — matches phrases in the *user's* message that
   suggest the assistant should call `brain_save`. Used by `stop.py` (activity
   audit column) and `user_prompt_submit.py` (soft nudge).

2. `is_save_promise(assistant_text)` — matches phrases in the *assistant's*
   message that explicitly commit to saving/checkpointing. Used by `stop.py` to
   gate turn-end: if the assistant promised to save but didn't call the tool,
   the Stop hook blocks until the promise is fulfilled or recanted. Drives the
   "say = do in same turn" invariant.

The nudge can be disabled per-install by setting `BRAIN_NUDGE=0` in the hook
env. The Stop-hook gate can be disabled with `BRAIN_STOP_GATE=0`. The audit
columns still record `sig=Y` and `pro=Y` either way — observability is never
gated.
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


# Patterns that match the assistant committing to a brain save/checkpoint in
# the current turn. The gate fires when any of these appears in the assistant's
# final message AND no brain_save/brain_checkpoint tool call occurred.
#
# Design: precision over recall. A miss means a silent loss (bad), but a false
# positive means an irritating turn-block that tells the model to "save or
# recant" — annoying but recoverable. We still bias toward keywords that are
# brain-specific ("checkpoint", "brain", "vault", or "as a <memory-type>") to
# avoid blocking on generic "I'll save the file" / "let me write the test".
PROMISE_PATTERNS = (
    # Future-tense save-verb + brain keyword / as-<type> within 120 chars.
    r"\b(i'?ll|i will|let me|i'?m\s+going\s+to|i am going to)\s+"
    r"(save|record|store|pin|persist|write|note)\b[^.\n]{0,120}?"
    r"\b(brain|vault|memory|as\s+(a\s+|an\s+|new\s+)?(feedback|project|user|reference))\b",
    # "checkpoint" is brain-specific vocab — future-tense alone is enough.
    r"\b(i'?ll|i will|let me|i'?m\s+going\s+to|i am going to)\s+checkpoint\b",
    # Progressive form with explicit destination: "saving this to brain".
    r"\b(saving|recording|storing|writing|noting)\b[^.\n]{0,80}?"
    r"\b(to|in|into)\s+(the\s+)?(brain|vault|memory)\b",
    # "Checkpointing …" as a verb — always brain-specific.
    r"\bcheckpointing\b",
    # "Saving now" / "saving this now" — shorthand commitment.
    r"\b(saving|recording)\s+(this\s+|that\s+|it\s+|them\s+)?now\b",
    # "I'll save that as feedback" / "saving as a project memory".
    r"\b(i'?ll|let me|i'?m\s+going\s+to|saving|recording)\b[^.\n]{0,30}"
    r"\bas\s+(a\s+|an\s+|new\s+)?(feedback|project|user|reference)\s+"
    r"(memory|note|entry|record)?\b",
)

_PROMISE_COMPILED = tuple(re.compile(p, re.IGNORECASE) for p in PROMISE_PATTERNS)

# Markdown spans stripped before promise-matching. The gate must not fire on
# documentation that quotes example promise phrases — a real commitment is
# never wrapped in backticks or asterisk emphasis. Without this strip, the
# gate false-positives on Brain-related summaries that enumerate the very
# phrases it matches (e.g. *"I'll save this to brain"* in a docstring).
_EMPHASIS_STRIP_PATTERNS = (
    re.compile(r"```[\s\S]*?```"),          # fenced code blocks
    re.compile(r"`[^`\n]*`"),                # inline backtick spans
    re.compile(r"\*+[^*\n]+?\*+"),           # *italic* and **bold**
    # Underscore italic only when the span contains whitespace — avoids stripping
    # code identifiers like `is_save_promise` or `BRAIN_STOP_GATE`.
    re.compile(r"_[^_\n]*\s[^_\n]*_"),
)


def _strip_markdown_emphasis(text: str) -> str:
    for pat in _EMPHASIS_STRIP_PATTERNS:
        text = pat.sub(" ", text)
    return text


def is_save_promise(text: str) -> bool:
    """True when the assistant's message contains a same-turn save commitment.

    Used by stop.py to decide whether to block turn-end when no brain_save /
    brain_checkpoint tool call has occurred. Markdown emphasis and code spans
    are stripped first so documentation quoting example phrases doesn't trip
    the gate.
    """
    if not text:
        return False
    stripped = _strip_markdown_emphasis(text)
    return any(p.search(stripped) for p in _PROMISE_COMPILED)


def nudge_enabled() -> bool:
    return os.environ.get("BRAIN_NUDGE", "1").strip() not in ("0", "false", "no", "off", "")


def gate_enabled() -> bool:
    """Stop-hook gate for unfulfilled save promises.

    Default on. Set `BRAIN_STOP_GATE=0` to disable (the audit column still
    records `pro=Y`, so `brain_doctor` can still surface gaps after the fact).
    """
    return os.environ.get("BRAIN_STOP_GATE", "1").strip() not in ("0", "false", "no", "off", "")


NUDGE_TEXT = (
    "Brain nudge: your last message contained a potential save-signal "
    "(preference, correction, durable rule, deadline, or external reference). "
    "Call `brain_save` now if the content fits one of the four memory types "
    "(user / feedback / project / reference). If it does not, ignore this nudge."
)

GATE_BLOCK_REASON = (
    "You told the user you would save or checkpoint something to the Brain, "
    "but no brain_save or brain_checkpoint tool call occurred in this turn. "
    "Say = do: a stated commitment must be fulfilled in the same turn. "
    "Either call the matching brain tool now, OR explicitly recant/defer the "
    "commitment to the user ('actually, I'll hold off on saving until…') "
    "before ending the turn."
)
