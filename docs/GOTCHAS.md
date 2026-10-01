# Gotchas that will bite you — the case histories

This is the incident record behind the rules in `CLAUDE.md` § *Rules that will bite you*.
Each rule there names a heading here. `CLAUDE.md` carries the rule and how to apply it;
this file carries what happened, what was measured, and why the fix has the shape it has.
Read the entry here before changing anything a rule guards — the rule is the summary,
and every one of these was paid for with a real incident.

Entries are in the order they were written, not by importance. Dates are when the fix
landed; `F<n>` numbers refer to the 2026-09-01 security-review findings.

### Recall's index sync is time-boxed on purpose — don't make it unbounded again (fixed 2026-08-24)

**Recall's index sync is time-boxed on purpose — don't make it unbounded again
(fixed 2026-08-24).** `vault.search_memories()` calls `EmbedIndex.sync()` in the
foreground on every recall. It used to embed the *entire* backlog before returning a
single result, so an index six weeks behind turned one `brain recall` into a **2m45s**
stall (897 files). The MCP server had a `_background_embed_warmup` thread and so never
showed it; the CLI — the primary interface — had no equivalent and paid it inline.
`sync()` now embeds **newest-mtime first**, in `SYNC_CHUNK`-sized batches, **committing
per chunk**, until `BRAIN_SYNC_MAX_SECONDS` (default 5s) is spent; `budget_seconds=0`
means unlimited and is what `brain reindex` and the MCP warmup pass. Per-chunk commit is
load-bearing: without it a truncated pass keeps no progress and every recall redoes the
same work. Chunk size is the overshoot granularity, since the deadline is only checked
between chunks — batching buys almost nothing here (464 ms/doc at batch=1 vs 419 at
batch=32), so keep it small.

Embedding cost scales with **token count up to the model's 512-token cap**, then flattens
— bodies past ~1500 chars all cost the same ~420 ms. Anything that shortens what actually
reaches the model is the real lever (a representative slice, not a blind truncation);
`batch_size`/`threads`/`parallel` are all noise. Catch-up runs via `brain reindex`
(cross-process lock at `.index/reindex.lock`, stale after 30 min) and the SessionStart
hook's `spawn_background_reindex()` — a **detached process**, not a thread, because hooks
exit in seconds and a daemon thread dies with them. `BRAIN_AUTO_REINDEX=0` disables it;
doctor's `INDEX_STALE` warns at >=50 pending so the latency can't hide again.

**A foreground sync returns immediately while the reindex lock is held, and every
*writer* carries a 30s busy timeout (`SQLITE_BUSY_TIMEOUT_S`) — both are load-bearing
(2026-08-24).** A recall during a running reindex used to kill it with "database is
locked": sqlite's default busy timeout is 5s, and the reindex is the process that loses
that race because it holds the longer transaction. The backlog it was draining then
silently never drained — and since SessionStart kicks a reindex and sessions usually open
with a recall, that was an ordinary session start, not a corner case.

**Readers must NOT copy the writers' 30s.** `doctor` connects read-only from inside the
SessionStart hook, which Claude Code kills at 15s — and a killed hook drops the entire
preload, which is worse than any finding it could have produced. One `PRAGMA
integrity_check` against a locked index measured **41.8s** at the writer's setting. So
doctor checks the cross-process reindex lock *first* (one file stat, and four of its
checks touch the index, so the busy timeouts would otherwise be additive) and waits
`doctor.index_busy_timeout()` — 2s — when it does connect. Waiting longer cannot improve
the diagnosis: "someone holds the write lock" is the same answer at 2s as at 30s.

Relatedly: **a locked index is not a corrupt one.** `_check_vector_index` used to map
every `DatabaseError` — `database is locked` included — onto `INDEX_CORRUPT`, whose hint
says to delete the index. Since SessionStart both kicks a reindex and renders that banner,
and global CLAUDE.md tells the model to *act on* the banner, a perfectly healthy index
could instruct the model to delete ~900 valid vectors and pay a 300s rebuild. Locks now
report `INDEX_BUSY`/info with no destructive hint; genuine corruption still warns.

A time-boxed (foreground) sync also **skips session checkpoints entirely** — they are
68.7% of the vault's files (616 of 897) and `render.recall_payload` filters them out of
default recall anyway, so a recall spending its slice on one buys nothing. They get their
vectors from the unbounded passes instead. `_indexable()` is the single source of truth
for what deserves a vector; `sync()` and `backlog()` must both go through it or
`INDEX_STALE` will warn about work `sync()` refuses to do.

### What gets embedded is a slice, not the raw file — and the recipe is versioned (2026-08-24)

**What gets embedded is a slice, not the raw file — and the recipe is versioned
(2026-08-24).** `embed_text()` feeds the model `name` + `description` + body lead,
capped at `BRAIN_EMBED_CHARS` (default 1000; 0 restores the raw file). The raw file
led with YAML (`type:`, `machine:`, `project:`) that is noise in the vector space and
was spending the model's first tokens on it, and `write_memory` derives `description`
from the body's first line so it was embedded twice. Since cost scales with token
count only up to the 512-token cap (~1500 chars) and is flat above it, everything past
the cap was paid for and then discarded by the tokenizer anyway.

The budget was picked by measurement — a full rebuild of the 903-file vault scored
against tail-query retrieval (151 queries drawn from text past every cap):

| budget | rebuild | vs raw | tail r@1 | MRR |
|---|---|---|---|---|
| full raw file | 433s | — | 40.4% | 0.553 |
| 1500 | 415s | −4% | 41.1% | 0.564 |
| 1200 | 364s | −16% | 40.4% | 0.552 |
| **1000** | **315s** | **−27%** | **40.4%** | **0.550** |
| 800 | 258s | −40% | 35.8% | 0.516 |

Title-query retrieval was ~98% r@1 / 100% r@3 at *every* budget including 400, so the
tail is the only axis that discriminates. The cliff is between 1000 and 800: 1000 is the
fastest budget still indistinguishable from embedding the whole file, so it is the
default. Note 1500 buys almost nothing — ~1500 chars is already ~500 tokens, i.e. the
model's cap, so the tokenizer was truncating there anyway.

**`EMBED_TEXT_VERSION` must be bumped whenever `embed_text()` changes what it feeds the
model**, and the budget is part of the recipe identity. Vectors from different recipes
aren't comparable, and the mtime staleness check cannot notice — the files didn't
change, the recipe did — so the index would silently mix two vector spaces forever. A
**foreground sync deliberately refuses to transition it**, because doing that mid-recall
would leave the rest of the session querying a half-built index. Doctor's
`INDEX_RECIPE_STALE` warns until a reindex runs.

An unbounded pass handles the mismatch **in place, without wiping first** (2026-08-24).
It used to commit a `DELETE FROM embeddings` and *then* spend ~300s refilling, so every
other process read a near-empty index for the whole rebuild and silently degraded to
ripgrep — and since the SessionStart kick is what performs the rebuild, the session that
triggered it was precisely the one querying the gutted index. Now `sync()` sets
`rebuilding`, which makes every row count as pending (`prior = None if rebuilding else …`)
and replaces them one chunk at a time, so the index stays whole and queryable throughout.
The recipe is stamped **after** the last chunk, never up front: an interrupted rebuild
must not look complete, or the un-re-embedded remainder is stranded in the old recipe
forever — the mtime check cannot tell the difference, so nothing would ever come back for
it. A **model** change is the one case that still wipes (`_wipe_for_model_change`): those
vectors need not even share a dimensionality, so `np.vstack` on the mixture raises rather
than merely ranking badly.

An **empty** index is never "changed" — there are no vectors to invalidate — but it must
still be *stamped*, or it looks permanently changed and every foreground sync bails out
at 0 and the index never fills. Keep the empty-index branch stamping — and note the stamp
is written by `_connect()`, not `sync()`, because `upsert()`/`delete()` populate an index
without ever calling `sync()`: a fresh vault whose first operation was `brain save` used to
end up with a one-row *unstamped* index, which is not empty, therefore reads as
"recipe changed" forever, so every later foreground sync returned 0 and the index never
filled (fixed 2026-08-24). The stamp requires an explicit `COUNT` of 0 — a read that
*errors* must never be mistaken for an empty index, or one transient failure blesses two
incompatible vector spaces as one.

A recipe bump also has to **spawn** the rebuild, and `backlog()` cannot see one: it
compares mtimes, and a recipe change touches no files. `spawn_background_reindex()`
therefore gates on `text_recipe_changed() or backlog_count() >= min_backlog`, checking the
recipe first because it is one sqlite read where `backlog_count()` stats the whole vault.
Without that clause nothing ever transitions the index on a CLI-first install — the
foreground sync refuses to, and there is no MCP warmup — so it serves superseded vectors
until a human notices `INDEX_RECIPE_STALE` and runs `brain reindex` by hand.

### Index rows are keyed on a vault-*relative*, forward-slashed path — keep the API boundary absolute (2026-08-24)

