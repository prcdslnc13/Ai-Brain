"""Sqlite-backed vector index for the Brain vault.

The index lives at `$BRAIN_VAULT/Brain/.index/embeddings.sqlite`. Embedding model is
fastembed's `BAAI/bge-small-en-v1.5` (384-dim, ONNX, CPU-only). Failures are non-fatal:
callers fall back to ripgrep substring search.

Model load is **offline-first**. fastembed has no `local_files_only` knob (0.8.0), and
`TextEmbedding(...)` makes a HuggingFace metadata round-trip on every construction even
when the model is fully cached. That call has no timeout, runs while `_Embedder._lock`
is held, and blocks the synchronous `brain_recall` handler — so a slow or unreachable
hub turns recall into an unbounded hang (observed: a ~1h lock-up on 2026-06-03). We fix
this by (a) pinning a stable machine-local cache dir so the model is found regardless of
how the harness rewrites TMP, (b) setting bounded HF network timeouts and `HF_HUB_OFFLINE`
*before* huggingface_hub freezes them into module constants at import, and (c) only going
online when the model is genuinely absent (then bounded by the timeouts, to self-heal).
"""

from __future__ import annotations

import os
import sqlite3
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import vault

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384


def _embed_cache_dir() -> Path:
    """Stable, machine-local directory for the ONNX model weights.

    Deliberately NOT the system temp dir (fastembed's default): the harness and
    OS temp-cleaners rewrite/purge TMP, which moves the cache out from under the
    server and forces re-downloads — the trigger behind the recall hang. Also
    deliberately NOT inside the synced vault, so the 64MB model never replicates
    over Obsidian Sync. Override with FASTEMBED_CACHE_PATH or BRAIN_EMBED_CACHE.
    """
    explicit = os.environ.get("FASTEMBED_CACHE_PATH") or os.environ.get("BRAIN_EMBED_CACHE")
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "Ai-Brain" / "fastembed"
    return Path.home() / ".cache" / "ai-brain" / "fastembed"


def _model_is_cached(cache_dir: Path) -> bool:
    """True when an ONNX model file already exists under cache_dir."""
    if not cache_dir.exists():
        return False
    try:
        for _ in cache_dir.rglob("*.onnx"):
            return True
    except OSError:
        pass
    return False


_CACHE_DIR = _embed_cache_dir()

# Configure HuggingFace behaviour BEFORE huggingface_hub is imported (its import
# is lazy, inside _Embedder.get()). huggingface_hub reads these into module-level
# constants once at import, so toggling them later — e.g. inside get(), after
# `from fastembed import TextEmbedding` has already pulled in the hub — is a no-op.
# Bound every network op so even a cache-miss download can't hang the session.
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "10")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "30")
# Offline-first: when the model is already on disk, forbid HF network round-trips
# entirely. On a genuine cache miss we stay online (bounded above) to self-heal.
if os.environ.get("BRAIN_EMBED_OFFLINE", "1") != "0" and _model_is_cached(_CACHE_DIR):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")


class EmbedUnavailable(RuntimeError):
    """fastembed/numpy missing or model failed to load."""


def _vec_to_blob(vec) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _blob_to_vec(blob: bytes):
    import numpy as np
    return np.frombuffer(blob, dtype="<f4")


@dataclass
class _Embedder:
    """Lazily-loaded fastembed wrapper. Thread-safe — concurrent get() calls
    serialize on the lock, so a background warmup and a foreground recall can
    race without double-loading the model."""
    _impl: object | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self):
        if self._impl is not None:
            return self._impl
        with self._lock:
            if self._impl is not None:
                return self._impl
            try:
                from fastembed import TextEmbedding  # type: ignore
            except ImportError as e:
                raise EmbedUnavailable(f"fastembed not installed: {e}") from e
            try:
                self._impl = TextEmbedding(
                    model_name=EMBED_MODEL, cache_dir=str(_CACHE_DIR)
                )
            except (OSError, RuntimeError) as e:
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


_SYNC_LOCK = threading.Lock()
# Cache the normalized vector matrix so repeat queries don't re-read every BLOB.
# Signature = (project_filter, row count, max mtime) — cheap sqlite query.
_MATRIX_CACHE: dict = {"key": None, "paths": None, "mat": None}


