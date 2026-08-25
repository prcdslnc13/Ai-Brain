"""Two checkpoints in the same instant must produce two files, not one.

`write_checkpoint` named files `<YYYY-MM-DD-HHMM>-<machine>.md`. PreCompact firing
and SessionEnd firing seconds later are the ordinary case, and both landed on the
same path -- the second silently replaced the first, so the checkpoint that captured
the *most* context was the one destroyed (reproduced 2026-08-25).

Seconds in the stamp is not the fix on its own: the writers are separate processes,
so anything that tests-then-writes still races. The reservation is O_EXCL.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from brain_mcp import compact, render, vault

# Characters Windows refuses in a filename, plus the separators. A vault syncs to a
# Windows machine, so a name only POSIX accepts is a file that cannot arrive there.
_WINDOWS_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _checkpoints(vault_dir: Path, project: str = "Widget") -> list[Path]:
    return sorted((vault_dir / "projects" / project / "sessions").glob("*.md"))


def test_many_checkpoints_in_one_instant_all_survive(vault_dir: Path) -> None:
    bodies = [f"Body number {i}." for i in range(12)]
    paths = [vault.write_checkpoint("Widget", b) for b in bodies]

    assert len(set(paths)) == len(bodies), "checkpoints collapsed onto the same path"
    assert len(_checkpoints(vault_dir)) == len(bodies)
    on_disk = {p.read_text(encoding="utf-8") for p in _checkpoints(vault_dir)}
    for body in bodies:
        assert any(body in text for text in on_disk), f"lost: {body}"


def test_precompact_then_sessionend_keeps_both(vault_dir: Path) -> None:
    """The exact trigger: two lifecycle hooks for one session, back to back."""
    a = vault.write_checkpoint("Widget", "PreCompact snapshot.")
    b = vault.write_checkpoint("Widget", "SessionEnd snapshot.")
    assert a != b
    assert "PreCompact snapshot." in a.read_text(encoding="utf-8")
    assert "SessionEnd snapshot." in b.read_text(encoding="utf-8")


def test_names_are_sortable_and_carry_the_machine(vault_dir: Path) -> None:
    """CLAUDE.md: the machine suffix is how the user finds where uncommitted work
    lives. And string order must still track write order."""
    paths = [vault.write_checkpoint("Widget", f"b{i}") for i in range(5)]
    names = [p.name for p in paths]
    assert names == sorted(names), f"not chronologically sortable: {names}"
    for name in names:
        assert "test-host" in name, name
        assert name.startswith(datetime.now().strftime("%Y-%m-%d-")), name


def test_new_names_sort_after_legacy_minute_precision_names(vault_dir: Path) -> None:
    """The vault already holds hundreds of `...-HHMM-<machine>.md` files. A mixed
    directory must not sort a new checkpoint above an older one from the same minute."""
    sessions = vault_dir / "projects" / "Widget" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    legacy = sessions / "2026-08-25-1249-test-host.md"
    legacy.write_text("old\n", encoding="utf-8")
    modern = sessions / "2026-08-25-124930-test-host.md"
    modern.write_text("new\n", encoding="utf-8")
    assert sorted(p.name for p in sessions.glob("*.md")) == [legacy.name, modern.name]


@pytest.mark.parametrize("machine", ["test-host", "joes-macbook-pro-3", "strixlappy"])
def test_filenames_are_legal_on_windows(vault_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                        machine: str) -> None:
    monkeypatch.setenv("BRAIN_MACHINE", machine)
    monkeypatch.setattr(vault, "_machine_name_cache", None)
    for _ in range(3):
        path = vault.write_checkpoint("Widget", "b")
        assert not _WINDOWS_ILLEGAL.search(path.name), path.name
        assert not path.name.endswith((" ", "."))
        assert len(path.name) < 120


def test_a_failed_write_leaves_no_empty_checkpoint(vault_dir: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """The reservation creates a real, empty `.md`. Left behind, it becomes the
    newest file in sessions/ and therefore the preload's latest-session slot."""
    def boom(path, text):
        raise OSError("disk full")

    monkeypatch.setattr(vault, "_atomic_write", boom)
    with pytest.raises(OSError):
        vault.write_checkpoint("Widget", "never lands")
    assert _checkpoints(vault_dir) == []


