"""A locked vector index must never be reported as a corrupt one.

Reproduces the 2026-08-24 false alarm: `brain doctor` connected to the index with
sqlite's 5s default busy timeout and mapped every `DatabaseError` — including
"database is locked" — onto INDEX_CORRUPT, whose hint tells the user to delete the
index. A reindex holds the write lock across its chunk commits, and SessionStart
both kicks a reindex and renders the doctor banner, so a perfectly healthy index
produced destructive advice on an ordinary session start.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from brain_mcp import doctor

PACKAGE = Path(doctor.__file__).resolve().parent


@pytest.fixture
def index_db(vault_dir: Path) -> Path:
    """A small, structurally valid embeddings index inside the throwaway vault."""
    idx = vault_dir / ".index" / "embeddings.sqlite"
    idx.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(idx)
    conn.execute(
        "CREATE TABLE embeddings (path TEXT PRIMARY KEY, mtime REAL NOT NULL, "
        "vector BLOB NOT NULL)"
    )
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO embeddings VALUES ('user/x.md', 1.0, ?)", (b"\x00" * 16,)
    )
    conn.commit()
    conn.close()
    return idx


def test_healthy_index_reports_ok(vault_dir: Path, index_db: Path) -> None:
    findings = doctor._check_vector_index(vault_dir)
    assert [f.code for f in findings] == ["INDEX_OK"]


def test_locked_index_is_not_reported_as_corrupt(vault_dir: Path, index_db: Path) -> None:
    """The regression test proper: hold the write lock, then run the check."""
    holder = sqlite3.connect(index_db, timeout=30)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        # Drop the timeout to keep the test fast; the point under test is the
        # classification of the error, not how long we wait for the lock.
        findings = _check_with_timeout(vault_dir, timeout=0.05)
    finally:
        holder.rollback()
        holder.close()

    codes = [f.code for f in findings]
    assert "INDEX_CORRUPT" not in codes, (
        "a locked index was classified as corrupt; the hint tells the user to delete "
        "it, costing a full re-embed of valid vectors"
    )
    assert codes == ["INDEX_BUSY"]
    assert all(f.severity in ("ok", "info") for f in findings)
    assert all("delete" not in (f.hint or "").lower() for f in findings), (
        "a busy index must never carry destructive advice"
    )


def test_a_genuinely_corrupt_index_is_still_reported(vault_dir: Path) -> None:
    """The lock carve-out must not swallow real corruption."""
    idx = vault_dir / ".index" / "embeddings.sqlite"
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_bytes(b"this is not a sqlite database, not even a little")

    findings = doctor._check_vector_index(vault_dir)
    assert [f.code for f in findings] == ["INDEX_CORRUPT"]
    assert findings[0].severity == "warn"


def _check_with_timeout(brain: Path, timeout: float):
    original = doctor.index_busy_timeout
    doctor.index_busy_timeout = lambda: timeout
    try:
        return doctor._check_vector_index(brain)
    finally:
        doctor.index_busy_timeout = original


def _balanced_call(text: str, start: int) -> str:
    """The argument text of a call whose opening paren ends at `start`.

    Paren-balanced rather than regex-matched: `sqlite3.connect(_index_path(), ...)`
    nests, and a non-greedy `\\(.*?\\)` stops at the inner close and reports a
    correctly-configured call as an offender.
    """
    depth = 1
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start:i]
    return text[start:]


def test_lock_error_classifier() -> None:
    assert doctor._is_lock_error(sqlite3.OperationalError("database is locked"))
    assert doctor._is_lock_error(sqlite3.OperationalError("database table is busy"))
    assert not doctor._is_lock_error(sqlite3.DatabaseError("file is not a database"))
    assert not doctor._is_lock_error(sqlite3.OperationalError("no such table: embeddings"))


def test_doctor_waits_far_less_than_a_writer() -> None:
    """A read-only health check must not wait like a writer.

    embed's 30s is right for writers, where waiting beats failing. Doctor runs inside
    the SessionStart hook, which Claude Code kills at 15s — and a killed hook drops
    the whole preload, which is worse than any finding it might have produced.
    """
    from brain_mcp import embed
    assert 0 < doctor.index_busy_timeout() < embed.SQLITE_BUSY_TIMEOUT_S
    assert doctor.index_busy_timeout() <= 5.0, (
        "doctor's index timeout is a meaningful fraction of the 15s hook budget, and "
        "three checks connect"
    )


SESSION_START_HOOK_TIMEOUT_S = 15  # templates/settings.hooks*.json


def test_doctor_stays_inside_the_hook_budget_during_a_reindex(
    populated_vault: Path, monkeypatch: pytest.MonkeyPatch, index_db: Path
) -> None:
    """The whole check must finish well inside SessionStart's timeout under a lock.

    Four checks touch the index. Before the reindex-lock fast path they connected
    one after another and each paid the busy timeout, so their cost under a running
    reindex was additive — measured 41.8s for `_check_vector_index` alone at the
    writer's 30s setting, nearly 3x the hook's entire budget.
    """
    import time

    monkeypatch.setenv("BRAIN_EMBED", "1")
    lock = populated_vault / ".index" / "reindex.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("12345", encoding="utf-8")

    holder = sqlite3.connect(index_db, timeout=30)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        start = time.monotonic()
        findings = doctor.check("Widget")
        elapsed = time.monotonic() - start
    finally:
        holder.rollback()
        holder.close()

    assert elapsed < SESSION_START_HOOK_TIMEOUT_S / 3, (
        f"doctor.check() took {elapsed:.1f}s while a reindex held the index lock; "
        f"the SessionStart hook budget is {SESSION_START_HOOK_TIMEOUT_S}s and the "
        f"bundle still has to be built after this returns"
    )
    codes = [f["code"] for f in findings]
    assert "INDEX_CORRUPT" not in codes
    assert "INDEX_BUSY" in codes


def test_reindex_lock_defers_index_checks(populated_vault: Path, index_db: Path) -> None:
    """While a reindex runs, index findings are deferred rather than wrong.

    INDEX_STALE in particular would be actively misleading: the backlog it reports
    is the one being drained as it reports it.
    """
    lock = populated_vault / ".index" / "reindex.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("12345", encoding="utf-8")

    assert doctor._reindex_running() is True
    assert [f.code for f in doctor._check_vector_index(populated_vault)] == ["INDEX_BUSY"]
    assert doctor._check_near_duplicates(populated_vault) == []

    lock.unlink()
    assert doctor._reindex_running() is False


def test_a_stale_lock_does_not_defer_checks_forever(
    populated_vault: Path, index_db: Path, dead_pid: int
) -> None:
    """A lock left behind by a killed process must not mute the checks permanently."""
    import os
    import time

    from brain_mcp import embed

    lock = populated_vault / ".index" / "reindex.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(dead_pid), encoding="utf-8")
    ancient = time.time() - (embed.REINDEX_LOCK_STALE_S + 60)
    os.utime(lock, (ancient, ancient))

    assert doctor._reindex_running() is False
    assert [f.code for f in doctor._check_vector_index(populated_vault)] == ["INDEX_OK"]


def test_every_index_connection_sets_a_busy_timeout() -> None:
    """No connection to the vector index may keep sqlite's 5s default.

    This is the invariant CLAUDE.md already claimed held ("every connection carries
    a 30s busy timeout") while two of doctor's did not. Asserting it as text is what
    keeps a newly-added connection from quietly reintroducing the false alarm.
    """
    offenders = []
    for source in sorted(PACKAGE.glob("*.py")):
        # transcript.py reads a foreign harness's event log, not our index.
        if source.name == "transcript.py":
            continue
        text = source.read_text(encoding="utf-8")
        for match in re.finditer(r"sqlite3\.connect\(", text):
            call = _balanced_call(text, match.end())
            if "timeout" in call:
                continue
            offenders.append(f"{source.name}:{text[: match.start()].count(chr(10)) + 1}")
    assert not offenders, (
        f"sqlite3.connect without a busy timeout at {offenders}. A reindex holds the "
        f"write lock longer than sqlite's 5s default, so these fail during one."
    )


@pytest.mark.parametrize("check", ["_check_index_stale", "_check_index_recipe"])
def test_backlog_and_recipe_checks_wait_like_doctor_not_like_a_writer(
    vault_dir: Path, index_db: Path, check: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two checks that connect *through embed* must honour doctor's timeout.

    `_check_vector_index` connected with `index_busy_timeout()` while
    `_check_index_stale` -> `backlog()` and `_check_index_recipe` ->
    `text_recipe_changed()` opened their own connections with the writer's 30s —
    so CLAUDE.md's "doctor waits 2s" was true of one check in three, and the hook
    could still blow its 15s budget behind a reindex. Both now take a `timeout=`
    and doctor passes its own.
    """
    import time

    monkeypatch.setattr(doctor, "index_busy_timeout", lambda: 0.05)
    holder = sqlite3.connect(index_db, timeout=30)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        start = time.monotonic()
        findings = getattr(doctor, check)(vault_dir)
        elapsed = time.monotonic() - start
    finally:
        holder.rollback()
        holder.close()

    assert elapsed < 1.0, f"{check} waited {elapsed:.1f}s on a locked index"
    codes = [f.code for f in findings]
    # Deferred, not wrong: a locked index has an *unknown* backlog and recipe.
    assert "INDEX_FRESH" not in codes, "a locked index was reported as up to date"
    assert "INDEX_STALE" not in codes
    assert "INDEX_RECIPE_STALE" not in codes
    assert "INDEX_CORRUPT" not in codes


def test_doctor_passes_its_own_timeout_into_embed() -> None:
    """Text invariant: every embed call doctor makes that can connect must carry
    `timeout=index_busy_timeout()`, or the 2s budget is a claim about one check
    in three again."""
    text = Path(doctor.__file__).read_text(encoding="utf-8")
    for fn in ("backlog", "text_recipe_changed"):
        calls = [m for m in re.finditer(rf"embed\.(?:EmbedIndex\.)?{fn}\(", text)]
        assert calls, f"doctor no longer calls embed.{fn}(); update this test"
        for m in calls:
            call = _balanced_call(text, m.end())
            assert "index_busy_timeout()" in call, (
                f"doctor calls embed.{fn}() without its own timeout: ({call})"
            )