**Index rows are keyed on a vault-*relative*, forward-slashed path — keep the API boundary
absolute (2026-08-24).** Absolute keys tie the index to one filesystem location, so moving
the vault (`D:` → `C:`, a renamed home dir, a restore onto a differently-shaped machine)
made every stored path miss against `_indexable()`; `sync()` then deleted all ~900 rows as
"stale" and re-embedded the corpus from scratch — ~300s to reconstruct vectors that were
still perfectly valid. The rule is **relative inside the DB, absolute at every API surface**:
`EmbedIndex.query()` still returns absolute paths, because `vault.search_memories` resolves
and stats them and a relative string would silently resolve against the process cwd.
`_index_key()` / `_key_path()` / `_normalize_key()` are the only places that know the
format; `doctor`'s near-duplicate check reads the column directly and has its own
absolute-ize step. Upgrading is a **rename, not a re-embed** — `_migrate_path_format()`
runs from `_connect()`, rewrites the keys in one transaction (908 rows, ~1s), stamps
`path_format` in `meta`, and clears `_MATRIX_CACHE`, whose `(project, count, max mtime)`
key a pure rename would otherwise slip straight past.

**This is not what keeps the index machine-local, and a previous session's claim that it
was is wrong.** `.index` is a hidden directory that is not `.obsidian`, so Obsidian never
enumerates it and Obsidian Sync never propagates it. Verified 2026-08-24 against this
vault's own record: four machines across two OSes (`spanier-geekom-ai01` 196 memories,
`joes-macbook-pro-3` 119, `strixlappy` 7, `joes-macbook-air` 3) over four months, zero
conflict files, no thrash. A shared sqlite would have made a full ~900-file re-embed the
cost of *every* Mac↔Windows switch. So don't drop a platform to "fix" this, and adding a
Mac back is not blocked. What does remain true: machine-locality is a property of the sync
tool, not of the code. Dropbox, OneDrive, iCloud, Syncthing and git all copy dotfiles;
relative keys stop the path thrash there but a live sqlite file under two writers still
risks real corruption. Keep the vault on Obsidian Sync.

### `_MATRIX_CACHE` is keyed on a `vector_epoch` counter, not just row shape — and never name a loop variable `key` in `_normalized_matrix` again (2026-08-24)

**`_MATRIX_CACHE` is keyed on a `vector_epoch` counter, not just row shape — and never
name a loop variable `key` in `_normalized_matrix` again (2026-08-24).** The cache
signature was `(project, row count, max mtime)`, which are all *row* properties: any write
that changes a vector's contents while leaving the row set alone slips straight past it.
That was sound until a recipe rebuild existed (same paths, same mtimes, new vectors) and
the path-format migration (a pure rename). `_bump_vector_epoch()` lives in `meta` rather
than clearing the in-memory dict specifically so it works **cross-process** — a long-lived
MCP server has to notice a `brain reindex` that ran elsewhere, and clearing its own dict
cannot tell it that. Every vector write bumps it: sync's chunk commits and stale deletes,
`upsert`, `delete`, the wipe, the migration.

The row loop uses `row_key`, because naming it `key` shadows the signature computed a few
lines above: the cache then stored the *last row's path* as its key, so every lookup
compared a `str` against a tuple, missed, and re-read every BLOB on every query. That
regression was introduced and caught the same afternoon — the test that catches it asserts
the key's first three components are identical across a rebuild while the epoch differs.

### `embed_text()` reads each file once

**`embed_text()` reads each file once.** It needs the raw text for its fallback path
anyway, so it parses via `vault.Memory.from_text(path, raw_text)` rather than
`from_file()`, which would re-read the same file from disk — doubled I/O across a
~900-file rebuild for nothing. `from_file()` is now a one-line wrapper over `from_text()`;
keep the split.

### Lexical-only hits get a reserved slot every 3rd position — don't "simplify" that back to appending them (fixed 2026-08-24)

**Lexical-only hits get a reserved slot every 3rd position — don't "simplify" that back to
appending them (fixed 2026-08-24).** `search_memories` used to append *all* ripgrep hits
after *all* 20 vector hits, so a file with no vector sorted to the bottom however well it
matched: a query whose literal text appeared in exactly one un-vectorized file ranked it
**21st of 21**, invisible at any sane `top_k`. `_merge_lexical` now weaves lexical-only
hits in at `LEXICAL_SLOT_EVERY` (3, chosen because `DEFAULT_TOP_K` is 3 — so the best
literal match is always visible in a default recall while positions 1–2 stay pure vector
ranking). Only the first `LEXICAL_MERGE_CAP` (20) participate; the rest still append, so
total match counts don't change and a broad query can't hand a third of the ranking to
files that merely mention the word — that was the 2026-07-11 blowup, and it would come
straight back through this door.

Two things had to be fixed alongside it, both of which only became visible once lexical
hits stopped sorting last: `EXCLUDE_FILES` keeps `activity.md` (the Stop-hook audit log)
and `_index.md` out of both the index and the search results — `activity.md` surfaced as
the #3 hit for "windows setup" — and `_ripgrep_search` now returns **occurrence counts**
(`rg -c`) instead of bare paths, because ordering lexical hits by mtime put "most recently
touched file that mentions the word once" ahead of "file that is largely about the word".

### `BRAIN_INDEX_SESSION_DAYS` still defaults to 0 (off), even though ranking is fixed

