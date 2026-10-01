"""A note whose mtime moves *backwards* is re-embedded, and counts as backlog until it is.

The index used to re-embed a file only when its mtime was newer than the recorded
one. Two ordinary events move it the other way: Obsidian Sync landing an edit from
a machine whose clock runs behind this one, and restoring a note from
`archive/versions/`. Either left the old vector in place for good: recall ranked
the note by text it no longer held, and `backlog()` (so INDEX_STALE) agreed nothing
was wrong. sync() and backlog() now share `_mtime_changed`, which asks "differs".
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from brain_mcp import embed
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


def _set_mtime(path: Path, when: float) -> None:
    os.utime(path, (when, when))


@pytest.mark.parametrize("delta", [-3600.0, +3600.0])
def test_any_mtime_change_reembeds(vault_dir: Path, stub_embedder: _StubEmbedder,
                                    delta: float) -> None:
    note = memory(vault_dir / "user" / "editor.md", "editor", "user", "I use vim.")
    embedded_at = note.stat().st_mtime
    assert embed.EmbedIndex.sync(budget_seconds=0) == 1

    # An edit synced from a machine whose clock differs by an hour either way.
    memory(note, "editor", "user", "I use helix now, not vim.")
    _set_mtime(note, embedded_at + delta)
    stub_embedder.texts.clear()

    assert embed.EmbedIndex.backlog() == 1, "the changed note is backlog until embedded"
    assert embed.EmbedIndex.sync(budget_seconds=0) == 1
    assert any("helix" in t for t in stub_embedder.texts)
    assert embed.EmbedIndex.backlog() == 0


def test_unchanged_mtime_is_not_reembedded(vault_dir: Path,
                                           stub_embedder: _StubEmbedder) -> None:
    memory(vault_dir / "user" / "editor.md", "editor", "user", "I use vim.")
    embed.EmbedIndex.sync(budget_seconds=0)
    stub_embedder.texts.clear()

    assert embed.EmbedIndex.backlog() == 0
    assert embed.EmbedIndex.sync(budget_seconds=0) == 0
    assert stub_embedder.texts == []