# What actually gets embedded. Embedding cost scales with token count up to the
# model's 512-token cap (~1500 chars) and is flat above it, so anything past the cap
# is paid for and then discarded by the tokenizer — the tail of a long memory never
# reached the model to begin with. Feeding a bounded, higher-signal slice instead of
# the raw file is therefore cheaper *and* strictly more informative per token:
#
#   - the raw file leads with YAML (`type:`, `machine:`, `project:`) that is pure
#     noise in the vector space, and it is what the first tokens get spent on;
#   - `name` and `description` are the most query-like text a memory has, so they go
#     first, unwrapped;
#   - the body lead carries the substance. The rest was never embedded anyway unless
#     the whole file fit under the cap.
#
# Budget picked by measurement, not taste. Full rebuild of the 903-file vault, and
# tail-query retrieval (151 queries drawn from text past every cap):
#
#     full raw file   433s        r@1 40.4%   MRR 0.553
#     slice 1500      415s  -4%   r@1 41.1%   MRR 0.564
#     slice 1200      364s -16%   r@1 40.4%   MRR 0.552
#     slice 1000      315s -27%   r@1 40.4%   MRR 0.550   <- default
#     slice  800      258s -40%   r@1 35.8%   MRR 0.516
#
# Title-query retrieval was ~98% r@1 and 100% r@3 for every budget including 400, so
# the tail is the only axis that discriminates. The quality cliff is between 1000 and
# 800; 1000 is the fastest budget that is still indistinguishable from embedding the
# whole file. Going lower is a real trade, not a free win.
#
# BRAIN_EMBED_CHARS tunes the budget; 0 restores embedding the full raw file.
EMBED_TEXT_CHARS_DEFAULT = 1000


def _embed_text_budget() -> int:
    raw = os.environ.get("BRAIN_EMBED_CHARS", "")
    if not raw:
        return EMBED_TEXT_CHARS_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return EMBED_TEXT_CHARS_DEFAULT


# Bump when embed_text() changes what it feeds the model. Vectors built by a
# different recipe are not comparable with new ones, and the mtime-based staleness
# check cannot notice — the files did not change, the *recipe* did — so the index
# would silently mix two vector spaces forever. The budget is part of the identity
# for the same reason: re-tuning BRAIN_EMBED_CHARS invalidates every vector.
EMBED_TEXT_VERSION = 2


def _text_recipe_id() -> str:
    return f"v{EMBED_TEXT_VERSION}:{_embed_text_budget()}:{EMBED_MODEL}"


def stored_text_recipe(conn=None) -> str | None:
    own = conn is None
    if own:
        try:
            idx = _index_path()
            if not idx.exists():
                return None
            conn = sqlite3.connect(f"file:{idx}?mode=ro", uri=True)
        except sqlite3.DatabaseError:
            return None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='text_recipe'").fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        if own:
            conn.close()
    return row[0] if row else None


def text_recipe_changed(conn=None) -> bool:
    """True when the index was built by a different embed_text() recipe.

    An empty index counts as unchanged — there is nothing to invalidate, and the
    first sync stamps the current recipe.
    """
    if conn is not None:
        try:
            if conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0:
                return False
        except sqlite3.DatabaseError:
            return False
    return stored_text_recipe(conn) != _text_recipe_id()


def _rebuild_for_recipe(conn) -> None:
    conn.execute("DELETE FROM embeddings")
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('text_recipe', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_text_recipe_id(),),
    )
    conn.commit()


def embed_text(path: Path) -> str:
    """The text to embed for `path`: title + description + body lead, budget-capped.

    Falls back to the raw file whenever the frontmatter can't be parsed, so a
    malformed note still gets indexed rather than silently embedding an empty
    string (doctor's MALFORMED_FRONTMATTER exists precisely because those happen).
    """
    budget = _embed_text_budget()
    raw_text = Path(path).read_text(encoding="utf-8")
    if budget <= 0:
        return raw_text
    try:
        mem = vault.Memory.from_file(Path(path))
    except Exception:
        return raw_text[:budget]
    parts = [mem.name or Path(path).stem]
    body = (mem.body or "").strip()
    desc = (mem.description or "").strip()
    # write_memory derives description from the body's first line, so for most
    # memories it is already a prefix of the body — repeating it would burn budget
    # on a duplicate rather than buying any signal.
    if desc and not body.startswith(desc[:80]):
        parts.append(desc)
    if body:
        parts.append("")
        parts.append(body)
    text = "\n".join(parts).strip()
    return text[:budget] if text else raw_text[:budget]