**`BRAIN_INDEX_SESSION_DAYS` still defaults to 0 (off), even though ranking is fixed.** It
bounds index growth (checkpoints accrue ~1/session forever; hand-written memories don't),
and an excluded checkpoint is now *findable* — the reserved slot guarantees that. But it is
only findable **lexically**: a query that is semantically related without literally
matching won't reach it at all. Measured benefit today is ~15% of files on a one-time
indexing cost; the cost is a permanent loss of semantic reach over old checkpoints. Turn it
on deliberately, for a vault where index size actually hurts.

### The installers must APPEND hook groups, never assign the event — and must refuse an unparseable `settings.json` rather than replace it (both fixed 2026-08-25)

**The installers must APPEND hook groups, never assign the event — and must refuse an
unparseable `settings.json` rather than replace it (both fixed 2026-08-25).** Every installer
pruned its own stale entries and then did `settings["hooks"][event] = definition`, which threw
away every *third-party* group registered for that event: a user with their own SessionStart,
Stop or PreCompact hook silently lost it to a Brain install, and `brain-setup.py` — the
installer most installs actually run — didn't even prune, it just assigned. Worse, all four
caught `json.JSONDecodeError`, treated the file as `{}`, and wrote that back: one stray comma
in `settings.json` and installing a memory system deleted the user's entire Claude
configuration, silently, while reporting success.

Both bugs existed in five places at once (`brain-setup.py` plus an embedded Python heredoc in
each of the three shell/PowerShell scripts), which is this repo's signature failure mode. The
fix is therefore structural: `brain_settings_merge.py` at the repo root is now the only
implementation, imported by the two Python entry points and invoked as a script by the other
six. **Do not re-inline it.** `test_installer_parity.py` asserts every installer and uninstaller
routes through it and that the old fragments (`settings["hooks"][event] = definition`,
`settings = {}`, `hooks_block`) never come back; `test_settings_merge.py` asserts the behaviour.

Three details are load-bearing. **Append + prune-first together** are what make re-running
idempotent *and* non-destructive — either alone is a bug (assign loses hooks, append without
prune duplicates ours every run), which is why the tests always merge twice. **The refusal is
scoped**: a missing or whitespace-only file still means `{}` (the installers used to create
exactly that themselves, and there is nothing to lose), but a JSON list, string, `null`, or
truncated object is a hard stop with a nonzero exit and a summary of which steps *did* land —
a partial install the user knows about beats a complete config the user has lost. And **the
write is a same-dir temp file + `os.replace`, with a timestamped backup first** — but only
when the content actually changes, or every re-run of setup litters the config dir with
identical copies of the same file.

### A `project` value is a directory basename and nothing else — build every project path with `vault.project_dir()` (added 2026-08-25)

**A `project` value is a directory basename and nothing else — build every project
path with `vault.project_dir()` (added 2026-08-25).** `project` was joined straight
into a path at five sites in `vault.py` (`write_memory`, `list_memories`,
`session_start_bundle`, `ensure_project_overview_stub`, `write_checkpoint`) plus
`doctor` and `brain-compact`, and it arrives from the CLI, the MCP server (i.e. from
a model), the hooks' payload `cwd`, and the pi extension. So `..`, `../../x`,
`/etc/x`, `C:\Windows` or `\\server\share` wrote outside the vault and made reads
enumerate arbitrary markdown on the disk — `session_start_bundle("../../stash")`
would preload someone else's `feedback/*.md` into the model's system prompt.

`vault.validate_project_name()` is THE predicate, in the same sense as
`is_memory_path()`, and `vault.project_dir(project, *parts, root=…)` is the only way
to turn one into a path; `vault.projects_root()` is the only way to enumerate the
directory. The rule is a **blacklist, not a whitelist**: a project name is a
basename off the user's own disk, so letters of any script, digits, spaces, dots,
dashes, underscores and ordinary punctuation must all survive — a tidy
`[a-z0-9-]` whitelist would silently orphan real project directories, which is a
worse outcome than the traversal. Rejected: empty; leading/trailing whitespace (a
Windows directory silently loses it, so the created dir would not carry the name we
validated); `.` / `..` / dots-only / a trailing dot; the characters
`/ \ < > : " | ? *` (which alone covers every separator, `C:foo`, UNC, and NTFS
alternate data streams); control characters; the Windows device names `CON PRN AUX
NUL COM1-9 LPT1-9` — the vault syncs to Windows, so a name only macOS accepts is a
project that cannot exist on half the machines; and anything over
`PROJECT_NAME_MAX_LEN` (96, chosen so the deepest path this appears in —
`<vault>/Brain/projects/<name>/sessions/<stamp>-<machine>_NN.md` — stays inside
Windows' 260-char MAX_PATH; the longest real name in the vault is 27). `project_dir`
additionally resolves the result and refuses anything not under the resolved
`Brain/projects/`, which is what catches a *symlinked* project directory.

Two boundary rules matter as much as the predicate. **`vault.project_basename()`
sanitizes and returns None; it never raises** — it feeds the SessionStart hook, and a
hook that raises drops the entire preload, so losing one session's project scope
beats losing every behavioural rule. `hooks/_common.project_basename()` delegates to
it rather than keeping its own `Path(cwd).name`. And **`doctor.check()` downgrades an
invalid project to a `PROJECT_NAME_INVALID` warn and continues unscoped**, for the
same reason. The CLI reports the rule with no class name and no traceback (exit 2);
the MCP server returns an error *result*, because a raise out of `call_tool` kills the
stdio loop and takes the session's whole memory with it.
`tests/test_project_path_safety.py` fails the build if any module under `brain_mcp/`
or `hooks/` joins a project value under `"projects"` itself — that invariant, not the
input battery, is the test that matters here.

### Checkpoint filenames must be claimed with `O_EXCL`, not composed and hoped for (fixed 2026-08-25)

**Checkpoint filenames must be claimed with `O_EXCL`, not composed and hoped for
(fixed 2026-08-25).** `write_checkpoint` named files `<YYYY-MM-DD-HHMM>-<machine>.md`
— minute precision — so two checkpoints for the same project on the same machine
within one minute were the same path and the second silently replaced the first.
PreCompact firing and SessionEnd firing seconds later is the ordinary case, not a
corner case, and the checkpoint that captured the *most* context was the one
destroyed. Seconds in the stamp is not the fix on its own: those two are separate
processes, so any exists-then-write still races. `_reserve_checkpoint_path()` claims
the name with `os.open(..., O_CREAT | O_EXCL)` and walks `_02`, `_03`, … on
collision.

The disambiguator is introduced by `_`, deliberately: `_` (0x5F) sorts after `.`
(0x2E), so `…-host.md` still precedes `…-host_02.md`, whereas the obvious `-02`
(0x2D) sorted the *second* checkpoint of a second ahead of the first and broke the
string ordering the scheme exists to preserve. It is zero-padded for the same reason.
Legacy minute-precision names still sort correctly against the new ones. The
reservation is a real empty `.md`, so a failed write unlinks it — left behind it
would be the newest file in `sessions/` and therefore the preload's latest-session
slot, i.e. a contentless "most recent checkpoint".

Claiming the destination also makes `_atomic_write`'s temp name unique, which was a
live bug one level down: the temp was a fixed `<name>.md.tmp`, so two processes
writing the *same* memory interleaved their bytes into one temp file and then both
renamed it over the real note. Temp names now carry pid + a process-local counter,
and still must not end in `.md` (vault globs, the embed index and Obsidian Sync all
key on that). `transcript._save_state` went through `_atomic_write` for the same
reason.

### Frontmatter is machine-written YAML — build it with `vault._frontmatter()` and write with `vault._atomic_write()`, never f-strings + `write_text` (fixed 2026-07-29, Windows incident)

**Frontmatter is machine-written YAML — build it with `vault._frontmatter()` and write with
`vault._atomic_write()`, never f-strings + `write_text` (fixed 2026-07-29, Windows
incident).** Three failure modes were live at once: (1) an interpolated title or
auto-description containing a colon (`name: F1 job path: .xf is a tar`) is invalid YAML, so
the note silently lost its `type` and vanished from every type/project-filtered recall —
81 vault files were affected when the doctor check landed; (2) the CLI decoded stdin with
the platform default (cp1252 on Windows), turning UTF-8 em dashes into mojibake, and an
undecodable byte could kill a save *mid-`write_text`*, truncating the existing note —
`cli.py` now forces UTF-8 on stdin/stdout/stderr and all vault writes go through a
tmp-file + `os.replace`; (3) `--project` filtering matched the substring `/projects/X/`,
which never matches Windows backslash paths — use `vault.path_in_project()` (path
components) everywhere, including `embed.py`. `brain doctor` now flags
`MALFORMED_FRONTMATTER`; if it fires, re-save the note or fix the YAML.

### The preload carries the rule, not the case history — `**Why:**` is deferred to recall (2026-08-25)

**The preload carries the rule, not the case history — `**Why:**` is deferred to recall
(2026-08-25).** Every feedback memory is a rule lead, a `**Why:**` recounting the incident
that produced it, and a `**How to apply:**`. The lead and the how-to-apply are directives;
the Why is evidence for judging edge cases, and it is **37% of the feedback corpus by
bytes**. `vault.preload_text()` replaces it with a short marker in the elastic sections
only, taking the session bundle 59.1 → 49.7 KB and the subagent bundle (the binding
constraint) from **91% → 74%** of its budget.

This is **lossless and reversible**: nothing on disk changes, `brain recall` still returns
the whole body, the marker tells the model the rationale is one recall away, and
`BRAIN_PRELOAD_DEFER_WHY=0` restores full bodies. That property is the whole reason to
prefer it over summarizing — these files are the record of corrections the user has given,
and a lossy rewrite of that record is the failure the Brain exists to prevent. `preload_text`
is deliberately conservative: it only cuts between a `**Why:**` and a following
`**How to apply:**`, so a memory whose entire substance is its rationale is left whole.

**Project-scoping is NOT the lever here, despite an earlier session claiming it was.**
Measured 2026-08-25: only 3 of 23 global feedback entries (4.7 KB, 9%) are genuinely
project-specific — two paseo, one LB-RAG. The 2026-08-06 scoping pass already took the
40% win; there is no second one. Useful detector if you look again: a global entry with
high cosine to an existing *project-scoped* entry is probably mis-scoped — that is how all
three surfaced. There are also no true duplicates left (nothing ≥ 0.85), though a
git-workflow cluster of 6 files sits at 0.79–0.83 and is one topic spread across six
memories.

### `vault.is_memory_path()` is the ONLY answer to "is this a memory" (unified 2026-08-24)

**`vault.is_memory_path()` is the ONLY answer to "is this a memory" (unified
2026-08-24).** There were three, and they disagreed: `iter_indexable_md` applied
`EXCLUDE_DIRS` + `EXCLUDE_FILES`, `list_memories` applied its own `_setup`/leading-underscore
filter, and `doctor.NON_MEMORY_NAMES` was a third list that alone knew about `README.md`.
So `brain list` returned `activity.md` — the 224 KB Stop-hook audit log — and the vault's
README as memories of type `unknown`, while both were correctly absent from recall and from
the index. Route every vault enumeration through the predicate; a test fails if any module
names those filenames literally again.

### `brain list` is capped too, and its truncation is round-robin across types — don't "simplify" that to a slice (2026-08-24)

**`brain list` is capped too, and its truncation is round-robin across types — don't
"simplify" that to a slice (2026-08-24).** `list` was the one path through `render.py` with
no cap, in a module whose entire purpose is bounding payloads: 287 entries / 57 KB by
default, 916 / 140 KB (~35k tokens) with `--include-sessions`, growing by one checkpoint per
session forever. But capping it in `list_memories`' path order (feedback, projects,
references, user) exhausted the budget inside the 224-entry project bucket and dropped
**every** user and reference memory — losing a whole category is much worse than losing a
slice of each, and user/feedback are the two the model's behaviour depends on. Selection is
now round-robin by type so the large bucket absorbs the truncation, and the omission is
always reported with the filters that would narrow it.

### Recall/list output is deliberately capped — don't "fix" that by removing the caps (added 2026-07-11)

**Recall/list output is deliberately capped — don't "fix" that by removing the caps
(added 2026-07-11).** Before `render.py`, `brain_recall` honored a model-supplied `top_k`
unbounded and `full_body=true` uncapped, and the result set merged *all* ripgrep substring
hits after the vector top-K. Every session checkpoint for a project mentions the project's
name, so a recall on a project name matched the whole `sessions/` history — one LMStudio
recall returned 200k+ tokens and blew the local model's context. Now: `top_k` defaults to 3
and is clamped (`BRAIN_RECALL_MAX_K`, default 10), previews are ~300 chars
(`BRAIN_RECALL_PREVIEW_CHARS`), `full_body` bodies are capped per file
(`BRAIN_RECALL_MAX_BODY_CHARS`, 6000) and per response (`BRAIN_RECALL_MAX_TOTAL_CHARS`,
20000), session checkpoints are excluded unless `include_sessions` is passed, and output is
compact markdown instead of `indent=2` JSON. Both frontends must keep routing recall/list
through `render.py` so a new frontend can't reintroduce the unbounded path.

### `brain_recall` could hang the whole session on the embedding-model load (fixed 2026-06-03)

**`brain_recall` could hang the whole session on the embedding-model load (fixed 2026-06-03).**
fastembed 0.8.0's `TextEmbedding()` makes a HuggingFace metadata round-trip on *every*
construction even when the model is fully cached, with no timeout, while holding
`_Embedder._lock`. The synchronous recall handler (and the background warmup thread that grabs
the lock first) block behind it, so a slow or unreachable hub turns a single `brain_recall` into
an unbounded hang (one observed lock-up ran ~1h). Two things compounded it: fastembed's default
cache is `tempfile.gettempdir()/fastembed_cache`, but Claude Code rewrites TMP for child
processes (e.g. `…\Temp\claude\…`), so the server often looked in an empty dir and re-downloaded
the 64MB ONNX every start; and `huggingface_hub` freezes `HF_HUB_OFFLINE` / `HF_HUB_*_TIMEOUT`
into module constants at import, so they must be set *before* fastembed (hence the hub) is
imported. The fix lives in `embed.py`'s module-top block: it pins a stable machine-local cache
(`%LOCALAPPDATA%\Ai-Brain\fastembed` on Windows, `~/.cache/ai-brain/fastembed` elsewhere), sets
bounded HF timeouts and `HF_HUB_OFFLINE` (only when the model is already cached) before any
fastembed import, and passes `cache_dir` to `TextEmbedding`. On a genuine cache miss it stays
online (bounded by the timeouts) so vector search self-heals; total failure falls back to
ripgrep. **Knobs:** `BRAIN_EMBED_OFFLINE=0` lets HF check for model updates every load;
`BRAIN_EMBED_CACHE` / `FASTEMBED_CACHE_PATH` relocate the cache; `BRAIN_EMBED=0` disables vector
search entirely. If recall ever feels slow again, confirm the model exists under the cache dir
and that `HF_HUB_OFFLINE=1` is being set — a missing cache forces the online path.

### `brain-setup.py` could report success while leaving brain unregistered (fixed 2026-06-03)

**`brain-setup.py` could report success while leaving brain unregistered (fixed 2026-06-03).**
The old `register_mcp` verify trusted a transient `claude mcp list` snapshot: if any line
starting with `brain` appeared at that instant, it returned success. That hid two real failures —
(a) you installed into one config dir but run Claude under a *different* one (e.g. registered
`.claude` but launch with `CLAUDE_CONFIG_DIR=.claude-f42`), and (b) the entry was written but
didn't persist to the target dir's `.claude.json`. Symptom: brain context still preloads (that's
the SessionStart *hook*, independent of MCP), but the `brain_*` tools are absent, and setup
printed no warning. The fix re-reads the target dir's actual `.claude.json` `mcpServers` map
after `claude mcp add` instead of grepping `claude mcp list`. If you see this symptom again,
check `mcpServers` in the `.claude.json` of the config dir Claude is *actually* launched with
(`$env:CLAUDE_CONFIG_DIR`), not just whichever dir setup targeted. Note the default `.claude`
dir's config file lives at `~/.claude.json` (home), not inside `~/.claude/`.

### Whatever writes the two behavioural templates must name its encoding explicitly (2026-08-24; the script that caused it is gone, the bug class is not)

**Whatever writes the two behavioural templates must name its encoding explicitly
(2026-08-24; the script that caused it is gone, the bug class is not).** The retired
`setup-windows.ps1` ran under Windows PowerShell 5.1, where `Get-Content -Raw` defaults to
the ANSI codepage: it decoded the templates' UTF-8 em dashes and ellipses as cp1252 and wrote
the mojibake straight back out — 43 corrupted sequences in the generated global `CLAUDE.md`
and 7 in the brain skill, i.e. the two load-bearing behavioural files, produced by the one
step whose entire job is to produce them. The templates themselves were clean, so nothing
upstream showed the damage.

`brain-setup.py` owns that step now, and Python is not immune: `read_text()` with no
`encoding=` uses the locale default, which is cp1252 on a stock Windows. Every file
read/write in the installer therefore passes `encoding="utf-8"`, and
`test_installer_parity.py::test_the_installer_reads_and_writes_templates_as_utf8` parses the
file with `ast` to prove it — parsed, not grepped, because a regex for the call stops at the
first `)` and reads `write_text(t.replace(a, b), encoding="utf-8")` as unqualified. Same
failure class as the 2026-07-29 CLI-stdin incident; verify with a mojibake byte-count
(`Ã¢â¬`) after any change to that step, not by eyeballing the file.

### Never install brain-mcp editable

**Never install brain-mcp editable** (`pip install -e .`). The .pth file generated by setuptools
doesn't reliably activate at startup, so `import brain_mcp` fails from any cwd other than the
project root. Use plain `pip install .` (non-editable) — `brain-setup.py` already does
this. If you "fix" it back to editable, hooks will silently break for anyone launching them from
a foreign cwd (which Claude Code does).

### User-scoped MCP servers are not registered by dropping a .mcp.json file

**User-scoped MCP servers are not registered by dropping a .mcp.json file.** Claude Code only
reads `.mcp.json` from the current project dir. User scope lives in `~/.claude-*/.claude.json`
and must be written with `claude mcp add --scope user`. Do not try to hand-write it.

### Hooks must set `BRAIN_VAULT` in the command string itself

**Hooks must set `BRAIN_VAULT` in the command string itself**, because the subprocess inherits
the parent's env but the parent (Claude Code) doesn't export `BRAIN_VAULT`. On macOS, the
`settings.hooks.json` template wraps each command as
`BRAIN_VAULT=<vault> <venv python> <hook>.py`. On Windows, Unix-style env prefixes don't work,
so `brain-setup.py` generates a `brain-launch.cmd` wrapper that sets the env and execs the
hook — `settings.hooks.win.json` just invokes that wrapper with the hook name as the argument.
Preserve whichever pattern matches the platform.

### Never walk up from `__file__` to find the vault

**Never walk up from `__file__` to find the vault.** That used to work when hooks lived inside
the vault itself; now they live in this repo, which has no relationship to the vault path.
Always read `BRAIN_VAULT` from env.

### Keep the vault out of macOS TCC-protected folders

**Keep the vault out of macOS TCC-protected folders** (`~/Documents`, `~/Desktop`,
`~/Downloads`, iCloud Drive) — recommended location is `~/Vaults/Ai-Brain`
(2026-07-28 incident). TCC grants file access *per host application*, and an ungranted host
can still **create** vault files (macOS stamps them with a `com.apple.macl` xattr binding
the creator) while getting EPERM opening pre-existing ones. So saves and checkpoints keep
"working" while overwrites, xattr reads, and the sqlite vector index fail — the doctor
reports a phantom `INDEX_CORRUPT` and `brain save` over an existing file (e.g. an overview
stub) raises PermissionError, sandboxed or not. If those symptoms appear, check the vault
path and the host app's Full Disk Access before debugging `vault.py` or rebuilding the
index. `brain-setup.py`'s `default_vault()` prefers `~/Vaults/Ai-Brain` and only falls back
to the legacy `~/Documents/Vaults/Ai-Brain` when a vault already exists there.

### The pre-approved CLI invocation is an *unattended* one — keep the file-import options off it (2026-08-25)

**The pre-approved CLI invocation is an *unattended* one — keep the file-import
options off it (2026-08-25).** The installers put the Brain command in
`permissions.allow` so proactive saves never raise a prompt; an unanswered prompt is
indistinguishable from the model deciding not to save, which is the failure the Brain
exists to prevent. But pre-approval means a prompt-injected model can run that command
with no human in the loop, and `brain save user notes --file ~/.ssh/id_rsa` copies the
file into the vault, where an ordinary `brain recall` hands it back and the SessionStart
preload may load it unasked. Demonstrated during the review: the Windows hosts file
landed in `Brain/user/` as a preloading "user memory".

**Narrowing the permission rule cannot fix this** — Claude Code's rules are *prefix*
matches, so `Bash(<cmd> save:*)` still matches `save --file <anything>`. The enforceable
boundary is `BRAIN_AGENT_SURFACE=1`, set from inside the generated
`brain-agent.py` launcher on every platform (2026-09-01; it used to be a `.cmd` wrapper on
Windows and an env prefix on POSIX — both gone, see the F2 gotcha).
`cli._enforce_agent_surface` refuses `--file`, `--from-pi` and `--from-cherryd` under it
and exits 2. An invocation *without* the variable is simply not pre-approved, so it
prompts like any other command — which is why operators, timers and the pi extension
(which clears the variable in its own spawns' env only)
keep the full CLI. Three things must stay in step or the boundary is decoration: the
installer must bake the variable into the launcher, `SKILL.md`'s own `allowed-tools` must stay
narrowed (it is a *second* pre-approval door), and any new option that opens a
caller-supplied path must join `cli.RESTRICTED_OPTIONS` — `test_agent_surface.py`
asserts all three.

The other half of that hole — an injected `--content` planting standing instructions —
was closed the same day by the trust fence below.

### Vault content reaches a model fenced, and the fence must stay unclosable from inside (ROADMAP 3F, 2026-08-25)

**Vault content reaches a model fenced, and the fence must stay unclosable from inside
(ROADMAP 3F, 2026-08-25).** Memory bodies are written by anything that can reach
`brain save` — a prompt-injected agent in another session included — and then load
verbatim into every later session's and every subagent's system prompt, in the position
the model reads as the operator's own text. So every surface that puts vault text in
front of a model wraps it in `<<<BRAIN-MEMORY-BEGIN>>>` … `<<<BRAIN-MEMORY-END>>>`,
preceded by a notice naming the block as stored data.

`vault.fence()` is the only way to produce that wrapper and `vault.neutralize_fence()` is
what makes it mean anything: a fence the fenced text can close is decoration, so anything
resembling a marker inside vault content — case, spacing, underscores, missing angle
brackets — is replaced with `[brain-fence marker removed]`. Neutralization runs where
content **enters** the payload (`session_start_bundle`'s two adders, `render.py`'s body
and description clipping), not only in the markdown renderer, because
`brain_session_start` hands its dict to MCP clients that assemble their own prompt —
defanging in the renderer alone would have made the boundary Claude-Code-only. It also
has to run *before* clipping, or a truncation could split a defanged marker back apart.

Two rules about position. **`brain_prep.render` and `render.py` are the only two modules
allowed to render vault text**, and both must call `fence()` — every frontend and all
three preload paths already route through them, which is what makes one convention
reachable. And **`doctor.render_banner` defangs its findings**, because the banner renders
*outside* the fence and several findings interpolate vault filenames: a memory named after
a marker would otherwise close the fence from the one piece of ground that must not be
closable.

The notice deliberately does **not** say "ignore this". Feedback memories are the user's
own standing corrections and exist to shape behaviour; the line is between shaping *how*
the current request is carried out and authorizing an action on their own — a command to
run, a file to send, an address to fetch, a credential to use, a confirmation to skip.
Softening that into "don't trust the vault" would break the Brain's whole purpose.

The fence costs ~0.9 KB, **reserved out of the bundle budget rather than added on top of
it** — otherwise `BUNDLE_SATURATED` under-reports by a fixed amount forever, which is the
2026-07-30 silent-drop failure through a new door. That reservation is also why the
never-return-an-empty-bundle guard counts *items* now: `consumed_bytes > 0` stopped
meaning "something loaded" the moment the fence was reserved into it. Measured after:
session 43.4/72 KB, subagent 35.2/56 KB, nothing skipped.

Finally, a fence the model has never been told about is decoration too: all three
behavioural templates (`global-CLAUDE.md`, the brain skill, `AGENTS-brain.md` — which the
pi extension strips its own guidance from) teach the convention, and
`test_trust_boundary.py` fails the build if one of them stops naming the markers, if
either renderer stops fencing, or if a hostile body can close the fence.

### `Path.resolve()` is not prefix-stable on Windows — normalize both operands before comparing them (2026-08-25)

**`Path.resolve()` is not prefix-stable on Windows — normalize both operands before
comparing them (2026-08-25).** CPython asks the OS for the final path, which always comes
back in the `\?\` extended-length form, then strips that prefix only if it can re-resolve
the stripped form and get the same answer. When that verification call fails — the path is
being created or removed by another thread right then, or it exceeds MAX_PATH — the prefix
survives. So one operand can be `\?\C:\…` while the other is `C:\…`, and a containment
test between them rejects a perfectly legal path. Found when 40 concurrent
`write_checkpoint` calls for ONE ordinary project raised `ProjectNameError` and dropped the
checkpoints — in exactly the concurrent-checkpoint case the uniqueness work exists to make
safe. `vault._comparable()` strips the prefix (`\?\UNC\` too) and case-folds, and is used
by `project_dir`'s containment check and by `forget_memory`'s "inside the Brain dir" guard.
Both guards *fail safe*, so the symptom is an intermittent refusal, not a hole — which is
why it survived a green test suite. Any new resolved-path comparison goes through it.

### Claude Code caps ONE hook command's `additionalContext` at 10,000 chars — the preload is delivered in `vault.PRELOAD_PARTS` parts, and the vault-side byte budget cannot see this (fixed 2026-09-01, F1)

**Claude Code caps ONE hook command's `additionalContext` at 10,000 chars — the preload is
delivered in `vault.PRELOAD_PARTS` parts, and the vault-side byte budget cannot see this
(fixed 2026-09-01, F1).** Anything larger is written to
`<config>/projects/<proj>/<session>/tool-results/hook-*-additionalContext.txt` and the model
receives a `<persisted-output>` header plus ~2 KB (bracketed on 2.1.258 with a controlled
hook: 9,526 chars delivered whole, 11,026 spilled). The session bundle was ~44 KB and the
subagent bundle ~35 KB, so from at least 2026-08-06 no session or subagent received a user
memory, a feedback rule, the overview or the latest checkpoint — only the banner and the
index — while `BUNDLE_BUDGET_OK` reported clean. The cap is **per hook command** (a 9.5 KB
test hook arrived intact in the same session where the Brain's hook spilled), so both
templates register `PRELOAD_PARTS` (7) entries per preload event, each
`--part I --parts N`, and `brain_prep.render_parts` renders one part per entry under
`BRAIN_HOOK_OUTPUT_CAP` (9000; headroom for the JSON envelope). Parts arrive in **any
order** (observed 3, 4, 2, 1), so every part is self-describing, carries its own trust
notice (full on part 1, `TRUST_NOTICE_SHORT` after) and its own fence; part 1 holds the
banner (outside the fence), index, overview, checkpoint and project feedback, later parts
continue project-feedback → global feedback → user. Items never split; one larger than a
part is clipped with a recall hint. Whatever does not fit — plus what the byte budget
skipped — is **catalogued by name** (`- type/stem`) on the last part under "Saved but not
loaded", one `brain recall` away instead of a bare count. Only part 1 runs
doctor/stub/reindex and renders the bundle doctor built (`check(..., bundle_cache=)`).
Doctor renders exactly what the hooks emit: `PRELOAD_PART_OVERSIZED` (a part over the cap)
and `PRELOAD_OVERFLOW` (names that fell into the catalogue). Three things must agree or
delivery silently degrades: `PRELOAD_PARTS`, the entry count in **both** templates
(`test_preload_parts.py` asserts it), and `brain-launch.cmd` forwarding `%2..%9` — it used
to exec the hook with no args, which would make every entry emit the whole bundle. No
`--part` = whole bundle as one document (brain-prep, pi, LMStudio unchanged).
global-CLAUDE.md also tells the model to read a `<persisted-output>` file in full before
its first response — the stopgap for any harness or config that still spills. Sized
against the real vault on 2026-09-01: 6 parts carried all but one user memory (largest
part ~9.0 KB), so 7 is the default for headroom; at 4 parts every global feedback rule
fell into the catalogue, which is why the fill order
changed (next bullet).

### Global feedback fills before user memories (reordered 2026-09-01)

**Global feedback fills before user memories (reordered 2026-09-01).** Feedback memories
are the behavioural rules the Brain exists to deliver; user memories are context, and
`user/` had grown to 19 files / ~23 KB of mostly incident notes. Under the per-hook cap
whatever fills last is what gets catalogued instead of loaded, and with user first every
one of the 16 global feedback rules was pushed off the end — the same self-defeating shape
the 12 KB subagent budget produced on 2026-07-30 (11 user entries, zero feedback). Order is
now project-feedback → global feedback → user. If the corpus outgrows 7 parts again, the
right move is compacting `user/` (re-type incident notes as `project`/`reference`), not
another reorder.

### `doctor.check()` isolates every check, and the SessionStart hook never treats a check failure as a vault failure (F4, 2026-09-01)

**`doctor.check()` isolates every check, and the SessionStart hook never treats a check
failure as a vault failure (F4, 2026-09-01).** Eighteen checks ran with no isolation and
the hook caught everything in one `try`, so any raising check emitted a banner blaming a
package import and exited *before building the bundle* — one bad file cost the session
every memory. Concrete raisers: a cp1252 note (UnicodeDecodeError is a ValueError, not
OSError), `type: [x]` (`list in set` → TypeError), non-mapping YAML in
`is_overview_stub`/`read_frontmatter_type`, and `p.stat()` in a comprehension on a
checkpoint deleted between glob and stat. Now `_run_check` turns a raise into an info
`CHECK_FAILED` finding; `vault._read_frontmatter_head` returns None for anything that is
not a UTF-8 mapping; sort keys and `max()` over files use `vault.safe_mtime`
(`test_no_sort_key_on_the_preload_path_stats_unguarded` fails the build on
`key=lambda p: p.stat()` in vault/doctor/brain_prep/the preload hooks — `compact.py` and
`transcript.py` still carry the pattern off the preload path); the hook separates import
failure (banner, stop) from doctor failure (`DOCTOR_FAILED` warn on top of a full preload)
and wraps the render. `search_memories` catches `(OSError, ValueError)` per file for the
same reason: a cp1252 note that ripgrep matched lexically costs one hit, not the recall.

### Pinned preload items are clipped, and the never-empty guard counts elastic items only (F5, 2026-09-01)

**Pinned preload items are clipped, and the never-empty guard counts elastic items only
(F5, 2026-09-01).** `add_pinned` never checked the budget and the guard counted pinned
items, so one 80 KB checkpoint consumed the whole bundle (81.98/72 KB), every user and
feedback entry was skipped, and `MEMORY_SIZES_OK` reported clean because it scans
user/feedback only. Overview and latest checkpoint are clipped to
`vault.pinned_max_chars()` (3500/2500 chars, `BRAIN_PRELOAD_PINNED_MAX_CHARS`) with a
marker saying where the rest is; `pinned_kb`/`pinned_clipped` ride on the bundle;
`OVERSIZED_PINNED` (info) flags overviews and newest checkpoints per project.
`latest_checkpoint()` skips 0-byte files (a leftover `O_EXCL` reservation must not become
"the most recent checkpoint") and breaks mtime ties by name. Budget knobs go through
`_budget_kb_from_env`: `inf` raised OverflowError at `int()`, `nan` raised ValueError
outside the guarded parse, both dropped the preload; now clamped to a finite positive
range (F24).

### The model-facing Brain command is a Python launcher, never a `.cmd` and never an env-prefix (fixed 2026-09-01, F2)

**The model-facing Brain command is a Python launcher, never a `.cmd` and never an
env-prefix (fixed 2026-09-01, F2).** `brain-setup.py` used to pre-approve
`<config>/brain.cmd`, a batch file forwarding `%*` to `brain.exe`. From Git Bash — which
is what Claude Code's Bash tool is on Windows — `brain.cmd recall 'x"&echo INJECTED'` ran
`echo INJECTED` as a second cmd.exe command: bash escapes the embedded quote as `\"`,
cmd.exe ignores that escape, and `%*` re-expands the text (BatBadBut, CVE-2024-24576).
Since that command sits in `permissions.allow`, the injection ran unattended; it also
voided the `BRAIN_AGENT_SURFACE` gate and let cmd expand `%VAR%` inside arguments. The
POSIX shape had the mirror-image failure: `BRAIN_AGENT_SURFACE=1 BRAIN_VAULT="…"
"/venv/bin/brain"` is a rule Claude Code's matcher does not reliably match past the
leading assignment, so it may never have pre-approved anything. Both are gone.
`brain_cmd_token()` now writes `<config>/brain-agent.py` (stdlib-only; sets both variables
in `os.environ`, then calls `brain_mcp.cli.main()`) and returns
`"<venv python>" "<config>/brain-agent.py"` — two quoted paths, on every platform. Nothing
between the model and `argv` re-parses text. `brain-launch.cmd` survives for **hooks
only**, because its arguments are fixed text from `settings.json`. Existing installs
upgrade on re-run: `is_brain_permission_rule` recognises every shape ever written
(`.cmd`, env-prefix, launcher) so the old rules are pruned, and `cleanup()` deletes the
legacy `brain.cmd` when its header says an installer generated it.
`test_agent_surface.py` imports the installer, builds the token under both `IS_WINDOWS`
values (no `.cmd`/`.bat`, no `VAR=` prefix, exactly two `shlex` words), executes the
launcher in a subprocess (`save --file` exits 2; `BRAIN_VAULT` is baked in, not
inherited) and pushes the BatBadBut payload through Git Bash against the real approved
command. If you ever need a wrapper again, it has to be one that receives `argv` already
split.

### Files the installer writes are marked, backed up when they aren't ours, and OEM-encoded when cmd.exe reads them (2026-09-01, F8/F9/F21/F26)

**Files the installer writes are marked, backed up when they aren't ours, and OEM-encoded
when cmd.exe reads them (2026-09-01, F8/F9/F21/F26).** `brain_settings_merge.write_managed_text`
is the ONE writer for the global `CLAUDE.md`, the brain skill and the generated launchers:
a destination without `MANAGED_MARKER` is a hand-written file and is copied to
`<name>.brain-backup-<stamp>` before being replaced (it used to be silently clobbered), and
the uninstaller refuses to delete anything without the marker — including `skills/brain`,
which it used to `rmtree` unconditionally. The skill's marker sits *after* its YAML
frontmatter, which must stay on line 1. Both hook templates quote every placeholder — a
space in the username split the command and every hook failed silently — and
`render_hooks_template` substitutes on the *parsed* JSON, because a backslash path spliced
into JSON text is an escape sequence (`D:\new\tab` became a newline and a tab).
`brain-launch.cmd` is written in the OEM codepage (`GetOEMCP()`, what cmd.exe actually
reads batch files in — not UTF-8, not the ANSI page Python's locale reports) with `%`
doubled in baked paths (`v%1x` spliced the hook's first argument into `BRAIN_VAULT`); an
unencodable path is a reported settings failure naming the codepage, never a traceback,
and the file's own text must stay ASCII. The parity encoding test demands the literal
`"utf-8"` everywhere in `brain-setup.py` *except* `write_windows_launch_cmd`, where it
demands a non-literal; both halves are the bug class. `cleanup()` deletes
`<config>/.mcp.json` only when it parses as a Brain-only registration.

### The lexical query is data — `_ripgrep_argv` passes it as `-F … -e <query> -- <root>`, and every one of those flags is load-bearing (2026-09-01, F3)

**The lexical query is data — `_ripgrep_argv` passes it as `-F … -e <query> -- <root>`,
and every one of those flags is load-bearing (2026-09-01, F3).** The query was positional
and regex: a query beginning with a dash was parsed by rg as an option (`--pre=<cmd>` runs
a command against every file in the vault), and it reaches this function from argparse
(anything after `--`) and from the MCP tool, i.e. from a model. `foo(` made rg exit 2 and
the lexical half of recall silently returned nothing, while the docstring promised a
literal match. `-F` makes it a fixed string, `-e` makes it an operand, `--` ends option
parsing before the root. `test_lexical_search_argv.py` fakes the subprocess boundary (rg is
not on every PATH) and asserts the argv *shape*, and pins the no-rg Python fallback to the
same literal, case-folded semantics.

### `_connect()` must return with no transaction open, and the query path must not use it (2026-09-01, F13)

**`_connect()` must return with no transaction open, and the query path must not use it
(2026-09-01, F13).** Its `INSERT OR IGNORE INTO meta` opened a transaction under sqlite3's
legacy control and nothing committed it, so every connection held the RESERVED write lock
from connect to close: the matrix load behind every recall held it for its whole BLOB
scan, `sync()` held it through the vault walk and the first chunk's embedding — not per
chunk, whatever the comment claimed — and concurrent recalls from the MCP server and the
CLI serialised on the 30s timeout. `_connect()` commits before returning;
`_normalized_matrix` opens `_connect_ro()` (`mode=ro`, `SQLITE_READ_TIMEOUT_S` = 5s).
`backlog()` and `text_recipe_changed()` take `timeout=` and doctor passes
`index_busy_timeout()`. `backlog()` raises `IndexBusy` on a lock rather than returning 0,
because 0 means "up to date". Build every sqlite URI with `embed.index_uri()` — a `#` or
`?` in the vault path truncates a raw `f"file:{path}?mode=ro"` and sqlite quietly opens
*some other file* (reproduced: an empty database and "no such table" with no error); a
test fails the build on any `sqlite3.connect("file:…")` that bypasses it.

### One unreadable note costs the index one row, not the pass (2026-09-01, F6)

**One unreadable note costs the index one row, not the pass (2026-09-01, F6).**
`embed_text()` read strict UTF-8 and the sync loop caught only `OSError`;
`UnicodeDecodeError` is a `ValueError`, so one cp1252 note escaped `sync()`: every
foreground recall fell back to ripgrep, `brain reindex` crashed, and the
SessionStart-spawned child died with stderr on DEVNULL so INDEX_STALE warned forever and
nothing said why. `sync()`/`upsert()` now skip `(OSError, ValueError)` per file and report
once per pass; the detached child writes to `.index/reindex.log` (truncated on each spawn
— **read it when INDEX_STALE persists across sessions**). Under a slice budget
`embed_text()` reads only `EMBED_READ_BYTES` (16 KB), decoded incrementally so a multibyte
character on the boundary is held back; the slice is byte-identical so
`EMBED_TEXT_VERSION` did not move.

### The reindex lock is owned by a pid, kept alive by a heartbeat, and stale only when old AND dead; the recipe lives on the row, so a rebuild resumes (2026-09-01, F14/F15)

**The reindex lock is owned by a pid, kept alive by a heartbeat, and stale only when old
AND dead; the recipe lives on the row, so a rebuild resumes (2026-09-01, F14/F15).** Age
alone (30 min) declared a live long rebuild abandoned: the next SessionStart unlinked the
live lock and spawned a second pass, the first pass's unconditional release then deleted
the second's lock, and two processes both observing "stale" could both unlink and both
create. Now `sync()` touches the lock after every chunk commit on unbounded passes,
`_lock_state` checks the pid only past `REINDEX_LOCK_STALE_S`, `REINDEX_LOCK_ABANDON_S`
(24h) bounds pid reuse, release/heartbeat act only on our own pid, takeover renames the
stale file and verifies it, and `acquire` never claims ownership on an unexpected error.
**Never plant a literal pid like `12345` in a test** — use `conftest.dead_pid`. With one
recipe stamp in `meta` written after the last chunk, an interrupted rebuild restarted
from zero; `embeddings.recipe` is added in place by `_connect()` (`ALTER TABLE` +
backfill; no vector touched, no epoch bump), every insert stamps the current recipe, and
a rebuild's pending set is rows whose recipe differs. The F6/F14/F15 tests drive `sync()`
for real by monkeypatching `embed._EMBEDDER` with a stub returning fixed 384-dim vectors —
use that pattern for any sync-path test rather than the real embedder.

### Hooks read their payload through `_common.read_payload()` and nothing else — it pins UTF-8 before the read (2026-09-01, F7)

**Hooks read their payload through `_common.read_payload()` and nothing else — it pins
UTF-8 before the read (2026-09-01, F7).** Claude Code hands hooks UTF-8 JSON, but a piped
Python on Windows decodes stdin as cp1252 and the launcher sets only `BRAIN_VAULT`; the
2026-07-29 fix covered `cli.py` only. A cwd of `D:/tmp/Café—x` arrived as `CafÃ©â€”x`,
passed `validate_project_name` (a blacklist by design), and the overview stub,
checkpoints and scoped feedback landed in a mojibake project dir while the real project
never preloaded; a Cyrillic cwd hit an undefined cp1252 byte and killed the hook.
`read_payload` calls `force_utf8_stdio()` first; `test_hook_stdio_utf8.py` runs every hook
under `PYTHONIOENCODING=cp1252` and fails the build if any hook touches `sys.stdin`
itself. Hooks also never create the vault: `vault_brain()` used to `mkdir` `Brain/`, so a
mistyped or not-yet-synced `BRAIN_VAULT` got a phantom vault on the first Stop and
`BRAIN_DIR_MISSING` could never fire again. `append_activity` writes nothing when the
directory is absent, and cuts `activity.md` back to its newest 1,500 rows once it passes
2,000 (it was 243 KB, rewritten every turn on every machine through Obsidian Sync).

### `stop.py` reads the transcript from the END, blanks quoted spans before matching, and audit rows carry `re=Y|N` (2026-09-01, F16/F17/F18)

> **Superseded in part on 2026-09-13.** The F17 command-parsing half of this entry — the
> quoted-span blanking, the heredoc blanking, the anchored `_CLI_SAVE_RE` — is gone; see
> *Saves are detected from the saver's record, not the model's command* below. The
> bounded read (F16) and the `re=Y|N` rows (F18) stand.

**`stop.py` reads the transcript from the END, blanks quoted spans before matching, and
audit rows carry `re=Y|N` (2026-09-01, F16/F17/F18).** It used to `list()` every JSON line
on every turn under the Stop hook's 5 s timeout — linear, so a long session crossed the
budget and silently lost both the gate and the audit row for the rest of its life.
`_analyze_last_turn` walks backwards in 64 KB blocks with a byte-regex pre-filter and
stops at the last user entry that carries text; `test_stop_hook.py` holds it under 1 s on
a 50 MB transcript. `is_cli_save_command` used to accept any whitespace as a boundary, so
`git commit -m "Fix brain checkpoint naming"` satisfied the gate with no save; it now
blanks quoted spans and heredoc bodies and anchors the executable at a command position
(env-prefix assignments allowed). Every save-promise pattern in `_savesig.py` requires a
Brain noun — bare `memory` was blocking turns on "store the result in memory" — and the
emphasis-strip regexes are bounded (the old one was quadratic: 1 s on 20,000 asterisks).
On a re-entry after a gate block the assistant text still spans the whole turn, promise
included, and the *first* row of a blocked turn is `pro=Y sav=N` by construction, so every
gate block produced a `PROMISE_GAP` warning; `doctor._audited_rows` treats a `re=Y` row as
superseding the preceding row for the same account+project and `_check_promise_gap` skips
gated turns. `brain_mcp.transcript.SYSTEM_TURN_PREFIXES` is THE list of system-generated
user-turn markers — `stop.py` and `parse_claude_transcript` each kept their own and 9 of
249 checkpoints carried subagent output where the user's request belonged; a test fails
the build if a marker literal appears anywhere else.

### The pi extension clears `BRAIN_AGENT_SURFACE` in its own spawns' env, never in `process.env` (2026-09-01, F23)

**The pi extension clears `BRAIN_AGENT_SURFACE` in its own spawns' env, never in
`process.env` (2026-09-01, F23).** `pi.exec` has no per-call env, so the extension
assigned `process.env.BRAIN_AGENT_SURFACE = "0"` — and the model's shell tool inherits
`process.env`, so every command the *model* ran had the gate cleared. Operator-owned
commands now go through `node:child_process` `execFile` with `shell: false` and an
explicit env. `BRAIN_PI_CMD` must be the venv executable (Node ≥ 20.12 refuses .cmd/.bat
under `shell:false`), and `resolvePrepCmd` only looks for a sibling `brain-prep` when
`brainCmd` is absolute.

"Our own spawns" first meant *every* spawn, the model-facing `brain_*` tools included
(2026-09-29). Those build argv from model-supplied strings with no `--`, so a
`brain_checkpoint` `project` of `"--from-pi=<path>"` parsed as the option — the positional
is optional — and imported an arbitrary pi session or cherryd log with the gate off. The
tools now run under `toolEnv` (gate set) and build argv through `toolArgv`: option values
glued on as `--name=value` (a separate value starting with `-` is otherwise an argparse
error, which also broke titles like `-foo`), positionals after `--`, and no `--` at all when
there are no positionals, because `brain list --` is itself an argparse error. Either layer
alone stops the import; `test_the_pi_tools_run_on_the_agent_surface` and
`test_the_pi_tool_argv_shape_keeps_model_strings_as_data` guard both.

### A save that replaces a memory archives what it replaced — and a slug is not a title (2026-09-01, F10/F11)

**A save that replaces a memory archives what it replaced — and a slug is not a title
(2026-09-01, F10/F11).** `write_memory` composed `<slug>.md` and wrote over whatever was
there: "Git discipline" and "git   discipline!" are one path, and every non-Latin title
slugified to the bare `untitled`, so a model saving a new rule under a colliding title
erased a user correction with no trace. Overwrite-by-title is a feature (the stub upgrade
relies on it), so `vault.save_memory()` does not refuse: it copies the previous content to
`Brain/archive/versions/<rel path>/<stamp>-<machine>[_NN].md` first (newest
`VERSION_KEEP`=5 kept; `archive/` is in `EXCLUDE_DIRS`, so versions never reach the index,
`brain list`, recall or a preload, but Obsidian Sync carries them), reports it (`warning:
overwrote …` on stderr; `overwrote`/`previous_version` in the MCP result), and writes
nothing on a byte-identical re-save (`unchanged`). Version names are **monotonic within a
second**, not lowest-free like checkpoints — pruning frees the lowest names, so a reused
low slot made the newest version sort as the oldest and get pruned. `slugify`
transliterates via NFKD (`Café notes` → `cafe-notes`; a pre-existing `caf-notes.md` will
not be matched by a re-save — doctor's near-duplicate check surfaces it) and appends
`-<8 hex of sha1(title)>` when no ASCII survives. Caller frontmatter is honoured only when
it parses as a mapping: a body opening with a markdown horizontal rule used to be written
verbatim with no `type`, vanishing from every typed recall. A declared `type` that differs
from the requested one is a `ValueError`, a missing one is filled in. `forget_memory`
accepted anything under `Brain/` from the pre-approved surface — `_index.md`,
`activity.md`, the sqlite index; it now requires a `.md` that `is_memory_path` accepts.

### brain-compact buckets and ages by the date in the filename, never mtime — and merges by marked section (2026-09-01, F19/F20)

**brain-compact buckets and ages by the date in the filename, never mtime — and merges by
marked section (2026-09-01, F19/F20).** mtime is when compaction last *wrote* a file, so
every daily one run produced landed in a single weekly named for the run's week, each
stage's ageing clock restarted on every run, and two machines compacting the same daily
on different days filed it under different weeks. `_day_of`/`_week_from_name` read the
date in the name; `_compact_project(…, today=)` is the injectable clock, and a test fails
if `st_mtime` is read anywhere else. Absorbed sections are marked
`<!-- brain-compact source: <name> -->` (the legacy `## <name>.md` heading is still
parsed, and headings inside absorbed bodies are no longer merge keys), a source is
reclaimed once every section it holds is in the target (the crash-between-write-and-unlink
leftover used to sit as a raw preload candidate forever), and archiving merges into an
existing archived weekly instead of `shutil.move`-ing over it. Test fixtures use
today-relative stamps: a literal January date ages past every threshold the moment the
year rolls on.

It also never rolls up a project's **newest** checkpoint (2026-09-29). Rolling every aged raw
file into `daily/` left a project untouched for a week with nothing in `sessions/*.md`, so
`latest_checkpoint` returned None: the preload lost the project's "latest session" and
`STALE_UNCOMMITTED` lost its baseline, in exactly the come-back-later case both exist for.
`_newest_by_name` keeps that one file, chosen by the stamp in its name like every other
decision here, skipping zero-byte reservations. One file per project stays bounded.

### Install turns Claude Code's auto memory off, and ownership lives in a sidecar (2026-09-29)

**Install turns Claude Code's auto memory off, and ownership lives in a sidecar.** Claude
Code's built-in auto memory is on by default, and its system-prompt section tells the model to
save `user`/`feedback`/`project`/`reference` notes under `<config>/projects/<slug>/memory/` with
a `MEMORY.md` index — the Brain's own taxonomy, stored machine-local and per repository,
invisible to pi, LMStudio and the other machines, and in direct contradiction of the global
CLAUDE.md ("those directories are obsolete and ignored"). The system prompt outranks CLAUDE.md,
which Claude Code delivers as a user message, so every session carried two competing memory
instructions. Seen in a live `.claude-work` session on strixlappy on 2026-09-29; the setting
and the `CLAUDE_CODE_DISABLE_AUTO_MEMORY` env var are documented at
code.claude.com/docs/en/memory.

`settings.json` has no room for an ownership marker, and uninstall must stay symmetric without
reversing a choice the user made, so the pre-install state goes in `.brain-auto-memory.json`
next to it: `{"present": bool, "value": …}`. It is written after `settings.json` lands (a
failed write must not claim a value we never set) and never over an existing marker (a
re-install after the user flipped the key back must remember the state from before the
*first* install). Uninstall restores it only while the key still reads `false`; a key that is
already `false` before install gets no marker, so it is never turned back on.

### Saves are detected from the saver's record, not the model's command (2026-09-13)

**`stop.py` no longer inspects shell commands to decide whether a brain save happened.** Every
model-facing save surface — `cli._cmd_save`, `cli._cmd_checkpoint`, and the `brain_save` /
`brain_checkpoint` MCP tools — calls `vault.record_save_event()` after its write lands, which
appends one JSON line (`ts`, `kind`, `surface`, `session`, `machine`, `path`) to
`Brain/.state/save-events.jsonl`. The hook reads the turn's first user-message `timestamp` from
the transcript and asks `vault.count_save_events(since=…, session_id=…)` for events at or after
it; a structured `brain_save`/`brain_checkpoint` `tool_use` block in the turn also counts.

The regex it replaced (`_CLI_SAVE_RE`, with `is_cli_save_command` around it) was patched four
times for the same predicate — `cd39ca9` (CLI-first), `17c95c1`, `3fb205b` (F17: anchoring and
quote blanking), and it would have been a fifth for this — because it had to know every shape
an installer, a wrapper or a user might spell the executable in: env-prefix assignments,
`.exe`, `.cmd`, quoted paths with spaces. The 2026-09-01 F2 fix replaced the pre-approved
command with `"<venv python>" "<config>/brain-agent.py"`, updated `stop.py` the same day for
F16–F18, and still never taught it that shape: the regex demanded whitespace immediately
after `brain`, and the launcher's name continues `-agent.py`. From that day until 2026-09-13
every save through the standard surface was audited `sav=N`, the gate blocked turns that had
just saved (a previous session's checkpoint at
`Brain/projects/Ai-Brain/sessions/2026-09-13-165148-joes-macbook-pro-3.md` records one), and
`SAVE_GAP` / `PROMISE_GAP` — the banners global CLAUDE.md tells the model to act on — were
drifting toward permanent false positives. Joe's read that day: "every time I open this
project up lately there is a new reason for false alarms", and the answer was that the
self-monitoring layer had to model the rest of the system and fell behind every time the
system changed shape.

Reading the record removes the class: the command's spelling is irrelevant, because the thing
that ran it is what writes the line. Design points that matter:

- **Session scoping.** Claude Code exports `CLAUDE_CODE_SESSION_ID` into the Bash tool's
  environment, and the Stop payload carries the same value as `session_id`, so a save from a
  parallel session does not absolve this one. An event with no session (an MCP server or a
  terminal without the variable) matches any session — a false pass of a nag gate, never a
  false block. An event's session mismatching is the only way it is excluded.
- **No timestamp, no guess.** A user entry without a `timestamp` (hand-made fixtures) yields
  `turn_start=None`, and then only the structured tool_use count applies. Counting "recent"
  events could credit the previous turn's save.
- **Bookkeeping never fails the save.** `record_save_event` swallows every exception and
  reports on stderr; `count_save_events` treats a missing file or a corrupt line as nothing.
  The memory is already on disk when either runs.
- **Machine-local by construction.** `.state/` is a hidden directory, which Obsidian Sync does
  not propagate — the same property `.index/` relies on. The hook runs on the machine the
  save ran on. The file is rotated at `SAVE_EVENTS_MAX_LINES` (400 → 200).
- **The MCP tool_use check stays.** It is a structured name comparison, not parsing, and it
  covers an MCP server whose `.state` write failed.
- **The hook's fallback when `brain_mcp` cannot be imported is `count_save_events → 0`**,
  which is correct: a venv that cannot import the package could not have saved either, and the
  `too=` column already records that condition.

`tests/test_save_events.py` asserts the class: every surface records an event (exercised,
not grepped), the hook credits exactly this turn's saves for this session, the 2026-09-13
incident replayed end to end no longer blocks, recording cannot fail a save, and
`stop.py` contains no `"Bash"`, `"PowerShell"`, `get("command"` or `is_cli_save_command` —
the regex must not come back through a new door.

### Each preload surface has its own budget knob; never size a local model from Claude Code's `settings.json` (2026-10-01)

`BRAIN_BUNDLE_BUDGET_KB` was the only budget name, and three surfaces read it: the Claude
Code hooks (default 72), the MCP server's `brain_session_start` (same default), and the pi
extension (its own default, 12). So shrinking the bundle for one reader shrank it for all of
them, or for none.

On 2026-10-01 `~/.claude-work/settings.json` carried `BRAIN_BUNDLE_BUDGET_KB: "48"`. It was
set deliberately, because the preload was overflowing smaller-context local models. But a
`settings.json` `env` block reaches only Claude Code's hooks and shell, and those sessions
ran Opus with a 1M window. The setting skipped ten user memories from every Opus session
(`BUNDLE_SATURATED`) and did nothing for LM Studio. LM Studio's `mcp.json` set no budget,
so `brain_session_start` returned the full 72 KB, about 17k tokens. It was the third time
a sub-default value in a `settings.json` had cost memories (`.claude-f42` on 2026-08-24, and
the 32 KB default on 2026-07-30).

The fix gives each surface a name of its own:

- **MCP:** `vault.mcp_budget_kb()` reads `BRAIN_MCP_BUDGET_KB` and falls back to
  `BRAIN_BUNDLE_BUDGET_KB`, so an existing `mcp.json` keeps its meaning. The tool takes
  `budget_kb` and `slim`. The argument is **lower-only**. The operator sized the config for
  the model's window, so a model asking for more is asking to overflow it. A non-numeric,
  non-finite or non-positive value is an error result, not a silent default.
- **pi:** reads `BRAIN_PI_BUDGET_KB`, then the shared name, then 12.
- **Claude Code hooks:** keep `BRAIN_BUNDLE_BUDGET_KB`. Its natural limit is the 7 hook
  parts, not the budget.

`tests/test_mcp_session_budget.py` covers the MCP precedence, the lower-only argument, bad
values, `slim`, and the advertised schema.

### MCP tools run in a worker thread, one at a time, and nothing in a tool may wait on stdin (2026-10-01)

`call_tool` ran every tool directly on the stdio server's event loop. All of them are
synchronous file and sqlite work, and a recall can spend its whole 5 s sync budget
embedding a backlog slice. For that time the server answered nothing: no ping, no
cancellation, no `tools/list`. The SDK already runs each request as its own task, so the
fix looked like one line, `await asyncio.to_thread(...)`.

That line hung the first recall indefinitely on Windows. With the loop free, the
transport's reader thread issues its next blocking read on stdin at once. A tool thread
then loaded numpy's extension DLL, and the load blocked until that read completed. An MCP
client waiting on a tool result sends nothing more, so it never completed. A stack dump
showed the tool thread inside `numpy/_core/multiarray.py`'s extension load, and the
reader thread in `anyio`'s worker. Starting a subprocess that inherits the server's stdin
hung the same way. The old handler never hit either, because a blocked loop never issued
the next read.

The rules that make the threaded handler safe:

- **`run()` calls `_preload_native_modules()` before `stdio_server`.** It imports numpy,
  and fastembed when embedding is on, after `embed`, whose module-top pins must come
  first. That took 0.16-0.83 s at startup on strixlappy.
- **Every `subprocess` call in `brain_mcp` passes `stdin=`**, `DEVNULL` for all of them
  today. A child must not share the protocol pipe anyway. The background reindex already
  did; ripgrep, the doctor's two git calls and macOS `scutil` did not.
- **`_TOOL_LOCK` keeps tool calls serial.** The vault and embedding code have module
  caches and a lazily built embedder, and were written for one caller per process. It is
  a threading lock because an asyncio one binds to the first loop that contends for it.

At stdin EOF the server exits and drops any call still in flight. Main did the same for
whichever call was in flight at EOF, so a heredoc smoke test that sends `tools/call`
lines may lose the last answers. Use `tools/list`, which still runs on the loop, or hold
stdin open.

`tests/test_mcp_tool_threading.py` asserts that the loop stays responsive during a slow
tool, that calls are serialized, and that the preload runs before stdio. It also scans
the package's AST for any `subprocess` call without `stdin=`. A pipe-level reproduction
needs Windows, so it asserts the properties instead.