def test_concurrent_writers_from_separate_processes(vault_dir: Path) -> None:
    """A thread-level test would not exercise the bug: the colliding writers are
    PreCompact and SessionEnd, two OS processes with no shared lock."""
    script = textwrap.dedent(
        """
        import sys
        from brain_mcp import vault
        print(vault.write_checkpoint("Widget", "body from worker " + sys.argv[1]))
        """
    )
    worker = vault_dir.parent / "worker.py"
    worker.write_text(script, encoding="utf-8")

    env = dict(os.environ)
    env["BRAIN_VAULT"] = str(vault_dir.parent)
    env["BRAIN_EMBED"] = "0"
    env["BRAIN_MACHINE"] = "test-host"
    # Point at the SOURCE tree, not the non-editable install in .venv: the
    # suite grades what is in the repo (see CLAUDE.md "Testing").
    env["PYTHONPATH"] = str(Path(vault.__file__).resolve().parents[1])

    procs = [
        subprocess.Popen([sys.executable, str(worker), str(i)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, env=env)
        for i in range(6)
    ]
    outs = [p.communicate() for p in procs]
    for (out, err), p in zip(outs, procs):
        assert p.returncode == 0, err

    written = _checkpoints(vault_dir)
    # Proves the workers ran the source tree, not the non-editable install: only the
    # new scheme puts six digits (HHMMSS) after the date.
    stamp_re = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{6}-test-host(_\d{2})?\.md$")
    for w in written:
        assert stamp_re.match(w.name), w.name
    assert len(written) == 6, [p.name for p in written]
    bodies = {p.read_text(encoding="utf-8") for p in written}
    assert len(bodies) == 6, "two processes wrote the same content into one file"
    for i in range(6):
        assert any(f"body from worker {i}" in b for b in bodies), f"worker {i} lost"


def test_atomic_write_temp_names_are_unique_per_writer(vault_dir: Path) -> None:
    """Two writers of the SAME destination used to share `<name>.md.tmp` and
    interleave their bytes into it before both renamed it over the real note."""
    seen = set()
    real_replace = os.replace

    def spy(src, dst):
        seen.add(Path(src).name)
        real_replace(src, dst)

    target = vault_dir / "user" / "same-note.md"
    import brain_mcp.vault as v
    orig = v.os.replace
    v.os.replace = spy
    try:
        for i in range(5):
            v._atomic_write(target, f"body {i}\n")
    finally:
        v.os.replace = orig

    assert len(seen) == 5, f"temp names collided: {seen}"
    assert all(not n.endswith(".md") for n in seen), (
        "temp files must not end in .md or vault globs and the embed index see them"
    )
    assert target.read_text(encoding="utf-8") == "body 4\n"


# ------------------------------------------------- downstream consumers still work

def test_latest_session_selection_still_picks_the_newest(vault_dir: Path) -> None:
    first = vault.write_checkpoint("Widget", "oldest checkpoint body")
    last = vault.write_checkpoint("Widget", "newest checkpoint body")
    # Consumers sort by mtime, and same-second writes can tie; make the intent explicit.
    old_time = (datetime.now() - timedelta(hours=2)).timestamp()
    os.utime(first, (old_time, old_time))

    bundle = vault.session_start_bundle("Widget")
    labels = {s["label"]: s for s in bundle["sections"]}
    latest = labels["project:Widget:latest-session"]["items"][0]
    assert "newest checkpoint body" in latest["content"]
    assert Path(latest["path"]).name == last.name


def test_compaction_handles_the_new_names(vault_dir: Path) -> None:
    """Rollups dedupe by source filename and bucket by mtime -- neither may care
    what the stamp looks like."""
    sessions = vault_dir / "projects" / "Widget" / "sessions"
    paths = [vault.write_checkpoint("Widget", f"body {i}") for i in range(4)]
    aged = (datetime.now() - timedelta(days=10)).timestamp()
    for p in paths:
        os.utime(p, (aged, aged))

    counts = compact._compact_project(sessions.parent, vault_dir / "archive", dry_run=False)
    assert counts["raw_to_daily"] == 4
    assert not list(sessions.glob("*.md")), "raw checkpoints should have been rolled up"

    daily = list((sessions / "daily").glob("*.md"))
    assert len(daily) == 1
    text = daily[0].read_text(encoding="utf-8")
    for p in paths:
        assert f"## {p.name}" in text, f"{p.name} missing from the rollup"
    for i in range(4):
        assert f"body {i}" in text

    # Idempotent: a second run must not duplicate the sources.
    compact._compact_project(sessions.parent, vault_dir / "archive", dry_run=False)
    assert daily[0].read_text(encoding="utf-8") == text


def test_checkpoints_are_still_classified_as_sessions(vault_dir: Path) -> None:
    """The suffixed `-2` variant must not fall out of the session filter and start
    showing up in ordinary recalls."""
    a = vault.write_checkpoint("Widget", "alpha checkpoint")
    b = vault.write_checkpoint("Widget", "alpha checkpoint too")
    assert a.name != b.name
    for p in (a, b):
        assert vault.is_session_path(p)
    payload = render.recall_payload(query="alpha")
    assert payload["shown"] == 0
    assert payload["sessions_excluded"] >= 2
