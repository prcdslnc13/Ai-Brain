"""The Stop hook learns that a save happened from the saver, never from the
model's shell command.

Until 2026-09-13 `hooks/stop.py` decided "did a brain save happen this turn?"
by regex-matching every Bash/PowerShell tool_use in the turn against the shape
of a `brain save` / `brain checkpoint` invocation. That predicate was patched
four times for the same reason — env prefixes, `.cmd` wrappers, quoting, and
then the `brain-agent.py` launcher every install has emitted since 2026-09-01,
which it never learned. From that day every save through the standard surface
was audited `sav=N`, the gate blocked turns that had just saved, and the
SAVE_GAP / PROMISE_GAP checks drifted toward permanent false positives.

Now every model-facing save surface appends an event to
`Brain/.state/save-events.jsonl` after its write lands, and the hook counts
events at or after the turn's user message for its session. The invariants:

  1. every surface a model can save through records an event — the CLI (which
     is what the launcher, pi and any wrapper call) and the MCP tools;
  2. the hook credits exactly the saves that happened in this turn, for this
     session, and consults nothing about how the command was spelled;
  3. recording is bookkeeping: it can never fail the save it follows.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import time
from pathlib import Path

import pytest

import stop
from brain_mcp import cli, server, vault

REPO_ROOT = Path(__file__).resolve().parents[2]
SESSION = "5206288e-a42c-4331-8645-194d4c81d093"


def _events(vault_dir: Path) -> list[dict]:
    path = vault_dir / vault.SAVE_EVENTS_REL
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _run_cli(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code in (0, None), f"{argv} exited {exc.value.code}"


# ------------------------------------------- 1. every surface records an event

def test_every_model_facing_save_surface_records_an_event(
    vault_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(vault.SESSION_ID_ENV, SESSION)
    t0 = time.time()

    _run_cli(["save", "user", "Shell", "--content", "zsh"])
    _run_cli(["checkpoint", "Widget", "--summary", "did things"])
    asyncio.run(server.call_tool("brain_save", {"type": "user", "name": "Editor", "content": "vim"}))
    asyncio.run(server.call_tool("brain_checkpoint", {"project": "Widget", "summary": "more things"}))

    events = _events(vault_dir)
    assert [(e["kind"], e["surface"]) for e in events] == [
        ("save", "cli"), ("checkpoint", "cli"), ("save", "mcp"), ("checkpoint", "mcp"),
    ]
    for e in events:
        assert e["session"] == SESSION
        assert e["machine"] == "test-host"
        assert e["ts"] >= t0
        assert Path(e["path"]).is_file(), "the event names the file that landed"


def test_an_unchanged_resave_still_counts_as_a_save(vault_dir: Path) -> None:
    """The model did save; that the vault already held the content is not its
    failure, and a gate that blocked here would demand a pointless rewrite."""
    _run_cli(["save", "user", "Shell", "--content", "zsh"])
    _run_cli(["save", "user", "Shell", "--content", "zsh"])
    assert len(_events(vault_dir)) == 2


def test_a_save_outside_claude_code_records_no_session(
    vault_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(vault.SESSION_ID_ENV, raising=False)
    _run_cli(["save", "user", "Shell", "--content", "zsh"])
    assert _events(vault_dir)[0]["session"] is None


# ------------------------------------------- 2. the hook credits this turn only

def _event(vault_dir: Path, ts: float, session: str | None = SESSION) -> None:
    path = vault_dir / vault.SAVE_EVENTS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "kind": "save", "surface": "cli", "session": session}) + "\n")


def test_only_events_since_the_turn_began_count(vault_dir: Path) -> None:
    turn_start = 1_800_000_000.0
    _event(vault_dir, turn_start - 1)
    assert not stop.saved_this_turn(0, turn_start, SESSION), "the previous turn's save is not this turn's"
    _event(vault_dir, turn_start)
    assert stop.saved_this_turn(0, turn_start, SESSION), "at the boundary counts"


def test_another_sessions_save_does_not_absolve_this_one(vault_dir: Path) -> None:
    turn_start = 1_800_000_000.0
    _event(vault_dir, turn_start + 5, session="other-session")
    assert not stop.saved_this_turn(0, turn_start, SESSION)
    _event(vault_dir, turn_start + 6, session=None)
    assert stop.saved_this_turn(0, turn_start, SESSION), "a sessionless event matches any session"


def test_a_turn_with_no_timestamp_trusts_only_the_structured_tool_use(vault_dir: Path) -> None:
    """Guessing from 'recent' events could credit the previous turn's save."""
    _event(vault_dir, time.time())
    assert not stop.saved_this_turn(0, None, SESSION)
    assert stop.saved_this_turn(1, None, SESSION)


def test_corrupt_lines_and_a_missing_file_count_as_nothing(vault_dir: Path) -> None:
    assert vault.count_save_events(since=0) == 0
    path = vault_dir / vault.SAVE_EVENTS_REL
    path.parent.mkdir(parents=True)
    path.write_text('not json\n{"kind": "save"}\n{"ts": "soon"}\n' + json.dumps({"ts": 5.0}) + "\n",
                    encoding="utf-8")
    assert vault.count_save_events(since=0) == 1