# Optional bound on checkpoint indexing, OFF by default. Session checkpoints are
# 68.7% of the vault's files (measured 2026-08-24: 616 of 897) and grow by one per
# session while hand-written memories grow slowly, so an unbounded index trends
# toward being nothing but checkpoints — hence the knob.
#
# It defaults to 0 (index everything) because dropping a file's vector currently
# makes it near-unfindable, not merely un-ranked: search_memories appends *all*
# ripgrep hits after *all* 20 vector hits, so a file with no vector lands at the
# bottom no matter how well it matches. Measured 2026-08-24 on a synthetic vault:
# a query whose literal text appeared in exactly one aged-out checkpoint ranked
# that checkpoint 21st of 21, invisible at any sane top_k. Until ripgrep-only hits
# are ranked fairly, set this only if you accept that trade.
SESSION_INDEX_DAYS_DEFAULT = 0


def _session_index_cutoff() -> float | None:
    raw = os.environ.get("BRAIN_INDEX_SESSION_DAYS", "")
    if not raw:
        days = SESSION_INDEX_DAYS_DEFAULT
    else:
        try:
            days = float(raw)
        except ValueError:
            days = SESSION_INDEX_DAYS_DEFAULT
    if days <= 0:
        return None
    return time.time() - days * 86400.0


def _indexable(root: Path) -> dict[str, float]:
    """Path -> mtime for every file that *should* carry a vector.

    Single source of truth for sync() and backlog(): if these two disagreed,
    backlog() would report work sync() refuses to do and INDEX_STALE would warn
    forever.
    """
    cutoff = _session_index_cutoff()
    out: dict[str, float] = {}
    for p in vault.iter_indexable_md(root):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if cutoff is not None and mtime < cutoff and vault.is_session_path(p):
            continue
        out[str(p)] = mtime
    return out


# A reindex outlives the hook that starts it, so the guard against concurrent
# passes has to be cross-process (a threading.Lock only covers _SYNC_LOCK's
# process). Treat a lock older than this as abandoned by a killed process.
REINDEX_LOCK_STALE_S = 1800


def _reindex_lock_path() -> Path:
    return vault.vault_root() / ".index" / "reindex.lock"


def reindex_lock_held() -> bool:
    try:
        lock = _reindex_lock_path()
        if not lock.exists():
            return False
        if time.time() - lock.stat().st_mtime > REINDEX_LOCK_STALE_S:
            return False
    except OSError:
        return False
    return True


def acquire_reindex_lock() -> bool:
    """Best-effort cross-process lock. O_EXCL create, with stale-lock takeover."""
    try:
        lock = _reindex_lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        if lock.exists() and time.time() - lock.stat().st_mtime > REINDEX_LOCK_STALE_S:
            lock.unlink(missing_ok=True)
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        # Can't lock (read-only vault, permissions) — don't block the reindex.
        return True
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)
    return True


def release_reindex_lock() -> None:
    try:
        _reindex_lock_path().unlink(missing_ok=True)
    except OSError:
        pass


def spawn_background_reindex(min_backlog: int = 1) -> bool:
    """Kick `brain reindex` as a detached process; return True if one was started.

    Deliberately a *process*, not a thread: the callers are hooks and the CLI,
    both of which exit in seconds, and a daemon thread dies with them — the whole
    point is that the catch-up pass outlives its launcher. Never raises; a failure
    to reindex in the background must not take a session start down with it.
    """
    if os.environ.get("BRAIN_EMBED", "1") == "0":
        return False
    if os.environ.get("BRAIN_AUTO_REINDEX", "1") == "0":
        return False
    try:
        if reindex_lock_held():
            return False
        if backlog_count() < max(1, min_backlog):
            return False
        import subprocess

        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "cwd": str(Path(sys.executable).parent),
            "env": os.environ.copy(),
        }
        if os.name == "nt":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — no console window,
            # survives the parent.
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(
            [sys.executable, "-m", "brain_mcp.cli", "reindex"], **kwargs
        )
        return True
    except Exception:
        return False


def backlog_count() -> int:
    """Module-level alias for EmbedIndex.backlog() — usable before the class body
    is referenced, and the natural name for hook/doctor call sites."""
    return EmbedIndex.backlog()


class EmbedIndex:
    """Vector index over the vault. Self-healing: rebuilds missing rows on sync()."""

    @classmethod
    def warm(cls) -> None:
        """Pre-load the model (used by setup scripts to avoid first-call stalls)."""
        try:
            _EMBEDDER.embed_one("warmup")
        except EmbedUnavailable as e:
            print(f"brain embed warm-up skipped: {e}", file=sys.stderr)

    # A foreground recall must not block on a large backlog. Embedding cost is
    # ~fixed per document (measured 2026-08-24: ~400 ms/doc for bge-small on CPU,
    # independent of body length — fastembed pads every input to the model's
    # 512-token window, so truncating bodies buys nothing), which means the only
    # way to bound recall latency is to bound the document count per pass.
    # Small: the deadline is only checked between chunks, so chunk size is the
    # overshoot granularity. Batching buys almost nothing here (measured: 464
    # ms/doc at batch=1 vs 419 at batch=32), so keeping it low is nearly free.
    SYNC_CHUNK = 4
    SYNC_BUDGET_DEFAULT = 5.0

    @classmethod
    def _budget_seconds(cls) -> float:
        raw = os.environ.get("BRAIN_SYNC_MAX_SECONDS", "")
        if not raw:
            return cls.SYNC_BUDGET_DEFAULT
        try:
            return max(0.0, float(raw))
        except ValueError:
            return cls.SYNC_BUDGET_DEFAULT

    @classmethod
    def sync(cls, budget_seconds: float | None = None) -> int:
        """Walk the vault, upsert stale/missing rows, drop rows for deleted files.

        Time-boxed by default: embeds newest-first until `budget_seconds` is spent
        (`BRAIN_SYNC_MAX_SECONDS`, default 5s; 0 or `budget_seconds=0` means
        unlimited, which is what `brain reindex` and the background warmup use).
        Always makes at least one chunk of progress, and commits per chunk, so a
        truncated pass keeps its work and the next call resumes where it stopped
        rather than starting over.

        A *time-boxed* pass also skips session checkpoints outright. Default recall
        filters checkpoints out of its results, so a foreground recall spending its
        slice on one is pure waste — checkpoints get their vectors from the
        unbounded passes instead (`brain reindex`, the MCP warmup, the SessionStart
        background kick), where nobody is waiting on the result.

        Returns the number of rows upserted. Raises EmbedUnavailable if the embedder
        cannot load. Serialized by a process-wide lock so the background startup
        warmup and a foreground recall don't both embed the same stale files.
        """
        with _SYNC_LOCK:
            if budget_seconds is None:
                budget_seconds = cls._budget_seconds()
            deadline = None if budget_seconds <= 0 else time.monotonic() + budget_seconds
            foreground = deadline is not None

            root = vault.vault_root()
            conn = _connect()
            try:
                if text_recipe_changed(conn):
                    # Wiping mid-recall would leave the rest of the session querying a
                    # near-empty index — worse than briefly serving old-recipe vectors,
                    # which are at least self-consistent. Let an unbounded pass (reindex,
                    # MCP warmup, the SessionStart kick) do the transition instead.
                    if foreground:
                        return 0
                    print("brain embed: embedding recipe changed, rebuilding index",
                          file=sys.stderr)
                    _rebuild_for_recipe(conn)

                existing: dict[str, float] = {}
                for path, mtime in conn.execute("SELECT path, mtime FROM embeddings"):
                    existing[path] = mtime

                # Aged-out checkpoints drop out of `current` and are therefore
                # deleted below, alongside genuinely removed files — which is what
                # keeps the index bounded as sessions accumulate.
                current = _indexable(root)

                stale = [p for p in existing if p not in current]
                if stale:
                    conn.executemany("DELETE FROM embeddings WHERE path = ?", ((p,) for p in stale))
                    conn.commit()

                pending: list[tuple[str, float]] = []
                for path, mtime in current.items():
                    prior = existing.get(path)
                    if prior is None or mtime > prior + 1e-6:
                        if foreground and vault.is_session_path(Path(path)):
                            continue
                        pending.append((path, mtime))

                # Hand-written memories before checkpoints, newest first within each
                # group. Only bites on unbounded passes (a foreground one has already
                # dropped every checkpoint above), where it still matters: an
                # interrupted reindex should have finished the memories first.
                pending.sort(key=lambda pm: (vault.is_session_path(Path(pm[0])), -pm[1]))

                done = 0
                for i in range(0, len(pending), cls.SYNC_CHUNK):
                    # `and done` guarantees forward progress even on a zero budget.
                    if deadline is not None and done and time.monotonic() >= deadline:
                        break
                    batch: list[tuple[str, float, str]] = []
                    for path, mtime in pending[i:i + cls.SYNC_CHUNK]:
                        try:
                            batch.append((path, mtime, embed_text(Path(path))))
                        except OSError:
                            continue
                    if not batch:
                        continue
                    vectors = _EMBEDDER.embed_many([t for (_, _, t) in batch])
                    conn.executemany(
                        "INSERT INTO embeddings(path, mtime, vector) VALUES (?, ?, ?) "
                        "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, vector=excluded.vector",
                        [(pa, mt, _vec_to_blob(v)) for (pa, mt, _), v in zip(batch, vectors)],
                    )
                    # Commit per chunk, not once at the end: a time-boxed pass must
                    # keep its progress or successive recalls redo the same work.
                    conn.commit()
                    done += len(batch)

                return done
            finally:
                conn.close()

    @classmethod
    def backlog(cls) -> int:
        """Count files whose embedding is missing or stale.

        Stat-only — no model load, no embedding — so doctor and the session-start
        hook can call it to decide whether a reindex is worth kicking off.
        """
        try:
            root = vault.vault_root()
        except Exception:
            return 0
        idx = root / ".index" / "embeddings.sqlite"
        if not idx.exists():
            return len(_indexable(root))
        existing: dict[str, float] = {}
        try:
            conn = sqlite3.connect(f"file:{idx}?mode=ro", uri=True)
            try:
                for path, mtime in conn.execute("SELECT path, mtime FROM embeddings"):
                    existing[path] = mtime
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            return 0
        n = 0
        for path, mtime in _indexable(root).items():
            prior = existing.get(path)
            if prior is None or mtime > prior + 1e-6:
                n += 1
        return n

    @classmethod
    def upsert(cls, path: Path) -> None:
        """Single-file upsert. Silently no-ops if the embedder is unavailable."""
        try:
            text = embed_text(Path(path))
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
        conn = _connect()
        try:
            conn.execute("DELETE FROM embeddings WHERE path = ?", (str(path),))
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def _normalized_matrix(cls, project_filter: str | None):
        """Load, filter, and cache the normalized vector matrix."""
        import numpy as np

        conn = _connect()
        try:
            row = conn.execute("SELECT COUNT(*), COALESCE(MAX(mtime), 0) FROM embeddings").fetchone()
            key = (project_filter, row[0], row[1])
            if _MATRIX_CACHE["key"] == key and _MATRIX_CACHE["mat"] is not None:
                return _MATRIX_CACHE["paths"], _MATRIX_CACHE["mat"]

            paths: list[str] = []
            vectors: list = []
            for path, blob in conn.execute("SELECT path, vector FROM embeddings"):
                if project_filter and not vault.path_in_project(Path(path), project_filter):
                    continue
                paths.append(path)
                vectors.append(_blob_to_vec(blob))
        finally:
            conn.close()

        if not paths:
            _MATRIX_CACHE.update(key=key, paths=paths, mat=None)
            return paths, None

        mat = np.vstack(vectors)
        norms = np.linalg.norm(mat, axis=1)
        norms[norms == 0] = 1.0
        mat = mat / norms[:, None]
        _MATRIX_CACHE.update(key=key, paths=paths, mat=mat)
        return paths, mat

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

        paths, mat = cls._normalized_matrix(project_filter)
        if mat is None:
            return []

        scores = mat @ q
        order = np.argsort(-scores)
        results: list[tuple[str, float]] = []
        for idx in order:
            path = paths[idx]
            if type_filter:
                t = vault.read_frontmatter_type(Path(path))
                if t != type_filter:
                    continue
            results.append((path, float(scores[idx])))
            if len(results) >= top_k:
                break
        return results
