"""One unreadable note must cost the index one row, not the whole pass.

`embed_text()` read each file with strict UTF-8 and the sync batch loop caught
only OSError. UnicodeDecodeError is a ValueError, so a single cp1252 note escaped
`sync()`: every foreground recall fell back to ripgrep (search_memories treats the
raise as "embed unavailable"), `brain reindex` crashed, and the SessionStart-
spawned child died with its stderr on DEVNULL — so INDEX_STALE warned forever and
nothing said why. Because chunks run newest-first and the failing chunk never
committed, nothing older than the bad note was ever indexed again either.

These tests run `sync()` for real against a throwaway vault with the embedder
stubbed out, so they need neither fastembed nor the cached model.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from brain_mcp import embed, vault
from conftest import memory

DIM = embed.EMBED_DIM


class _StubEmbedder:
    """Deterministic vectors, no model. Records what it was asked to embed."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed_many(self, texts):
        self.texts.extend(texts)
        return [[float(len(t) % 7) + 1.0] * DIM for t in texts]

    def embed_one(self, text):
        return self.embed_many([text])[0]


@pytest.fixture
def stub_embedder(vault_dir: Path, monkeypatch: pytest.MonkeyPatch) -> _StubEmbedder:
    monkeypatch.delenv("BRAIN_EMBED", raising=False)
    stub = _StubEmbedder()
    monkeypatch.setattr(embed, "_EMBEDDER", stub)
    return stub


def _rows(root: Path) -> dict[str, float]:
    conn = sqlite3.connect(root / ".index" / "embeddings.sqlite")
    try:
        return dict(conn.execute("SELECT path, mtime FROM embeddings").fetchall())
    finally:
        conn.close()


