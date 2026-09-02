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
# recant" — annoying but recoverable. Every pattern therefore requires a
# Brain-specific noun: "brain", "the vault", "long-term memory", "checkpoint",
# or "as (a) <memory-type> (memory|note|entry|record|context)". Bare "memory"
# was dropped on 2026-09-01: "I'll store the result in memory" is ordinary
# programming prose, as are "add it as a user setting" and "save that as a
# reference implementation", and all three used to block the turn.
_FUTURE = r"(?:i'?ll|i will|let me|i'?m\s+going\s+to|i am going to)"
_SAVE_VERB = r"(?:save|record|store|pin|persist|write|note)"
_BRAIN_NOUN = r"(?:(?:the\s+)?brain|(?:the\s+)?vault|long-term memory|checkpoint)"
# "as feedback" is Brain vocabulary on its own; the other three types are
# ordinary English words and need a memory noun after them.
_AS_TYPE = (
    r"as\s+(?:a\s+|an\s+|new\s+)?"
    r"(?:feedback(?:\s+(?:memory|note|entry|record))?"
    r"|(?:user|project|reference)\s+(?:memory|note|entry|record|context))"
)
PROMISE_PATTERNS = (
    # Future-tense save-verb + brain noun / as-<type> within 120 chars.
    rf"\b{_FUTURE}\s+{_SAVE_VERB}\b[^.\n]{{0,120}}?\b(?:{_BRAIN_NOUN}|{_AS_TYPE})\b",
    # "checkpoint" is brain-specific vocab — future-tense alone is enough.
    rf"\b{_FUTURE}\s+checkpoint\b",
    # Progressive form with explicit destination: "saving this to brain".
    rf"\b(?:saving|recording|storing|writing|noting)\b[^.\n]{{0,80}}?"
    rf"\b(?:to|in|into)\s+{_BRAIN_NOUN}\b",
    # "Checkpointing …" as a verb — always brain-specific.
    r"\bcheckpointing\b",
    # "Saving now" / "saving this now" — shorthand commitment.
    r"\b(?:saving|recording)\s+(?:this\s+|that\s+|it\s+|them\s+)?now\b",
    # "I'll save that as feedback" / "saving as a project memory".
    rf"\b(?:{_FUTURE}|saving|recording)\b[^.\n]{{0,30}}\b{_AS_TYPE}\b",
)

_PROMISE_COMPILED = tuple(re.compile(p, re.IGNORECASE) for p in PROMISE_PATTERNS)

# Markdown spans stripped before promise-matching. The gate must not fire on
# documentation that quotes example promise phrases — a real commitment is
# never wrapped in backticks or asterisk emphasis. Without this strip, the
# gate false-positives on Brain-related summaries that enumerate the very
# phrases it matches (e.g. *"I'll save this to brain"* in a docstring).
#
# Every span pattern is bounded. `\*+[^*\n]+?\*+` was quadratic on a run of
# asterisks (1 s on 20,000 of them, inside a hook with a 5 s budget), and the
# underscore pattern's two unbounded `[^_\n]*` runs backtracked against each
# other on long whitespace. Markdown emphasis is 1-3 markers wide and a real
# emphasised span is short; anything longer is not emphasis and may stay.
_EMPHASIS_STRIP_PATTERNS = (
    re.compile(r"```[\s\S]*?```"),                  # fenced code blocks
    re.compile(r"`[^`\n]*`"),                        # inline backtick spans
    re.compile(r"\*{1,3}[^*\n]{1,400}?\*{1,3}"),     # *italic* and **bold**
    # Underscore italic only when the span contains whitespace — avoids stripping
    # code identifiers like `is_save_promise` or `BRAIN_STOP_GATE`.
    re.compile(r"_[^_\n]{0,200}\s[^_\n]{0,200}_"),
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
    "Save it now if the content fits one of the four memory types "
    "(user / feedback / project / reference) — run `brain save` per your "
    "global CLAUDE.md, or call the brain_save MCP tool if registered. "
    "If it does not fit, ignore this nudge."
)

GATE_BLOCK_REASON = (
    "You told the user you would save or checkpoint something to the Brain, "
    "but no brain save happened this turn (neither a `brain save`/`brain "
    "checkpoint` CLI command via Bash nor a brain_save/brain_checkpoint MCP "
    "tool call). Say = do: a stated commitment must be fulfilled in the same "
    "turn. Either perform the save now, OR explicitly recant/defer the "
    "commitment to the user ('actually, I'll hold off on saving until…') "
    "before ending the turn."
)
