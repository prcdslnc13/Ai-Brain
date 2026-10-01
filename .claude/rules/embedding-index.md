---
paths:
  - "mcp-server/brain_mcp/embed.py"
  - "mcp-server/brain_mcp/vault.py"
  - "mcp-server/brain_mcp/doctor.py"
  - "mcp-server/tests/test_embed_*.py"
  - "mcp-server/tests/test_*index*.py"
  - "mcp-server/tests/test_reindex_lock.py"
  - "mcp-server/tests/test_recipe_resume.py"
  - "mcp-server/tests/test_lexical_search_argv.py"
---

# Rules: the embedding index and recall search

Each rule has a case history in `docs/GOTCHAS.md` under the same lead phrase: what
happened, what was measured, and why the fix has the shape it has. Read that entry before
changing anything a rule guards.

- **Recall's index sync is time-boxed; never make it unbounded again.** `EmbedIndex.sync()`
  embeds newest-first in `SYNC_CHUNK` batches, committing per chunk, until
  `BRAIN_SYNC_MAX_SECONDS` (5s); only `brain reindex` and the MCP warmup pass `budget_seconds=0`.
  A foreground sync returns at once while the reindex lock is held, and takes `_SYNC_LOCK` without
  waiting (the MCP warmup holds it for minutes); writers use a 30s sqlite busy
  timeout, readers must not (doctor waits `index_busy_timeout()`, 2s, inside a 15s hook). A locked
  index is `INDEX_BUSY`, never `INDEX_CORRUPT`. Foreground syncs skip session checkpoints;
  `_indexable()` is the one predicate for what gets a vector. Embedding cost flattens past ~1500
  chars, so shortening what reaches the model is the only lever.
- **What is embedded is a slice, and the recipe is versioned.** `embed_text()` feeds name +
  description + body lead capped at `BRAIN_EMBED_CHARS` (1000, chosen by measurement). Bump
  `EMBED_TEXT_VERSION` whenever `embed_text()` changes what reaches the model. A recipe change is
  rebuilt in place by an unbounded pass and stamped only after the last chunk; foreground syncs
  refuse to transition it; an empty index is still stamped (by `_connect()`);
  `spawn_background_reindex()` gates on `text_recipe_changed()` as well as the backlog.
- **Index rows are keyed on a vault-relative, forward-slashed path; every API surface stays
  absolute.** `_index_key()` / `_key_path()` / `_normalize_key()` alone know the format;
  `EmbedIndex.query()` returns absolute paths; upgrades are a rename (`_migrate_path_format()`),
  never a re-embed. This is not what keeps the index machine-local — Obsidian Sync skipping hidden
  directories is. Keep the vault on Obsidian Sync.
- **`_MATRIX_CACHE` is keyed on `vector_epoch`, not row shape.** Every vector write bumps
  `_bump_vector_epoch()` in `meta` so other processes notice. Never name the row loop variable
  `key` in `_normalized_matrix` — it shadows the signature.
- **`embed_text()` reads each file once**, parsing via `Memory.from_text(path, raw)`;
  `from_file()` is a wrapper over it. Keep the split.
- **Lexical-only hits get a reserved slot every 3rd position** (`LEXICAL_SLOT_EVERY`, first
  `LEXICAL_MERGE_CAP` only); don't append them after the vector hits. `EXCLUDE_FILES` keeps
  `activity.md` and `_index.md` out of both index and results; `_ripgrep_search` returns
  occurrence counts for ordering.
- **`BRAIN_INDEX_SESSION_DAYS` defaults to 0 (off).** Turning it on bounds index growth but
  makes old checkpoints findable only lexically. Enable deliberately.
- **`embed.py`'s module-top block pins the fastembed cache and HF timeouts before any fastembed
  import.** Machine-local cache, `HF_HUB_OFFLINE` when the model is cached, bounded timeouts,
  ripgrep fallback. Knobs: `BRAIN_EMBED_OFFLINE=0`, `BRAIN_EMBED_CACHE`, `BRAIN_EMBED=0`. Slow
  recall: check the cache dir and the offline flag first.
- **The lexical query is data: `_ripgrep_argv` passes `-F … -e <query> -- <root>`.** Every flag
  is load-bearing; the no-rg Python fallback keeps the same literal, case-folded semantics.
- **`_connect()` returns with no transaction open; the query path uses `_connect_ro()`.**
  `backlog()` and `text_recipe_changed()` take `timeout=`; `backlog()` raises `IndexBusy` rather
  than returning 0; every sqlite URI comes from `embed.index_uri()`.
- **One unreadable note costs the index one row, not the pass.** `sync()`/`upsert()` skip
  `(OSError, ValueError)` per file; the detached reindex logs to `.index/reindex.log` — read it
  when `INDEX_STALE` persists across sessions. `embed_text()` reads `EMBED_READ_BYTES`, decoded
  incrementally.
- **The reindex lock is pid-owned, heartbeated, and stale only when old AND dead; the recipe
  lives on each row so a rebuild resumes.** Never plant a literal pid in a test — use
  `conftest.dead_pid`. Drive `sync()` in tests with the stub-embedder pattern.
