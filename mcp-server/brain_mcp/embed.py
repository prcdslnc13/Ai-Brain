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


# Rows are keyed on a *vault-relative*, forward-slashed path. Absolute keys tie the
# index to one filesystem location, so moving the vault — D: to C:, a renamed home
# directory, a restore onto a differently-shaped machine — makes every stored path
# miss against _indexable(), and sync() then deletes the whole corpus as "stale" and
# re-embeds it from scratch: ~300s of work to reconstruct vectors that were still
# perfectly valid. Forward slashes so a key is byte-identical whichever OS wrote it.
#
# Note this is *not* what keeps the index machine-local. `.index` is a hidden
# directory that is not `.obsidian`, so Obsidian never enumerates it and Obsidian
# Sync never propagates it — verified 2026-08-24 against a vault shared by four
# machines across two OSes for four months with no thrash and no conflict files.
# Relative keys are about surviving relocation, and they are only a *partial* guard
# for a vault behind a sync tool that does copy dotfiles (Dropbox, OneDrive,
# Syncthing, git): they stop the path thrash, but a live sqlite file replicated
# under two writers still risks corruption. Don't put the vault behind one.
PATH_FORMAT = "relative"


def _index_key(path: Path, root: Path) -> str:
    """DB key for `path`.

    Falls back to the absolute string for a path outside the vault, which sync()
    then clears as stale — the same treatment a deleted file gets, and the only
    honest answer for a row this vault cannot resolve.
    """
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _key_path(key: str, root: Path) -> Path:
    """Absolute path for a DB key.

    Tolerates legacy absolute keys, so an index that has not been migrated yet —
    and every read-only consumer, which never migrates one — still resolves.
    """
    p = Path(key)
    return p if p.is_absolute() else root / p


def _normalize_key(key: str, root: Path) -> str:
    """Re-key a possibly-legacy row the way _indexable() would key it, so a
    read-only consumer can compare against a not-yet-migrated index without
    reporting every file as missing."""
    return _index_key(_key_path(key, root), root)


# sqlite's default busy timeout is 5s, which a full reindex can outlast: a recall's
# foreground sync and a background reindex both write, and on 2026-08-24 that collision
# killed the reindex outright with "database is locked". Waiting is always better than
# failing here — every writer holds the lock only for one chunk's commit.
SQLITE_BUSY_TIMEOUT_S = 30.0


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_index_path(), timeout=SQLITE_BUSY_TIMEOUT_S)
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
    _stamp_recipe_if_empty(conn)
    _migrate_path_format(conn)
    return conn


_SYNC_LOCK = threading.Lock()
# Cache the normalized vector matrix so repeat queries don't re-read every BLOB.
# Signature = (project_filter, row count, max mtime, vector epoch) — cheap sqlite
# queries. The epoch covers writes that change vectors without changing the row set;
# see _bump_vector_epoch().
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
            conn = sqlite3.connect(
                f"file:{idx}?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT_S
            )
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


def _index_is_empty(conn) -> bool:
    try:
        return conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    except sqlite3.DatabaseError:
        return True


def text_recipe_changed(conn=None) -> bool:
    """True when a *populated* index was built by a different embed_text() recipe.

    An empty index is never "changed": there are no vectors to invalidate, so the
    answer is no even though it carries no stamp yet.
    """
    own = conn is None
    if own:
        idx = _index_path()
        if not idx.exists():
            return False
        try:
            conn = sqlite3.connect(
                f"file:{idx}?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT_S
            )
        except sqlite3.DatabaseError:
            return False
    try:
        if _index_is_empty(conn):
            return False
        return stored_text_recipe(conn) != _text_recipe_id()
    finally:
        if own:
            conn.close()


def _stamp_recipe(conn) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('text_recipe', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_text_recipe_id(),),
    )
    conn.commit()


def _stamp_recipe_if_empty(conn) -> None:
    """Stamp an empty index with the current recipe.

    Called from _connect(), so *every* write path is covered — not just sync().
    upsert() and delete() create the tables and populate them without ever going
    through sync(), so a fresh vault whose first operation is `brain save` used to
    end up with a one-row index carrying no stamp. That index is not empty, so
    text_recipe_changed() reports True forever, and every later foreground sync
    takes the "populated + changed + foreground" branch and returns 0 without
    indexing anything. The index then never fills — the same deadlock the
    empty-index stamp inside sync() exists to prevent, reached through the door
    sync() doesn't guard.

    Requires an explicit COUNT of 0: a read that *errors* must never be mistaken
    for an empty index, or one transient failure would stamp the current recipe
    over a populated index built by an older one and silently bless two
    incompatible vector spaces as one.
    """
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()
        if count == 0:
            _stamp_recipe(conn)
    except sqlite3.DatabaseError:
        return