def _latin1_note(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "---\nname: caf\xe9\ndescription: caf\xe9 note\ntype: user\n---\n\nUn caf\xe9 tr\xe8s fort.\n"
    path.write_bytes(body.encode("cp1252"))
    return path


def test_one_undecodable_note_does_not_abort_the_pass(
    vault_dir: Path, stub_embedder: _StubEmbedder, capsys: pytest.CaptureFixture
) -> None:
    good = [
        memory(vault_dir / "user" / "a.md", "a", "user", "alpha memory"),
        memory(vault_dir / "feedback" / "b.md", "b", "feedback", "bravo memory"),
        memory(vault_dir / "references" / "c.md", "c", "reference", "charlie memory"),
    ]
    bad = _latin1_note(vault_dir / "user" / "cafe.md")
    # Make the bad note the *newest*, so it lands in the first chunk — the exact
    # shape that used to strand everything older.
    newest = max(p.stat().st_mtime for p in good) + 10
    os.utime(bad, (newest, newest))

    done = embed.EmbedIndex.sync(budget_seconds=0)  # must not raise

    assert done == len(good)
    rows = _rows(vault_dir)
    assert set(rows) == {embed._index_key(p, vault_dir) for p in good}
    assert embed._index_key(bad, vault_dir) not in rows

    err = capsys.readouterr().err
    assert err.count("brain embed: skipped") == 1, "report the skip once per pass, not per file"
    assert "cafe.md" in err


def test_foreground_sync_survives_it_too(vault_dir: Path, stub_embedder: _StubEmbedder) -> None:
    """The time-boxed path a recall takes — the one that turned into a permanent
    ripgrep fallback."""
    memory(vault_dir / "user" / "a.md", "a", "user", "alpha memory")
    _latin1_note(vault_dir / "user" / "cafe.md")
    assert embed.EmbedIndex.sync() == 1


def test_upsert_of_an_undecodable_note_is_a_no_op(
    vault_dir: Path, stub_embedder: _StubEmbedder
) -> None:
    bad = _latin1_note(vault_dir / "user" / "cafe.md")
    embed.EmbedIndex.upsert(bad)  # must not raise: a save must never fail here
    assert not embed._index_path().exists() or bad.name not in str(_rows(vault_dir))


# ---------- head-only reads ----------

def _big_note(path: Path, body_bytes: int) -> tuple[Path, str]:
    head = "---\nname: big\ndescription: big note\ntype: project\n---\n\n"
    text = head + ("lorem ipsum dolor sit amet. " * (body_bytes // 28 + 1))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path, text


def test_a_budgeted_read_stops_at_the_head(vault_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Under a slice budget, embed_text reads EMBED_READ_BYTES, not the file."""
    path, _ = _big_note(vault_dir / "projects" / "P" / "big.md", 200_000)
    seen: list[int] = []
    real_open = open

    def spy_open(file, mode="r", *a, **k):
        fh = real_open(file, mode, *a, **k)
        if "b" in mode and Path(file) == path:
            real_read = fh.read

            def read(n=-1):
                seen.append(n)
                return real_read(n)

            fh.read = read
        return fh

    monkeypatch.setattr("builtins.open", spy_open)
    text = embed.embed_text(path)
    assert seen and seen[0] == embed.EMBED_READ_BYTES
    assert all(n >= 0 for n in seen), f"unbounded read of a budgeted file: {seen}"
    assert len(text) <= embed._embed_text_budget()
    assert text.startswith("big")


def test_a_multibyte_character_on_the_read_boundary_is_not_an_error(vault_dir: Path) -> None:
    """A valid UTF-8 file whose 16 KB boundary splits a 2-byte character must
    decode cleanly; incremental decoding holds the partial sequence back."""
    head = b"---\nname: edge\ndescription: edge\ntype: user\n---\n\n"
    fill = embed.EMBED_READ_BYTES - len(head)
    if fill % 2 == 0:
        head += b"x"
        fill -= 1
    # `fill` is odd, so the last byte before the boundary is the first byte of
    # an "é" and the boundary lands in the middle of it.
    body = b"a" * (fill - 1) + "é".encode("utf-8") * 2000
    path = vault_dir / "user" / "edge.md"
    path.write_bytes(head + body)
    assert path.stat().st_size > embed.EMBED_READ_BYTES

    text = embed.embed_text(path)  # must not raise
    assert text.startswith("edge")


def test_the_raw_file_recipe_still_reads_the_whole_file(
    vault_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAIN_EMBED_CHARS", "0")
    path, full = _big_note(vault_dir / "projects" / "P" / "big.md", 100_000)
    assert embed.embed_text(path) == full


def test_the_slice_is_unchanged_by_the_head_read(vault_dir: Path) -> None:
    """Reading the head must not change what reaches the model — otherwise
    EMBED_TEXT_VERSION would need a bump and every vault a rebuild."""
    path, full = _big_note(vault_dir / "projects" / "P" / "big.md", 100_000)
    mem = vault.Memory.from_text(path, full)
    body, desc = mem.body.strip(), mem.description.strip()
    parts = [mem.name] + ([desc] if not body.startswith(desc[:80]) else []) + ["", body]
    expected = "\n".join(parts)[: embed._embed_text_budget()]
    assert embed.embed_text(path) == expected


# ---------- the detached child's diagnostics ----------

def test_spawned_reindex_logs_to_the_index_dir_and_truncates(
    vault_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BRAIN_EMBED", raising=False)
    monkeypatch.delenv("BRAIN_AUTO_REINDEX", raising=False)
    memory(vault_dir / "user" / "a.md", "a", "user", "alpha memory")  # backlog >= 1

    log = embed.reindex_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("x" * 5000, encoding="utf-8")  # a previous pass's output

    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        # Emulate the child writing while the parent has already closed its copy.
        kwargs["stdout"].write(b"reindexed 1 file(s)\n")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert embed.spawn_background_reindex() is True

    kw = captured["kwargs"]
    assert kw["stdout"] is not subprocess.DEVNULL, "the child's output is discarded"
    assert Path(kw["stdout"].name).resolve() == log.resolve()
    assert kw["stderr"] is subprocess.STDOUT, "stderr must land in the same log"
    assert kw["stdout"].closed, "the parent must not keep the log handle open"
    assert kw["stdin"] is subprocess.DEVNULL
    assert captured["argv"][-1] == "reindex"

    content = log.read_bytes()
    assert b"x" * 100 not in content, "the log was appended to, not truncated"
    assert content.startswith(b"reindexed")
    assert log.parent == embed._reindex_lock_path().parent, "log lives beside the lock"


def test_spawn_still_runs_when_the_log_cannot_be_opened(
    vault_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Diagnostics are worth less than the reindex itself."""
    monkeypatch.delenv("BRAIN_EMBED", raising=False)
    monkeypatch.delenv("BRAIN_AUTO_REINDEX", raising=False)
    memory(vault_dir / "user" / "a.md", "a", "user", "alpha memory")

    log = embed.reindex_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.mkdir()  # a directory where the file should be: open() raises

    captured: dict = {}
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: captured.update(kw))
    assert embed.spawn_background_reindex() is True
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL
