"""The reindex lock: owned by a pid, kept alive by a heartbeat, retired only when
its owner is dead.

The old lock was a file whose *age alone* decided staleness (30 min) and whose
release was an unconditional unlink. So a rebuild longer than 30 minutes was
declared stale while alive; the next SessionStart unlinked the live lock and
spawned a second pass; the first pass's release then deleted the second's lock;
and two processes that both observed "stale" could both unlink and both create
(check-then-act). `acquire` also returned True on any non-EEXIST OSError, which
turned a read-only `.index/` into two unsynchronised writers.

Every rule here is exercised in-process against the throwaway vault; the only
subprocess is the one that supplies a pid known to be dead.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from brain_mcp import embed
from conftest import memory


def _lock(vault_dir: Path) -> Path:
    lock = vault_dir / ".index" / "reindex.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    return lock


def _plant(vault_dir: Path, content: str, age_s: float) -> Path:
    lock = _lock(vault_dir)
    lock.write_text(content, encoding="ascii")
    then = time.time() - age_s
    os.utime(lock, (then, then))
    return lock


OLD = embed.REINDEX_LOCK_STALE_S + 60
ANCIENT = embed.REINDEX_LOCK_ABANDON_S + 60


# ---------- ownership ----------

def test_acquire_records_the_owning_pid_and_release_removes_it(vault_dir: Path) -> None:
    assert embed.acquire_reindex_lock() is True
    lock = _lock(vault_dir)
    assert lock.read_text(encoding="ascii").strip() == str(os.getpid())
    assert embed.reindex_lock_held() is True
    assert embed.acquire_reindex_lock() is False, "a fresh lock is not re-acquirable"
    embed.release_reindex_lock()
    assert not lock.exists()
    assert embed.reindex_lock_held() is False


def test_release_leaves_another_process_lock_alone(vault_dir: Path) -> None:
    """The old release deleted whichever lock was there — including one a later
    pass had taken over after judging ours stale."""
    lock = _plant(vault_dir, "999999999", age_s=0)
    embed.release_reindex_lock()
    assert lock.exists(), "release() unlinked a lock this process does not own"
    assert lock.read_text(encoding="ascii") == "999999999"


def test_release_leaves_a_pidless_lock_alone(vault_dir: Path) -> None:
    lock = _plant(vault_dir, "", age_s=0)
    embed.release_reindex_lock()
    assert lock.exists()


# ---------- staleness = old AND dead ----------

def test_an_old_lock_with_a_live_owner_is_still_held(vault_dir: Path) -> None:
    """The regression proper: a rebuild past REINDEX_LOCK_STALE_S is not abandoned."""
    lock = _plant(vault_dir, str(os.getpid()), age_s=OLD)
    assert embed._lock_state(lock) == "held"
    assert embed.reindex_lock_held() is True
    assert embed.acquire_reindex_lock() is False
    assert lock.read_text(encoding="ascii") == str(os.getpid()), "the live lock was replaced"


def test_an_old_lock_with_a_dead_owner_is_taken_over(vault_dir: Path, dead_pid: int) -> None:
    lock = _plant(vault_dir, str(dead_pid), age_s=OLD)
    assert embed._lock_state(lock) == "stale"
    assert embed.reindex_lock_held() is False
    assert embed.acquire_reindex_lock() is True
    assert lock.read_text(encoding="ascii").strip() == str(os.getpid())
    assert not list(lock.parent.glob("reindex.lock.stale-*")), "tombstone left behind"


def test_a_fresh_lock_with_a_dead_owner_is_still_held(vault_dir: Path, dead_pid: int) -> None:
    """Age gates the pid check. A just-written lock whose owner is dead is the
    window between a crash and the stale threshold, and also the window between
    O_EXCL create and pid write — neither may be taken over on sight."""
    lock = _plant(vault_dir, str(dead_pid), age_s=10)
    assert embed._lock_state(lock) == "held"
    assert embed.acquire_reindex_lock() is False


def test_an_old_lock_with_an_unreadable_pid_falls_back_to_the_age_test(vault_dir: Path) -> None:
    for content in ("", "not-a-pid", "-1"):
        lock = _plant(vault_dir, content, age_s=OLD)
        assert embed._lock_state(lock) == "stale", f"content={content!r}"
        lock.unlink()
    lock = _plant(vault_dir, "not-a-pid", age_s=10)
    assert embed._lock_state(lock) == "held"


def test_an_ancient_lock_is_abandoned_even_if_its_pid_is_alive(vault_dir: Path) -> None:
    """Bounds pid reuse: a heartbeat keeps a live pass under this age, so only a
    dead pass whose pid was handed to an unrelated process can reach it."""
    lock = _plant(vault_dir, str(os.getpid()), age_s=ANCIENT)
    assert embed._lock_state(lock) == "stale"


def test_pid_liveness_probe(dead_pid: int) -> None:
    assert embed._pid_alive(os.getpid()) is True
    assert embed._pid_alive(dead_pid) is False
    assert embed._pid_alive(0) is False
    assert embed._pid_alive(-5) is False


# ---------- failure modes of acquire ----------

def test_acquire_does_not_claim_success_on_an_unexpected_oserror(
    vault_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = os.open

    def denied(path, flags, *a, **k):
        if str(path).endswith("reindex.lock"):
            raise PermissionError(13, "denied", str(path))
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", denied)
    assert embed.acquire_reindex_lock() is False, (
        "acquire() reported ownership of a lock it could not create — two passes "
        "would then write the index at once"
    )


def test_acquire_does_not_claim_success_when_the_index_dir_cannot_be_made(
    vault_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(self, *a, **k):
        raise PermissionError(13, "denied", str(self))

    monkeypatch.setattr(Path, "mkdir", denied)
    assert embed.acquire_reindex_lock() is False


# ---------- takeover races ----------

def test_takeover_never_retires_a_lock_it_did_not_judge(vault_dir: Path, dead_pid: int) -> None:
    """Two processes observe the same stale lock. P1 retires it and creates a
    fresh one; P2, acting on its earlier verdict, must not clear P1's lock."""
    lock = _plant(vault_dir, str(dead_pid), age_s=OLD)
    judged_by_p2 = lock.stat()

    # P1 wins the takeover.
    assert embed.acquire_reindex_lock() is True
    p1_content = lock.read_text(encoding="ascii")

    # P2 proceeds with its stale verdict.
    assert embed._retire_stale_lock(lock, judged_by_p2) is False
    assert lock.exists(), "P2 cleared P1's fresh lock"
    assert lock.read_text(encoding="ascii") == p1_content
    assert not list(lock.parent.glob("reindex.lock.stale-*"))


def test_takeover_retires_exactly_the_lock_it_judged(vault_dir: Path, dead_pid: int) -> None:
    lock = _plant(vault_dir, str(dead_pid), age_s=OLD)
    assert embed._retire_stale_lock(lock, lock.stat()) is True
    assert not lock.exists()
    assert not list(lock.parent.glob("reindex.lock.stale-*"))


def test_second_of_two_takeovers_loses(vault_dir: Path, dead_pid: int, monkeypatch) -> None:
    """Interleave at the O_EXCL: the loser's second create must fail, not loop."""
    _plant(vault_dir, str(dead_pid), age_s=OLD)
    real_open = os.open
    calls = {"n": 0}

    def racing_open(path, flags, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2 and str(path).endswith("reindex.lock"):
            # The other process created its lock between our retire and our create.
            Path(path).write_text("31337", encoding="ascii")
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", racing_open)
    assert embed.acquire_reindex_lock() is False
    assert _lock(vault_dir).read_text(encoding="ascii") == "31337"


# ---------- heartbeat ----------

def test_heartbeat_touches_only_an_owned_lock(vault_dir: Path) -> None:
    mine = _plant(vault_dir, str(os.getpid()), age_s=OLD)
    embed._heartbeat_reindex_lock()
    assert time.time() - mine.stat().st_mtime < 5

    theirs = _plant(vault_dir, "999999999", age_s=OLD)
    embed._heartbeat_reindex_lock()
    assert time.time() - theirs.stat().st_mtime > OLD - 5, "heartbeat touched a foreign lock"


class _StubEmbedder:
    def embed_many(self, texts):
        return [[1.0] * embed.EMBED_DIM for _ in texts]

    def embed_one(self, text):
        return [1.0] * embed.EMBED_DIM


def test_an_unbounded_sync_heartbeats_the_lock_per_chunk(
    vault_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heartbeat lives in sync(), after the chunk commit: that is the unit of
    progress, and a pass that stops committing *should* start looking stale."""
    monkeypatch.delenv("BRAIN_EMBED", raising=False)
    monkeypatch.setattr(embed, "_EMBEDDER", _StubEmbedder())
    for i in range(3 * embed.EmbedIndex.SYNC_CHUNK):
        memory(vault_dir / "user" / f"m{i}.md", f"m{i}", "user", f"memory number {i}")

    assert embed.acquire_reindex_lock() is True
    lock = _lock(vault_dir)
    then = time.time() - OLD
    os.utime(lock, (then, then))

    touches: list[float] = []
    real_utime = os.utime

    def spy(path, *a, **k):
        if Path(path) == lock:
            touches.append(time.time())
        return real_utime(path, *a, **k)

    monkeypatch.setattr(os, "utime", spy)
    try:
        done = embed.EmbedIndex.sync(budget_seconds=0)
    finally:
        monkeypatch.setattr(os, "utime", real_utime)
        embed.release_reindex_lock()

    assert done == 3 * embed.EmbedIndex.SYNC_CHUNK
    assert len(touches) == 3, "one heartbeat per committed chunk"