def _bump_vector_epoch(conn) -> None:
    """Advance the counter that tells a cached matrix its vectors are stale.

    `_MATRIX_CACHE` keys on (project, row count, max mtime) — all *row* properties.
    Any write that changes a vector's contents without changing which rows exist or
    when they were modified slips straight past it. A recipe rebuild does exactly
    that (same paths, same mtimes, new vectors), and so does the path-format
    migration. The key was sound before those existed, because a row's vector only
    ever changed when its mtime did.

    Stored in the DB rather than cleared in memory because it has to work
    cross-process: a long-lived MCP server must notice a `brain reindex` that ran in
    another process, and no amount of clearing its own dict will tell it that.

    Caller commits — every call site is already inside a transaction it commits.
    """
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('vector_epoch', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
    )


def _vector_epoch(conn) -> str:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='vector_epoch'").fetchone()
    except sqlite3.DatabaseError:
        # Unreadable meta must not let a stale matrix look fresh: a value nothing
        # else returns forces a cache miss rather than a false hit.
        return "?"
    return row[0] if row else "0"


def _migrate_path_format(conn) -> None:
    """One-time rewrite of absolute row keys to vault-relative ones.

    A pure rename — the vectors themselves stay valid — so the upgrade costs one
    transaction instead of the ~300s a full re-embed would. Runs from _connect(),
    so every writing path migrates; read-only consumers cope via _key_path().

    Rows that cannot be relativized are dropped rather than kept: they name a vault
    location this machine does not have, so nothing can read them and sync() would
    delete them as stale on its next pass regardless.
    """
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='path_format'").fetchone()
    except sqlite3.DatabaseError:
        return
    if row and row[0] == PATH_FORMAT:
        return

    renames: list[tuple[str, str]] = []
    drops: list[tuple[str]] = []
    try:
        root = vault.vault_root()
        for (raw,) in conn.execute("SELECT path FROM embeddings").fetchall():
            p = Path(raw)
            if not p.is_absolute():
                continue
            try:
                renames.append((p.relative_to(root).as_posix(), raw))
            except ValueError:
                drops.append((raw,))
        if renames:
            # OR REPLACE: a legacy absolute row and an already-relative row for the
            # same file collide on the primary key, and the newer one should win.
            conn.executemany(
                "UPDATE OR REPLACE embeddings SET path = ? WHERE path = ?", renames
            )
        if drops:
            conn.executemany("DELETE FROM embeddings WHERE path = ?", drops)
        if renames or drops:
            # A rename moves neither row count nor max mtime, so without this a
            # matrix cached before the migration would go on serving pre-migration
            # paths.
            _bump_vector_epoch(conn)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('path_format', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (PATH_FORMAT,),
        )
        conn.commit()
    except (sqlite3.DatabaseError, OSError):
        return


def _recipe_model(recipe: str | None) -> str | None:
    """The model component of a recipe id (`v{version}:{budget}:{model}`)."""
    if not recipe:
        return None
    parts = recipe.split(":", 2)
    return parts[2] if len(parts) == 3 else None


def _wipe_for_model_change(conn) -> None:
    """Empty the index because the *model* changed.

    The only case that still justifies a wipe. Vectors from two models do not share
    a space and need not even share a dimensionality, so mixing them is not merely
    inaccurate — `np.vstack` on ragged rows raises, taking vector search down for the
    whole rebuild. A version or budget bump keeps the model, so those rows stay
    stackable and comparable and get replaced in place instead (see sync()).
    """
    conn.execute("DELETE FROM embeddings")
    _bump_vector_epoch(conn)
    _stamp_recipe(conn)


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
    # The whole slice-building path is inside the try, not just from_file(): the
    # fields it hands back are parsed YAML, and reading them is exactly as capable
    # of raising as parsing them was. Anything that escapes here escapes sync()
    # too — the batch loop only catches OSError — and search_memories turns that
    # into "embed unavailable", so one unparseable note would take vector search
    # down for the entire vault. Falling back to the raw file keeps the blast
    # radius at the one file, which is what the promise above says.
    try:
        mem = vault.Memory.from_text(Path(path), raw_text)
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
    except Exception:
        return raw_text[:budget]
    return text[:budget] if text else raw_text[:budget]


# Optional bound on checkpoint indexing, OFF by default. Session checkpoints are
# 68.7% of the vault's files (measured 2026-08-24: 616 of 897) and grow by one per
# session while hand-written memories grow slowly, so an unbounded index trends
# toward being nothing but checkpoints — hence the knob.
#
# It defaults to 0 (index everything), but no longer because dropping a vector makes
# a file unreachable — vault._merge_lexical now reserves every 3rd slot for
# lexical-only hits, so an un-vectorized file is never worse than position 3. (The
# old rationale here described the pre-fix ranking, where such a file sorted below
# *every* vector hit and measured 21st of 21.)
#
# The remaining cost is narrower but real: an excluded checkpoint is findable only
# *lexically*, so a query that is semantically related without literally matching
# won't reach it at all. Measured benefit is ~15% fewer indexed files for a one-time
# indexing cost; the price is a permanent loss of semantic reach over old
# checkpoints. Turn it on deliberately, for a vault where index size actually hurts.
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
        out[_index_key(p, root)] = mtime
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
        # A recipe change is invisible to backlog(), which compares mtimes only —
        # the files did not change, the recipe did. Without this clause nothing
        # ever spawns the rebuild: the foreground sync refuses to do it (wiping
        # mid-recall is worse), and on a CLI-first install there is no MCP warmup
        # either, so the index serves superseded vectors until a human notices
        # doctor's INDEX_RECIPE_STALE and runs `brain reindex` by hand. Checked
        # first because it is one cheap sqlite read; backlog_count() stats the
        # whole vault.
        if not text_recipe_changed() and backlog_count() < max(1, min_backlog):
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

    # A foreground recall must not block on a large backlog, and the only way to bound
    # its latency is to bound the document count per pass.
    #
    # Cost is ~400 ms/doc for bge-small on CPU once a document reaches the model's
    # 512-token cap, and *flat* above it — but it scales with token count below that,
    # which is the whole reason embed_text() feeds a bounded slice (see
    # EMBED_TEXT_CHARS_DEFAULT: a 1000-char budget cut a full rebuild 27%, 433s ->
    # 315s). This comment used to assert the opposite, which was true of the raw-file
    # recipe it was written for and false the moment the slice landed in the same
    # file — so if you change the recipe, re-read this paragraph too.
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

            # A reindex is already draining the whole backlog; a foreground pass would
            # only contend with it for the write lock and duplicate its work. The
            # background pass is the one that dies in that race (it holds the longer
            # transaction), which would leave the backlog permanently undrained —
            # precisely the failure INDEX_STALE exists to surface.
            if foreground and reindex_lock_held():
                return 0

            root = vault.vault_root()
            conn = _connect()
            try:
                # Set when the whole corpus must be re-embedded under a new slice
                # recipe. Nothing is deleted for it; every row is simply treated as
                # missing so it gets replaced in place.
                rebuilding = False
                stored = stored_text_recipe(conn)
                if stored != _text_recipe_id():
                    if _index_is_empty(conn):
                        # Nothing to invalidate. Stamp now — an index that is never
                        # stamped looks permanently "changed", which would make every
                        # later foreground sync bail out and never index anything.
                        _stamp_recipe(conn)
                    elif foreground:
                        # Wiping mid-recall would leave the rest of the session querying
                        # a near-empty index — worse than briefly serving old-recipe
                        # vectors, which are at least self-consistent. Let an unbounded
                        # pass (reindex, MCP warmup, SessionStart kick) transition it.
                        return 0
                    elif _recipe_model(stored) != EMBED_MODEL:
                        print("brain embed: embedding model changed, rebuilding index",
                              file=sys.stderr)
                        _wipe_for_model_change(conn)
                    else:
                        # Same model, different slice recipe. Re-embed every row, but
                        # *in place*: the old wipe-then-refill committed a DELETE and
                        # then spent ~300s refilling, during which every other process
                        # read a near-empty index and silently degraded to ripgrep —
                        # and since the SessionStart kick is what performs the rebuild,
                        # the session that triggered it was precisely the one querying
                        # the gutted index. Replacing row by row keeps the index whole
                        # and queryable throughout; the vectors it serves meanwhile are
                        # from the same model, so they remain comparable.
                        print("brain embed: embedding recipe changed, re-embedding in place",
                              file=sys.stderr)
                        rebuilding = True

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
                    _bump_vector_epoch(conn)
                    conn.commit()

                pending: list[tuple[str, float]] = []
                for key, mtime in current.items():
                    # A rebuild ignores what is already stored — every row's vector
                    # is from the superseded recipe, however fresh its mtime is.
                    prior = None if rebuilding else existing.get(key)
                    if prior is None or mtime > prior + 1e-6:
                        if foreground and vault.is_session_path(_key_path(key, root)):
                            continue
                        pending.append((key, mtime))

                # Hand-written memories before checkpoints, newest first within each
                # group. Only bites on unbounded passes (a foreground one has already
                # dropped every checkpoint above), where it still matters: an
                # interrupted reindex should have finished the memories first.
                pending.sort(
                    key=lambda pm: (vault.is_session_path(_key_path(pm[0], root)), -pm[1])
                )

                done = 0
                truncated = False
                for i in range(0, len(pending), cls.SYNC_CHUNK):
                    # `and done` guarantees forward progress even on a zero budget.
                    if deadline is not None and done and time.monotonic() >= deadline:
                        truncated = True
                        break
                    batch: list[tuple[str, float, str]] = []
                    for key, mtime in pending[i:i + cls.SYNC_CHUNK]:
                        try:
                            batch.append((key, mtime, embed_text(_key_path(key, root))))
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
                    _bump_vector_epoch(conn)
                    # Commit per chunk, not once at the end: a time-boxed pass must
                    # keep its progress or successive recalls redo the same work.
                    conn.commit()
                    done += len(batch)

                if rebuilding and not truncated:
                    # Stamp only once every row carries the new recipe. Stamping up
                    # front (as the wipe-first version did) would make an interrupted
                    # rebuild look complete, stranding the un-re-embedded remainder in
                    # the old recipe forever — the mtime check cannot see the
                    # difference, so nothing would ever come back for them.
                    _stamp_recipe(conn)

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
            conn = sqlite3.connect(
                f"file:{idx}?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT_S
            )
            try:
                for path, mtime in conn.execute("SELECT path, mtime FROM embeddings"):
                    # Normalize as we read: this connection is read-only and never
                    # migrates, so without it a pre-migration index would report
                    # every file missing and kick a pointless full reindex.
                    existing[_normalize_key(path, root)] = mtime
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
        root = vault.vault_root()
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO embeddings(path, mtime, vector) VALUES (?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, vector=excluded.vector",
                (_index_key(Path(path), root), mtime, _vec_to_blob(vec)),
            )
            _bump_vector_epoch(conn)
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def delete(cls, path: Path) -> None:
        root = vault.vault_root()
        conn = _connect()
        try:
            conn.execute(
                "DELETE FROM embeddings WHERE path = ?",
                (_index_key(Path(path), root),),
            )
            _bump_vector_epoch(conn)
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
            # The epoch is what makes this key able to see a vector change that left
            # the row set alone — a recipe rebuild, or the path-format migration.
            key = (project_filter, row[0], row[1], _vector_epoch(conn))
            if _MATRIX_CACHE["key"] == key and _MATRIX_CACHE["mat"] is not None:
                return _MATRIX_CACHE["paths"], _MATRIX_CACHE["mat"]

            # Relative inside the index, absolute at the API boundary: query()'s
            # callers resolve and stat these, so a vault-relative string would
            # silently resolve against the process cwd.
            root = vault.vault_root()
            paths: list[str] = []
            vectors: list = []
            # Not `key` — that name holds the cache signature above, and shadowing it
            # here silently stored the last row's path as the cache key, so every
            # lookup compared a str against a tuple and missed.
            for row_key, blob in conn.execute("SELECT path, vector FROM embeddings"):
                p = _key_path(row_key, root)
                if project_filter and not vault.path_in_project(p, project_filter):
                    continue
                paths.append(str(p))
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
