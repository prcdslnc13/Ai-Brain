"""A recipe rebuild resumes where it died; it does not start over.

The recipe used to be one stamp in `meta`, written only after a rebuild's last
chunk. Every row counted as pending while the stamp was stale, so an interrupted
rebuild — a killed SessionStart child, a closed laptop — restarted from row one
and, on a large vault, could never finish. The recipe is now stored per row: a
rebuild's pending set is "rows whose recipe differs", and rows a previous pass
already re-embedded are skipped.

The embedder is stubbed, so these run without fastembed or the cached model.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from brain_mcp import embed
from conftest import memory

N = 3 * embed.EmbedIndex.SYNC_CHUNK  # three chunks of memories


class _StubEmbedder:
    def __init__(self) -> None:
        self.calls = 0
        self.texts: list[str] = []
        self.fail_on_call: int | None = None

    def embed_many(self, texts):
        self.calls += 1
        if self.fail_on_call is not None and self.calls == self.fail_on_call:
            raise RuntimeError("simulated kill mid-rebuild")
        self.texts.extend(texts)
        return [[float(embed.EMBED_TEXT_VERSION)] * embed.EMBED_DIM for _ in texts]

    def embed_one(self, text):
        return self.embed_many([text])[0]


@pytest.fixture
def stub(vault_dir: Path, monkeypatch: pytest.MonkeyPatch) -> _StubEmbedder:
    monkeypatch.delenv("BRAIN_EMBED", raising=False)
    s = _StubEmbedder()
    monkeypatch.setattr(embed, "_EMBEDDER", s)
    for i in range(N):
        memory(vault_dir / "user" / f"m{i:02d}.md", f"m{i}", "user", f"memory number {i}")
    return s


def _recipes(root: Path) -> dict[str, str | None]:
    conn = sqlite3.connect(root / ".index" / "embeddings.sqlite")
    try:
        return dict(conn.execute("SELECT path, recipe FROM embeddings").fetchall())
    finally:
        conn.close()


def _meta(root: Path, key: str) -> str | None:
    conn = sqlite3.connect(root / ".index" / "embeddings.sqlite")
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_rows_carry_the_recipe_they_were_built_with(vault_dir: Path, stub: _StubEmbedder) -> None:
    assert embed.EmbedIndex.sync(budget_seconds=0) == N
    assert set(_recipes(vault_dir).values()) == {embed._text_recipe_id()}
    assert _meta(vault_dir, "text_recipe") == embed._text_recipe_id()


def test_an_interrupted_rebuild_resumes_instead_of_restarting(
    vault_dir: Path, stub: _StubEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embed, "EMBED_TEXT_VERSION", 1)
    old = embed._text_recipe_id()
    assert embed.EmbedIndex.sync(budget_seconds=0) == N
    epoch_before = _meta(vault_dir, "vector_epoch")

    # Recipe bump. The first unbounded pass dies after one chunk.
    monkeypatch.setattr(embed, "EMBED_TEXT_VERSION", 2)
    new = embed._text_recipe_id()
    assert embed.text_recipe_changed()
    stub.calls, stub.texts, stub.fail_on_call = 0, [], 2
    with pytest.raises(RuntimeError):
        embed.EmbedIndex.sync(budget_seconds=0)

    recipes = _recipes(vault_dir)
    done_rows = [p for p, r in recipes.items() if r == new]
    assert len(done_rows) == embed.EmbedIndex.SYNC_CHUNK, "the first chunk's commit was kept"
    assert len(recipes) == N, "no row was deleted for the rebuild"
    assert _meta(vault_dir, "text_recipe") == old, "an interrupted rebuild must not look complete"
    assert embed.text_recipe_changed()
    assert _meta(vault_dir, "vector_epoch") != epoch_before, "replaced vectors must bump the epoch"

    # Second pass: only the remainder is embedded, then the stamp lands.
    stub.calls, stub.texts, stub.fail_on_call = 0, [], None
    done = embed.EmbedIndex.sync(budget_seconds=0)
    assert done == N - embed.EmbedIndex.SYNC_CHUNK, (
        f"resumed pass re-embedded {done} rows; {N - embed.EmbedIndex.SYNC_CHUNK} were pending"
    )
    assert len(stub.texts) == N - embed.EmbedIndex.SYNC_CHUNK, "already-done rows were re-embedded"
    assert set(_recipes(vault_dir).values()) == {new}
    assert _meta(vault_dir, "text_recipe") == new
    assert not embed.text_recipe_changed()


def test_a_foreground_pass_still_refuses_to_transition_the_recipe(
    vault_dir: Path, stub: _StubEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-row recipes do not change the rule that a recall never rebuilds."""
    monkeypatch.setattr(embed, "EMBED_TEXT_VERSION", 1)
    embed.EmbedIndex.sync(budget_seconds=0)
    monkeypatch.setattr(embed, "EMBED_TEXT_VERSION", 2)
    assert embed.EmbedIndex.sync() == 0
    assert set(_recipes(vault_dir).values()) == {"v1:%d:%s" % (embed._embed_text_budget(), embed.EMBED_MODEL)}


def test_a_legacy_index_gains_the_column_in_place(vault_dir: Path, stub: _StubEmbedder) -> None:
    """An index from before the column: ALTER + backfill from meta, no re-embed,
    no epoch bump, every BLOB byte-identical."""
    idx = vault_dir / ".index" / "embeddings.sqlite"
    idx.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(idx)
    conn.execute(
        "CREATE TABLE embeddings (path TEXT PRIMARY KEY, mtime REAL NOT NULL, "
        "vector BLOB NOT NULL)"
    )
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    legacy_recipe = "v1:1000:" + embed.EMBED_MODEL
    conn.executemany(
        "INSERT INTO meta VALUES (?, ?)",
        [("text_recipe", legacy_recipe), ("path_format", embed.PATH_FORMAT), ("vector_epoch", "7")],
    )
    blobs = {f"user/m{i:02d}.md": bytes([i]) * 16 for i in range(N)}
    conn.executemany(
        "INSERT INTO embeddings VALUES (?, ?, ?)", [(p, 1.0, b) for p, b in blobs.items()]
    )
    conn.commit()
    conn.close()

    embed._connect().close()

    conn = sqlite3.connect(idx)
    try:
        rows = conn.execute("SELECT path, vector, recipe FROM embeddings").fetchall()
        assert {p: b for p, b, _ in rows} == blobs, "a vector changed during a pure migration"
        assert {r for _, _, r in rows} == {legacy_recipe}
        assert conn.execute("SELECT value FROM meta WHERE key='vector_epoch'").fetchone()[0] == "7"
    finally:
        conn.close()


def test_upsert_stamps_the_current_recipe(vault_dir: Path, stub: _StubEmbedder) -> None:
    path = vault_dir / "user" / "m00.md"
    embed.EmbedIndex.upsert(path)
    assert _recipes(vault_dir) == {"user/m00.md": embed._text_recipe_id()}
