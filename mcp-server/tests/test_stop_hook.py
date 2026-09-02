"""The Stop hook: bounded transcript reads, honest save detection, and an audit
trail the doctor can read without false alarms.

Everything here runs inside a 5 s hook budget (templates/settings.hooks*.json)
on every turn of every session, so the failure modes are all silent: a slow
read means no gate and no audit row for the rest of the session; a loose
regex means the gate passes with no save; a naive audit means a PROMISE_GAP
warning at every session start for a promise the gate already made the model
resolve.
"""

from __future__ import annotations

import io
import json
import re
import sys
import time
from pathlib import Path

import pytest

import _common
import _savesig
import stop
from brain_mcp import doctor, transcript

REPO_ROOT = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------ helpers

def _user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _tool_result(size: int) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "x", "content": "y" * size}]}}


def _assistant(text: str | None = None, tool: tuple[str, dict] | None = None) -> dict:
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "name": tool[0], "input": tool[1]})
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def _snapshot(size: int) -> dict:
    return {"type": "file-history-snapshot", "snapshot": {"blob": "z" * size}}


def _write_jsonl(path: Path, entries: list[dict]) -> Path:
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


# ---------------------------------------------------------- F16: bounded read

def test_last_turn_cost_is_bounded_by_the_turn_not_the_session(tmp_path: Path) -> None:
    """50 MB of history, evaluated in well under a second, with the right turn.

    The hook used to `list()` every JSON line of the transcript — file-history
    snapshots and multi-megabyte tool results included — on every Stop. That
    was 0.11 s at 4.3 MB and linear, so a long session crossed the 5 s budget
    and lost both the gate and the audit for the rest of its life.
    """
    entries: list[dict] = []
    per_turn = 500_000
    while (len(entries) // 5) * per_turn < 50_000_000:  # ~per_turn bytes per iteration
        entries.append(_user(f"turn {len(entries)} — please do the thing"))
        entries.append(_assistant("working", ("Bash", {"command": "ls"})))
        entries.append(_tool_result(per_turn // 2))
        entries.append(_snapshot(per_turn // 2))
        entries.append(_assistant("old promise: I'll save this to brain", ("brain_save", {})))
    entries.append(_user("final question"))
    entries.append(_assistant("thinking", ("Bash", {"command": "brain save user t --content x"})))
    entries.append(_tool_result(1000))
    entries.append(_assistant("done, saving this to brain now"))
    path = _write_jsonl(tmp_path / "big.jsonl", entries)
    assert path.stat().st_size > 50_000_000

    t0 = time.perf_counter()
    last_user, assistant_text, saves = stop._analyze_last_turn(str(path))
    elapsed = time.perf_counter() - t0

    assert elapsed < 1.0, f"took {elapsed:.2f}s on a {path.stat().st_size >> 20} MB transcript"
    assert last_user == "final question"
    assert assistant_text == "thinking\ndone, saving this to brain now"
    assert saves == 1, "only the last turn's save counts"


def test_tool_result_entries_are_not_turn_boundaries(tmp_path: Path) -> None:
    """A tool_result arrives as a 'user' entry with no text; the turn continues."""
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user("first"),
        _assistant("a"),
        _user("second"),
        _assistant("b", ("Bash", {"command": "ls"})),
        _tool_result(10),
        _assistant("c"),
    ])
    last_user, assistant_text, _ = stop._analyze_last_turn(str(path))
    assert last_user == "second"
    assert assistant_text == "b\nc"


def test_pretty_printed_and_blank_lines_survive(tmp_path: Path) -> None:
    text = json.dumps(_user("hi"), indent=2).replace("\n", " ") + "\n\n" + json.dumps(_assistant("yo")) + "\n"
    path = tmp_path / "t.jsonl"
    path.write_text(text, encoding="utf-8")
    assert stop._analyze_last_turn(str(path)) == ("hi", "yo", 0)


def test_last_line_without_trailing_newline_is_read(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(_user("q")) + "\n" + json.dumps(_assistant("final")), encoding="utf-8")
    assert stop._analyze_last_turn(str(path)) == ("q", "final", 0)


def test_missing_or_absent_transcript_is_an_empty_turn(tmp_path: Path) -> None:
    assert stop._analyze_last_turn(None) == ("", "", 0)
    assert stop._analyze_last_turn(str(tmp_path / "nope.jsonl")) == ("", "", 0)


# --------------------------------------------------- F17: CLI save detection

CLI_CASES = [
    # real invocations
    ('C:/Users/spani/.claude-f42/brain.cmd save feedback "title"', True),
    ('"C:/Users/Joe B/.claude/brain.cmd" checkpoint X <<\'EOF\'\nsummary\nEOF', True),
    ("BRAIN_VAULT=~/Vaults/Ai-Brain ~/src/Ai-Brain/mcp-server/.venv/bin/brain checkpoint Foo <<'EOF'\nbody\nEOF", True),
    ("C:\\Users\\spani\\.claude-f42\\brain.cmd checkpoint Ai-Brain <<'EOF'\nstuff\nEOF", True),
    ('cd /x && brain save user "t" --content "x"', True),
    ('git commit -m "brain save fix" && brain save feedback t --content x', True),
    ("brain save feedback \"quoted title with brain checkpoint\" <<'EOF'\nbody says brain save\nEOF", True),
    ("x=$(brain checkpoint P --summary s)", True),
    ("`brain save user t --content x`", True),
    (r"& 'C:\Users\x\brain.cmd' save user t", True),
    ("brain.exe save user t", True),
    ("ls\nbrain save user t --content x", True),
    # the phrase inside an argument, a heredoc body, or a non-command position
    ('git commit -m "Fix brain checkpoint naming"', False),
    ("git commit -m 'brain save: tighten regex'", False),
    ("cat <<'EOF'\nrun brain save later\nEOF", False),
    ("cat <<EOF\nbrain checkpoint X\nEOF", False),
    ("echo brain save", False),
    ("python -c \"import os; print('brain save')\"", False),
    ('ls; echo "brain save"; ls', False),
    ("git log --grep 'brain checkpoint'", False),
    # other subcommands and other executables
    ("brain recall foo", False),
    ("brain-prep save", False),
    ("", False),
]


@pytest.mark.parametrize("command,expected", CLI_CASES, ids=[c[:40] for c, _ in CLI_CASES])
def test_is_cli_save_command(command: str, expected: bool) -> None:
    assert stop.is_cli_save_command(command) is expected


# ------------------------------------------------------ F17: promise regex

PROMISE_CASES = [
    # the phrasings CLAUDE.md names
    ("I'll save this to brain", True),
    ("Checkpointing now.", True),
    ("Saving this as feedback.", True),
    # other real commitments
    ("Let me record that as project context.", True),
    ("I'll checkpoint after this commit.", True),
    ("Saving this to the vault now.", True),
    ("I'll save this as a project memory", True),
    ("I'm going to store that in the Brain.", True),
    ("I'll note this in long-term memory.", True),
    # generic prose that used to block the turn
    ("Store the result in memory and return it.", False),
    ("I'll store the result in memory between calls.", False),
    ("Add it as a user setting in the config.", False),
    ("I'll save it as a user setting.", False),
    ("Save that as a reference implementation.", False),
    ("I'll save that as a reference implementation for the team.", False),
    ("Let me write the test file.", False),
    ("I will save the file to disk.", False),
    # quoted examples are documentation, not commitments
    ('The gate matches *"I\'ll save this to brain"* in the final message.', False),
    ("The gate matches `checkpointing now`.", False),
    ("", False),
]


@pytest.mark.parametrize("text,expected", PROMISE_CASES, ids=[t[:40] for t, _ in PROMISE_CASES])
def test_is_save_promise(text: str, expected: bool) -> None:
    assert _savesig.is_save_promise(text) is expected


@pytest.mark.parametrize("text", ["*" * 20_000, "_" + " " * 20_000, "* _ " * 10_000, "`" * 20_000],
                         ids=["stars", "underscore-spaces", "mixed", "backticks"])
def test_emphasis_strip_is_linear(text: str) -> None:
    """`\\*+[^*\\n]+?\\*+` took ~1 s on 20,000 asterisks — inside a 5 s hook."""
    t0 = time.perf_counter()
    _savesig.is_save_promise(text)
    assert time.perf_counter() - t0 < 0.1


# -------------------------------------------------- F18: re-entry audit rows

def _run_stop(payload: dict, monkeypatch: pytest.MonkeyPatch, capsys) -> str:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(stop, "brain_tools_callable", lambda: True)
    with pytest.raises(SystemExit):
        stop.main()
    return capsys.readouterr().out


def _rows(vault_dir: Path) -> list[str]:
    return (vault_dir / "activity.md").read_text(encoding="utf-8").splitlines()


def test_gate_block_then_reentry_writes_a_tagged_row(
    vault_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    project = tmp_path / "Widget"
    project.mkdir()
    transcript_path = _write_jsonl(tmp_path / "t.jsonl", [
        _user("please remember this"),
        _assistant("I'll save this to brain."),
    ])
    payload = {"cwd": str(project), "transcript_path": str(transcript_path)}

    out = _run_stop(payload, monkeypatch, capsys)
    assert json.loads(out)["decision"] == "block"
    first = _rows(vault_dir)[-1]
    assert "pro=Y" in first and "sav=N" in first and "re=N" in first

    # Claude Code re-enters after the model fulfils the promise.
    _write_jsonl(transcript_path, [
        _user("please remember this"),
        _assistant("I'll save this to brain."),
        _assistant(None, ("Bash", {"command": "brain save user t --content x"})),
        _assistant("Saved."),
    ])
    out = _run_stop({**payload, "stop_hook_active": True}, monkeypatch, capsys)
    assert out == "", "a re-entry never blocks again"
    second = _rows(vault_dir)[-1]
    assert "pro=Y" in second and "sav=Y" in second and "re=Y" in second

    findings = {f.code: f for f in doctor._check_promise_gap(vault_dir)}
    assert "PROMISE_GAP" not in findings, findings
    assert "PROMISE_GAP_OK" in findings


def _activity(vault_dir: Path, rows: list[str]) -> None:
    (vault_dir / "activity.md").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _row(cols: str, project: str = "Widget", account: str = "claude") -> str:
    return f"2026-09-01 10:00 {account} {project} [{cols}] — snippet"


def test_promise_gap_ignores_a_gated_turn_the_model_recanted(vault_dir: Path) -> None:
    _activity(vault_dir, [
        _row("sig=N sav=N nud=N pro=Y too=Y sys=N re=N"),
        _row("sig=N sav=N nud=N pro=Y too=Y sys=N re=Y"),  # recanted: no save, gate satisfied
    ])
    codes = {f.code for f in doctor._check_promise_gap(vault_dir)}
    assert "PROMISE_GAP" not in codes


def test_promise_gap_still_fires_for_an_unanswered_promise(vault_dir: Path) -> None:
    _activity(vault_dir, [
        _row("sig=N sav=N nud=N pro=Y too=Y sys=N re=N"),
    ])
    codes = {f.code for f in doctor._check_promise_gap(vault_dir)}
    assert "PROMISE_GAP" in codes


def test_promise_gap_counts_legacy_rows_without_the_re_column(vault_dir: Path) -> None:
    _activity(vault_dir, [
        _row("sig=N sav=N nud=N pro=Y too=Y sys=N"),
    ])
    codes = {f.code for f in doctor._check_promise_gap(vault_dir)}
    assert "PROMISE_GAP" in codes


def test_reentry_supersedes_only_its_own_project(vault_dir: Path) -> None:
    """A re-entry from another session must not absolve this one's promise."""
    _activity(vault_dir, [
        _row("sig=N sav=N nud=N pro=Y too=Y sys=N re=N", project="Alpha"),
        _row("sig=N sav=N nud=N pro=Y too=Y sys=N re=N", project="Beta Two"),
        _row("sig=N sav=Y nud=N pro=Y too=Y sys=N re=Y", project="Beta Two"),
    ])
    finding = next(f for f in doctor._check_promise_gap(vault_dir) if f.code.startswith("PROMISE_GAP"))
    assert finding.code == "PROMISE_GAP"
    assert finding.message.startswith("1 of ")


def test_save_gap_credits_a_save_made_on_reentry(vault_dir: Path) -> None:
    rows = []
    for _ in range(3):
        rows.append(_row("sig=Y sav=N nud=Y pro=Y too=Y sys=N re=N"))
        rows.append(_row("sig=Y sav=Y nud=Y pro=Y too=Y sys=N re=Y"))
    _activity(vault_dir, rows)
    codes = {f.code for f in doctor._check_save_gap(vault_dir)}
    assert "SAVE_GAP" not in codes
    assert "SAVE_GAP_OK" in codes


# ------------------------------------------- F25: one system-turn prefix list

SYSTEM_MARKERS = ["<task-notification>", "[SYSTEM NOTIFICATION", "<local-command-", "<command-",
                  "Base directory for this skill:"]


def test_system_turn_prefixes_have_exactly_one_home() -> None:
    """Both consumers must read `transcript.SYSTEM_TURN_PREFIXES`.

    Two lists disagreed until 2026-09-01: the checkpoint renderer knew only the
    command wrappers, so 9 of 249 checkpoints carried subagent output under
    "What the user asked for". A marker literal anywhere but the one list is
    the N-sites bug coming back.
    """
    offenders = []
    for py in list((REPO_ROOT / "hooks").glob("*.py")) + list((REPO_ROOT / "mcp-server" / "brain_mcp").glob("*.py")):
        if py.name == "transcript.py":
            continue
        src = py.read_text(encoding="utf-8")
        for marker in SYSTEM_MARKERS:
            if marker in src:
                offenders.append(f"{py.name}: {marker!r}")
    assert not offenders, offenders
    assert stop.is_system_turn is transcript.is_system_turn


@pytest.mark.parametrize("prefix", list(transcript.SYSTEM_TURN_PREFIXES))
def test_both_consumers_agree_on_every_prefix(prefix: str, tmp_path: Path) -> None:
    text = prefix + "whatever follows"
    assert stop.is_system_turn(text)
    path = _write_jsonl(tmp_path / "t.jsonl", [_user(text), _assistant("ok")])
    assert transcript.parse_claude_transcript(path)["user_msgs"] == []


def test_a_prompt_behind_a_system_reminder_is_still_the_users() -> None:
    text = "<system-reminder>context</system-reminder>\n\nfix the build"
    assert not stop.is_system_turn(text)
    assert transcript.user_authored_text(text) == "fix the build"
    assert stop.is_system_turn("<system-reminder>only this</system-reminder>")
    assert not stop.is_system_turn("")


# ------------------------------------ F26: no phantom vault, bounded activity

def test_append_activity_never_creates_the_brain_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A mistyped or not-yet-synced BRAIN_VAULT used to get a Brain/ directory
    on the first Stop, after which doctor's BRAIN_DIR_MISSING could never fire."""
    missing = tmp_path / "not-synced-yet"
    monkeypatch.setenv("BRAIN_VAULT", str(missing))
    _common.append_activity("row")
    assert not missing.exists()
    assert "does not exist" in capsys.readouterr().err


def test_activity_is_rotated_past_the_high_water_mark(vault_dir: Path) -> None:
    activity = vault_dir / "activity.md"
    activity.write_text("".join(f"row {i}\n" for i in range(_common.ACTIVITY_MAX_LINES)), encoding="utf-8")
    _common.append_activity("the newest row")
    lines = activity.read_text(encoding="utf-8").splitlines()
    assert len(lines) == _common.ACTIVITY_KEEP_LINES
    assert lines[-1] == "the newest row"
    assert lines[0] == f"row {_common.ACTIVITY_MAX_LINES - _common.ACTIVITY_KEEP_LINES + 1}"
    assert not list(vault_dir.glob("activity.md.*")), "rotation temp file left behind"
    assert _common.ACTIVITY_KEEP_LINES >= doctor.SAVE_GAP_WINDOW


def test_activity_below_the_mark_is_left_alone(vault_dir: Path) -> None:
    activity = vault_dir / "activity.md"
    activity.write_text("a\nb\n", encoding="utf-8")
    _common.append_activity("c")
    assert activity.read_text(encoding="utf-8") == "a\nb\nc\n"


def test_activity_row_format_matches_the_doctor_regex(
    vault_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    project = tmp_path / "Widget"
    project.mkdir()
    _run_stop({"cwd": str(project)}, monkeypatch, capsys)
    row = _rows(vault_dir)[-1]
    m = doctor._ACTIVITY_COLUMNS_RE.search(row)
    assert m is not None, row
    assert m.group(7) == "N"
    assert re.match(r"^\S+ \S+ claude Widget \[sig=", row), row
