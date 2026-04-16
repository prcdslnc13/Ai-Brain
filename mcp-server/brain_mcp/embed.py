"""Sqlite-backed vector index for the Brain vault.

The index lives at `$BRAIN_VAULT/Brain/.index/embeddings.sqlite`. Embedding model is
fastembed's `BAAI/bge-small-en-v1.5` (384-dim, ONNX, CPU-only). Failures are non-fatal:
callers fall back to ripgrep substring search.
"""

from __future__ import annotations

import os
import sqlite3
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from . import vault

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384

_EXCLUDE_PARTS = {"archive", "_setup", ".pending-saves", ".index"}


class EmbedUnavailable(RuntimeError):
    """fastembed/numpy missing or model failed to load."""


def _vec_to_blob(vec) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _blob_to_vec(blob: bytes):
    import numpy as np
    return np.frombuffer(blob, dtype="<f4")


@dataclass
class _Embedder:
    """Lazily-loaded fastembed wrapper."""
    _impl: object | None = None

    def get(self):
        if self._impl is not None:
            return self._impl
        try:
            from fastembed import TextEmbedding  # type: ignore
        except ImportError as e:
            raise EmbedUnavailable(f"fastembed not installed: {e}") from e
        try:
            self._impl = TextEmbedding(model_name=EMBED_MODEL)
        except Exception as e:
            raise EmbedUnavailable(f"failed to load embedding model: {e}") from e
        return self._impl

    def embed_one(self, text: str):
        impl = self.get()
        for vec in impl.embed([text]):
            return vec
        raise EmbedUnavailable("embedder returned no vectors")

    def embed_many(self, texts: list[str]):
        impl = self.get()
        return list(impl.embed(texts))


_EMBEDDER = _Embedder()


def _index_path() -> Path:
    root = vault.vault_root()
    idx_dir = root / ".index"
    idx_dir.mkdir(parents=True, exist_ok=True)
    return idx_dir / "embeddings.sqlite"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_index_path())
    conn.execute(
        "CREATE TABLE IF NOT EXISTS embeddings ("
        "  path TEXT PRIMARY KEY,"
        "  mtime REAL NOT NULL,"
        "  vector BLOB NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('model', ?), ('dim', ?)",
        (EMBED_MODEL, str(EMBED_DIM)),
    )
    return conn


def _iter_vault_md(root: Path):
    for p in root.rglob("*.md"):
        rel_parts = p.relative_to(root).parts
        if any(part in _EXCLUDE_PARTS for part in rel_parts):
            continue
        yield p


class EmbedIndex:
    """Vector index over the vault. Self-healing: rebuilds missing rows on sync()."""

    @classmethod
    def warm(cls) -> None:
        """Pre-load the model (used by setup scripts to avoid first-call stalls)."""
        try:
            _EMBEDDER.embed_one("warmup")
        except EmbedUnavailable as e:
            print(f"brain embed warm-up skipped: {e}", file=sys.stderr)

    @classmethod
    def sync(cls) -> int:
        """Walk the vault, upsert stale/missing rows, drop rows for deleted files.

        Returns the number of rows upserted. Raises EmbedUnavailable if the embedder
        cannot load.
        """
        root = vault.vault_root()
        conn = _connect()
        try:
            existing: dict[str, float] = {}
            for path, mtime in conn.execute("SELECT path, mtime FROM embeddings"):
                existing[path] = mtime

            current: dict[str, float] = {}
            for p in _iter_vault_md(root):
                try:
                    current[str(p)] = p.stat().st_mtime
                except OSError:
                    continue

            stale = [p for p in existing if p not in current]
            if stale:
                conn.executemany("DELETE FROM embeddings WHERE path = ?", ((p,) for p in stale))

            to_upsert: list[tuple[str, float, str]] = []
            for path, mtime in current.items():
                prior = existing.get(path)
                if prior is None or mtime > prior + 1e-6:
                    try:
                        text = Path(path).read_text(encoding="utf-8")
                    except OSError:
                        continue
                    to_upsert.append((path, mtime, text))

            if to_upsert:
                texts = [t for (_, _, t) in to_upsert]
                vectors = _EMBEDDER.embed_many(texts)
                rows = [
                    (path, mtime, _vec_to_blob(vec))
                    for (path, mtime, _), vec in zip(to_upsert, vectors)
                ]
                conn.executemany(
                    "INSERT INTO embeddings(path, mtime, vector) VALUES (?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, vector=excluded.vector",
                    rows,
                )

            conn.commit()
            return len(to_upsert)
        finally:
            conn.close()

    @classmethod
    def upsert(cls, path: Path) -> None:
        """Single-file upsert. Silently no-ops if the embedder is unavailable."""
        try:
            text = Path(path).read_text(encoding="utf-8")
            mtime = Path(path).stat().st_mtime
            vec = _EMBEDDER.embed_one(text)
        except (OSError, EmbedUnavailable):
            return
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO embeddings(path, mtime, vector) VALUES (?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, vector=excluded.vector",
                (str(path), mtime, _vec_to_blob(vec)),
            )
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def delete(cls, path: Path) -> None:
        try:
            conn = _connect()
        except Exception:
            return
        try:
            conn.execute("DELETE FROM embeddings WHERE path = ?", (str(path),))
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def query(
        cls,
        text: str,
        top_k: int = 10,
        type_filter: str | None = None,
        project_filter: str | None = None,
    ) -> list[tuple[str, float]]:
        """Return up to top_k (path, score) tuples ranked by cosine similarity."""
        import numpy as np

        q_vec = _EMBEDDER.embed_one(text)
        q = np.asarray(q_vec, dtype="float32")
        q_norm = float(np.linalg.norm(q))
        if q_norm == 0.0:
            return []
        q /= q_norm

        conn = _connect()
        try:
            paths: list[str] = []
            vectors: list = []
            for path, blob in conn.execute("SELECT path, vector FROM embeddings"):
                if project_filter and f"/projects/{project_filter}/" not in path:
                    continue
                paths.append(path)
                vectors.append(_blob_to_vec(blob))
        finally:
            conn.close()

        if not paths:
            return []

        mat = np.vstack(vectors)
        norms = np.linalg.norm(mat, axis=1)
        norms[norms == 0] = 1.0
        mat = mat / norms[:, None]
        scores = mat @ q

        order = np.argsort(-scores)
        results: list[tuple[str, float]] = []
        for idx in order:
            path = paths[idx]
            if type_filter:
                t = vault._read_frontmatter_type(Path(path))
                if t != type_filter:
                    continue
            results.append((path, float(scores[idx])))
            if len(results) >= top_k:
                break
        return results
