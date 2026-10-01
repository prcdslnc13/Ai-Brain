"""PreCompact and SessionEnd label their checkpoints from the fields Claude Code sends.

Both hooks read `matcher_value`, a key no hook payload has carried, so all 26 PreCompact
checkpoints in the vault said `pre-compact:auto` and all 179 SessionEnd ones said
`session-end:other`. The installed Claude Code builds these inputs as
`{hook_event_name: "PreCompact", trigger, custom_instructions}` and
`{hook_event_name: "SessionEnd", reason}`.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

import pre_compact
import session_end


def _run(module, payload: dict, monkeypatch: pytest.MonkeyPatch) -> str:
    seen = {}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(module, "write_session_checkpoint",
                        lambda transcript, project, source: seen.setdefault("source", source))
    with pytest.raises(SystemExit):
        module.main()
    return seen["source"]


@pytest.mark.parametrize("trigger", ["manual", "auto"])
def test_pre_compact_reads_trigger(trigger: str, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"hook_event_name": "PreCompact", "trigger": trigger, "transcript_path": "t.jsonl"}
    assert _run(pre_compact, payload, monkeypatch) == f"pre-compact:{trigger}"


@pytest.mark.parametrize("reason", ["clear", "resume", "logout", "prompt_input_exit", "other"])
def test_session_end_reads_reason(reason: str, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"hook_event_name": "SessionEnd", "reason": reason, "transcript_path": "t.jsonl"}
    assert _run(session_end, payload, monkeypatch) == f"session-end:{reason}"


def test_missing_fields_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(pre_compact, {"transcript_path": "t"}, monkeypatch) == "pre-compact:auto"
    assert _run(session_end, {"transcript_path": "t"}, monkeypatch) == "session-end:other"