def test_the_transcript_timestamp_is_read_as_utc() -> None:
    assert stop._parse_timestamp("2026-09-13T21:52:01.131Z") == pytest.approx(1789336321.131)
    assert stop._parse_timestamp("2026-09-13T21:52:01.131+00:00") == pytest.approx(1789336321.131)
    assert stop._parse_timestamp(None) is None
    assert stop._parse_timestamp("yesterday") is None


def test_the_launcher_shaped_save_no_longer_blocks_the_gate(
    vault_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The 2026-09-13 incident, end to end: a promise, a save through the
    launcher, a Stop. The old hook blocked this turn and wrote sav=N."""
    project = tmp_path / "Widget"
    project.mkdir()
    turn_ts = "2026-09-13T21:52:01.131Z"
    transcript = tmp_path / "t.jsonl"
    with transcript.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "timestamp": turn_ts,
                            "message": {"role": "user", "content": "remember this"}}) + "\n")
        f.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "I'll save this to brain."},
            {"type": "tool_use", "name": "Bash", "input": {
                "command": '"/v/bin/python" "/c/brain-agent.py" save user t --content x'}},
            {"type": "text", "text": "Saved as a user memory."}]}}) + "\n")
    monkeypatch.setenv(vault.SESSION_ID_ENV, SESSION)
    _run_cli(["save", "user", "t", "--content", "x"])  # what the launcher calls
    capsys.readouterr()  # drain the CLI's own "saved:" line

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "cwd": str(project), "transcript_path": str(transcript), "session_id": SESSION})))
    monkeypatch.setattr(stop, "brain_tools_callable", lambda: True)
    with pytest.raises(SystemExit):
        stop.main()
    assert capsys.readouterr().out == "", "the gate must not block a turn that saved"
    row = (vault_dir / "activity.md").read_text(encoding="utf-8").splitlines()[-1]
    assert "sav=Y" in row and "pro=Y" in row


def test_the_stop_hook_never_reads_shell_commands() -> None:
    """The class, not the instance: no regex over command text may come back.

    A predicate on the model's command has to know every shape an installer,
    a wrapper or a user might spell the executable in, and it has been wrong
    four times. The hook's only sources are the structured tool_use name and
    the saver's own record."""
    src = (REPO_ROOT / "hooks" / "stop.py").read_text(encoding="utf-8")
    for forbidden in ('"Bash"', '"PowerShell"', 'get("command"', "is_cli_save_command", "_CLI_SAVE_RE"):
        assert forbidden not in src, f"stop.py inspects shell commands again: {forbidden!r}"
    assert not hasattr(stop, "is_cli_save_command")
    assert stop.count_save_events is vault.count_save_events


# ------------------------------------------- 3. recording never fails the save

def test_recording_failure_never_fails_the_save(vault_dir: Path, capsys) -> None:
    (vault_dir / ".state").write_text("a file where the directory should be", encoding="utf-8")
    result = vault.save_memory("user", "Shell", "zsh")
    vault.record_save_event("save", "cli", result.path)
    assert result.path.is_file()
    assert "could not record save event" in capsys.readouterr().err


def test_the_event_file_is_rotated(vault_dir: Path) -> None:
    for i in range(vault.SAVE_EVENTS_MAX_LINES):
        _event(vault_dir, float(i))
    vault.record_save_event("save", "cli")
    lines = (vault_dir / vault.SAVE_EVENTS_REL).read_text(encoding="utf-8").splitlines()
    assert len(lines) == vault.SAVE_EVENTS_KEEP_LINES
    assert json.loads(lines[-1])["surface"] == "cli"
    assert not list((vault_dir / ".state").glob("*.tmp")), "rotation temp file left behind"


def test_rotation_survives_crlf_lines_and_leaves_lf(vault_dir: Path) -> None:
    """Bytes read from disk must not be re-written through text mode.

    On Windows the writer used to emit CRLF (text-mode default) and the rotation
    pushed those bytes back through a text-mode write, turning every CRLF into
    CR CR LF -- `splitlines()` then reported 400 lines, half of them empty, and the
    installer self-test went red on every Windows checkout (2026-09-14). Seed the
    file with CRLF explicitly so the same case is exercised on every platform.
    """
    path = vault_dir / vault.SAVE_EVENTS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    crlf = b"".join(
        json.dumps({"ts": float(i), "kind": "save", "surface": "cli", "session": SESSION}).encode()
        + b"\r\n"
        for i in range(vault.SAVE_EVENTS_MAX_LINES)
    )
    path.write_bytes(crlf)
    vault.record_save_event("save", "cli")
    data = path.read_bytes()
    assert b"\r" not in data, "rotation must normalise to LF, never add carriage returns"
    lines = data.decode("utf-8").splitlines()
    assert len(lines) == vault.SAVE_EVENTS_KEEP_LINES
    assert all(json.loads(ln)["kind"] == "save" for ln in lines), "every kept line is one event"
    assert json.loads(lines[-1])["surface"] == "cli"
    assert vault.count_save_events(since=0) == vault.SAVE_EVENTS_KEEP_LINES, "the hook reads every kept line"
