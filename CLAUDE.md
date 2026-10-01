# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This repo is the **code half** of a two-location memory system for Claude Code and local LLMs. The
other half — the memory **content** — lives in an Obsidian vault at `~/Vaults/Ai-Brain`
and is propagated across machines by Obsidian Sync.

- `~/src/Ai-Brain` (this repo) — hooks, MCP server, templates, setup scripts. Synced via git.
- `~/Vaults/Ai-Brain` — `Brain/user/`, `Brain/feedback/`, `Brain/projects/`, session
  checkpoints, `_index.md`. Synced via Obsidian Sync.

Do not store memory content in this repo. Do not put code in the vault. The split is the whole
point — each side has the sync mechanism that suits it.

## Architecture

The moving parts fit together as follows:

- **`mcp-server/`** — the `brain_mcp` Python package with two thin frontends over one core:
  - **`brain_mcp/cli.py`** → the `brain` console script, the **primary interface**. Claude Code
    (via the brain skill + global CLAUDE.md) and pi run `brain recall|save|list|forget|checkpoint|
    stats|reindex|doctor` through their shell tool. Costs no context tokens until invoked.
  - **`pi/extensions/brain.ts`** → the third frontend: a pi (pi.dev) extension that shells
    out to the same CLI. See below.
  - **`brain_mcp/server.py`** → stdio MCP server exposing the same operations as typed tools
    (`brain_session_start`, `brain_recall`, `brain_save`, `brain_list`, `brain_forget`,
    `brain_checkpoint`, `brain_stats`, `brain_doctor`) for MCP clients: LMStudio, MCP-aware
    Ollama frontends, or Claude Code when setup ran with `--with-mcp`.
  - Every save and checkpoint is stamped with the originating machine
    (`vault.machine_name()`: `BRAIN_MACHINE` override → macOS LocalHostName → hostname).
    Checkpoints carry it in the *filename* (`2026-08-06-124907-joes-macbook-pro-3.md`,
    plus an `_02` disambiguator on a same-second collision) so uncommitted work is
    traceable to the machine it lives on; recall/list render it as `[type @ machine]`.
    Nothing parses checkpoint filenames (consumers sort by mtime) — keep it that way.
  - Feedback can be **project-scoped** (2026-08-06): `brain save feedback --project X` lands in
    `projects/X/feedback/` and preloads only in that project's sessions (and its subagents —
    the slim bundle includes project feedback but not overview/checkpoint). Global feedback
    stays in `feedback/`. Bundle fill order is project-feedback → global feedback → user (reordered 2026-09-01;
    user used to precede global feedback, and under the per-hook output cap that pushed
    every behavioural rule into the catalogue), so a tight budget drops user context first.
  - Core logic lives in `brain_mcp/vault.py` (search, write, frontmatter, session bundle);
    recall/list payload caps and compact-markdown rendering in `brain_mcp/render.py` (shared by
    both frontends — keep it that way); health checks in `brain_mcp/doctor.py`. Everything reads
    `BRAIN_VAULT` from env and operates on files inside `$BRAIN_VAULT/Brain/`.

- **`hooks/`** — Python scripts wired into Claude Code's hook events via `settings.json`:
  - `session_start.py` — preloads the vault bundle as `additionalContext` — delivered in `vault.PRELOAD_PARTS`
    parts, one hook entry each, because Claude Code caps a single hook's output at 10,000
    chars (see the rules below) — so the model sees user
    profile + feedback + project context in its system prompt at every session start. Also runs
    `brain_mcp.doctor.check(project, project_cwd)` and prepends a `## Brain Health` banner for any
    warn/error findings: silent failures like unset `BRAIN_VAULT`, Obsidian Sync conflict files,
    corrupt vector index, accidental editable install, plus these stop-gap checks —
    `STALE_UNCOMMITTED` (project has on-disk changes postdating the last checkpoint; prior session
    likely died before checkpointing — reads `project_cwd` from the hook payload, disable with
    `BRAIN_STALE_CHECK=0`), `PROMISE_GAP` (recent turns promised saves without fulfilling
    them), `BUNDLE_SATURATED` and `OVERSIZED_MEMORIES` (below), plus `INDEX_STALE`
    (vector-index backlog) and `INDEX_RECIPE_STALE` (index built by a superseded
    embedding recipe) — see the recall-sync gotchas. Surfacing these at the top of the
    session forces reconstruction instead of silent context loss.

    Doctor also runs three **corpus-hygiene checks** (added 2026-08-06 after a manual dedup
    audit found ~30 stale/duplicate entries polluting recall): `STUB_SHADOWED_OVERVIEW` (warn —
    a stub `overview.md` coexists with a sibling memory named like the real overview, so the
    stub preloads while the real context never does), `STUB_ONLY_PROJECTS` (info — project dirs
    holding only a 30-day-stale stub, the fingerprint of wrong-cwd session launches), and
    `NEAR_DUPLICATE_MEMORIES` (info — memory pairs with cosine ≥ `BRAIN_DUP_THRESHOLD`,
    default 0.92, computed from vectors already in the embedding index; no model load).
    These catch the mechanical duplication classes only — semantic supersession (entry A
    corrects entry B) still needs a periodic model-driven review pass.

    **The preload budget is a silent-failure surface.** `session_start_bundle` adds user and
    feedback files until `BRAIN_BUNDLE_BUDGET_KB` is exhausted, then *stops* — the overflow is
    reported only as a small "skipped N feedback" note in the banner. On 2026-07-30 the default
    (then 32 KB) was dropping 18 of 22 feedback memories from every session: saved correctly,
    never loaded, so the rules they encoded silently stopped applying and read as the model
    ignoring past corrections. Default is now 72 KB, `BUNDLE_SATURATED` warns whenever anything
    is skipped, and `OVERSIZED_MEMORIES` (info) flags bodies over
    `doctor.MEMORY_BODY_SOFT_LIMIT` so the corpus gets compacted rather than the budget raised
    forever. The subagent path has its own, much smaller `BRAIN_SUBAGENT_BUDGET_KB` —
    doctor sizes that bundle too (`SUBAGENT_BUNDLE_SATURATED`), because on 2026-08-06 the
    corpus outgrew the subagent budget and 3 feedback rules were silently dropped from
    every subagent while the session-budget check reported OK.
  - `subagent_start.py` — injects a *slim* bundle (index + user + feedback, no project
    overview/checkpoint) into every subagent via the SubagentStart event. Claude 5-era models
    delegate heavily, and the SessionStart preload reaches only the main session — without this,
    delegated work runs without the user's behavioral rules. The budget is
    `vault.SUBAGENT_BUDGET_DEFAULT_KB` (56 KB; measured 50.9 KB consumed on 2026-08-24, i.e.
    **91% full** — this has silently saturated twice already, so treat a new feedback memory as
    something that can push the corpus over). `BRAIN_SUBAGENT_BUDGET_KB` tunes it, but note the bundle fills with
    `user/` *before* `feedback/`, so lowering it drops the behavioral rules first: the old 12 KB
    default delivered 11 user entries and zero feedback, defeating the hook's entire purpose
    (found 2026-07-30). `BRAIN_SUBAGENT_PRELOAD=0` disables. Verified
    2026-07-28: SubagentStart fires and injects on Claude Code 2.1.220, hook config picked up
    mid-session, payload carries `agent_id`/`agent_type`.
  - `pre_compact.py` / `session_end.py` — share `_checkpoint.py`, a thin wrapper over
    `brain_mcp.transcript`, which parses the transcript JSONL and writes a structural checkpoint
    to `Brain/projects/<project>/sessions/<timestamp>.md`. No LLM call — the next session's model
    will summarize/integrate when it sees the file. The parsing/rendering lives in the package
    rather than in `hooks/` because the `brain checkpoint --from-cherryd` and
    `--from-pi` CLI paths produce byte-identical checkpoints for harnesses that have no hooks;
    keep them all sharing one renderer.
  - `stop.py` — two jobs. (1) Gate: when the assistant's final message contains a save-promise
    phrase (*"I'll save this to brain"*, *"checkpointing now"*, etc.) and no brain save occurred
    in the turn, emit `{decision: "block", reason: …}` so Claude Code feeds the reason back to the
    model and it must either fulfill the commitment or recant before ending. "A brain save" is
    read from the **save-event record**: every model-facing save surface (the CLI, however it is
    invoked, and the `brain_save`/`brain_checkpoint` MCP tools) calls `vault.record_save_event()`
    after its write lands, and the hook counts events at or after the turn's user-message
    timestamp for the payload's `session_id` (`vault.count_save_events`); a structured
    `brain_save`/`brain_checkpoint` tool_use block in the turn also counts. The hook never
    inspects shell commands — the regex that used to was wrong four times, most recently by
    never learning the `brain-agent.py` launcher (see docs/GOTCHAS.md, *Saves are detected from
    the saver's record*). Disable per-install with `BRAIN_STOP_GATE=0`. Re-entries (payload
    `stop_hook_active=true`) bypass the gate to avoid infinite loops. (2) Audit: append a
    breadcrumb to `Brain/activity.md` with columns
    `[sig=Y|N sav=Y|N nud=Y|N pro=Y|N too=Y|N sys=Y|N re=Y|N]` — save-signal in the user
    message, brain save this turn, nudge enabled, save-promise in the assistant text, a save
    interface available this session, user message system-generated, and re-entry after a gate
    block. `brain_doctor._check_save_gap` / `_check_promise_gap` read the tail; both skip `too=N`
    rows (an unsatisfiable promise is an infra failure, not a model bug), the save-gap check also
    skips `sys=Y` rows, and a `re=Y` row supersedes the row before it for the same
    account+project. Promise-gap threshold is 1; save-gap threshold is 3 in a 30-turn window.
  - `user_prompt_submit.py` — optional soft nudge. If the incoming prompt matches a save-signal
    regex (same patterns as stop.py's audit, kept in `_savesig.py`) and `BRAIN_NUDGE` is not `0`,
    injects a one-line `additionalContext` reminder telling the model to call `brain_save`.
    Stateless, no marker files, no pending-saves dir. Disable per-install with `BRAIN_NUDGE=0` in
    the hook env (e.g., to keep prompts tight for local-model sessions, though hooks only fire
    under Claude Code anyway).
  - `_common.py` / `_checkpoint.py` / `_savesig.py` — shared helpers (`_checkpoint.py` now just
    re-exports from `brain_mcp.transcript`). All read `BRAIN_VAULT` from
    env, never from the filesystem layout. `_savesig.py` is named with a prefix because `_signal`
    is a CPython builtin module that shadows local imports.

- **`pi/extensions/brain.ts`** — the Brain as a [pi](https://pi.dev) extension, with the
  `package.json` manifest at the repo root (pi cannot address a subdirectory of a git repo, so
  the manifest must sit at the top and point inward). pi has no MCP support by design, so this
  is a TypeScript extension shelling out to the `brain` CLI, not a server registration. It
  supplies the three things the `AGENTS.md`-snippet route cannot: a session preload
  (`brain-prep --slim --budget-kb`, injected as a hidden `brain-bundle` message on the first
  turn), five `brain_*` tools, and automatic checkpoints on `session_before_compact` (PreCompact
  parity — pi hands us `reason: threshold|overflow|manual` rather than cherryd's guess from a
  token count), on a settled-turn cadence, and on `session_shutdown`. Rules that matter:
  - **It renders nothing.** Checkpoint bodies come from `brain checkpoint --from-pi`, which
    parses pi's session JSONL in `brain_mcp.transcript`. cherryd rendering its own checkpoints
    in another repo needed a commit to regain byte parity down to a trailing newline; one
    renderer is how that stays fixed.
  - **Dedup lives in the shared state file** (`Brain/.state/harness-checkpoints.json`), keyed
    by the session's leaf entry id, so a cadence checkpoint immediately followed by a shutdown
    checkpoint writes one file, not two.
  - **Automatic checkpoints never go through tool dispatch** — autosave is the operator's
    policy, not the model asking, so it must never raise an approval prompt.
  - **Behavioural guidance is read from `templates/AGENTS-brain.md`** at load, with the CLI
    syntax block and the "no automatic preload" paragraph stripped, and is skipped entirely
    when that snippet is already loaded as a context file. Two mechanisms, one source of truth.
  - Configuration is environment-only (pi has no per-extension settings block); `PI-SETUP.md`
    holds the table. `BRAIN_CMD` is only honoured when it is a bare path — in the Claude Code
    templates it is a shell string, and `pi.exec` spawns without a shell.

- **`templates/`**:
  - `global-CLAUDE.md` — the load-bearing proactive-memory directives. Copied to
    `~/.claude-*/CLAUDE.md` by setup with `__BRAIN_VAULT__` and `__BRAIN_CMD__` (the full CLI
    invocation for the machine) substituted. This is what makes the model save/recall/checkpoint
    automatically instead of waiting for `/brain` commands.
  - `settings.hooks.json` — the hooks block merged into `~/.claude-*/settings.json`. Each command
    is wrapped with `BRAIN_VAULT=<vault> <venv python> <repo hook>.py` so the env is set at launch.
  - `skills/brain/SKILL.md` — the CLI syntax reference and `/brain save|recall|checkpoint|forget|
    list` handler. Rendered with `__BRAIN_CMD__` substituted. The proactive triggers live in
    global-CLAUDE.md; the skill holds the how.
  - `AGENTS-brain.md` — the Brain snippet for AGENTS.md-style agents (pi). Manually rendered by
    the user per `PI-SETUP.md` (setup scripts don't know about pi installs).

- **`brain_settings_merge.py`** (repo root) — the ONE settings.json merge/prune, shared by
  all four installers and all four uninstallers (2026-08-25). `brain-setup.py` and
  `brain-uninstall.py` import it; the three shell/PowerShell scripts invoke it as a script
  (`merge`/`prune` subcommands). Stdlib only and runnable by a bare system `python3`, because
  the uninstallers may run after the venv is gone. It owns four things that must never diverge
  again: (1) Brain hook groups are **appended** to each event's surviving groups, never assigned
  over them; (2) Brain-owned entries are pruned first, so a re-run is idempotent; (3) an
  unparseable `settings.json` is **refused**, never rewritten; (4) mutations are backed up
  (`settings.json.brain-backup-<ts>`) and written atomically. The ownership predicate
  (`is_brain_command`: `BRAIN_VAULT=`, `brain-launch`, or the repo `hooks/` dir, either slash
  direction) is deliberately shared across the install/uninstall boundary — a narrower predicate
  in the uninstaller strands orphan hooks, a wider one deletes a third-party hook.
  `_assert_block_is_ownable()` fails the install if a template command isn't detectable as ours,
  because such a command could never be pruned and would duplicate on every run.

- **`brain-setup.py`** — **THE installer, on every platform** (ROADMAP 3G retired
  `setup-mac.sh`, `setup-linux.sh` and `setup-windows.ps1` on 2026-08-25; do not add a
  platform script back — see 3G for what four of them cost). It creates the venv, installs
  brain-mcp non-editable, writes the global CLAUDE.md and brain skill (with `__BRAIN_CMD__`
  substituted; the skill frontmatter pre-approves the brain CLI via `allowed-tools`), merges
  one `permissions.allow` rule **per agent subcommand** (`Bash(<BRAIN_CMD> recall:*)`,
  `… save:*`, …) so proactive CLI saves never hit permission prompts without pre-approving the
  whole CLI, and merges the hooks block into settings.json **via `brain_settings_merge.py`** —
  a refused merge makes `main()` exit 1 after reporting the partial install. Runs interactively
  by default; `--non-interactive --vault P --claude-dir D` for scripted use. **MCP registration
  is opt-in**: only `--with-mcp` runs `claude mcp add`; without it, any existing user-scope
  `brain` registration is *removed* so the CLI-first token saving actually lands.

  The model-facing command is the same on every platform: `"<venv python>" "<config-dir>/brain-agent.py"`,
  a generated stdlib-only launcher that sets `BRAIN_VAULT` and `BRAIN_AGENT_SURFACE=1` in
  `os.environ` and calls `brain_mcp.cli.main()` — never a `.cmd` and never an env-prefix (see
  the F2 gotcha). On Windows it additionally generates `<config-dir>rain-launch.cmd` for the
  HOOK commands in `settings.json` (`<launch.cmd> <hook-name> [args]`, fixed text from the
  template, never model-chosen) and merges `templates/settings.hooks.win.json` instead of
  `settings.hooks.json`. Hooks and server/CLI code are identical across platforms.

  It runs on **any** Python 3 (`from __future__ import annotations`, no `match`, stdlib only —
  verified on macOS 26.5.1's stock 3.9.6); `find_python3` then locates a 3.11+ *for the venv*,
  preferring version-suffixed binaries because a stock `/usr/bin/python3` is 3.9 and
  `pyproject.toml` refuses it. That property is what made retiring the shell installers safe,
  so keep it: no 3.10+ syntax in this file.
  `mcp-server/tests/test_installer_parity.py` guards the invariants;
  `mcp-server/tests/test_settings_merge.py` tests the shared merge's actual behaviour, which no
  amount of text parity can.

  **It also installs the `dev` extra and runs the test suite as its last shared step
  (2026-08-25).** `pytest` is declared in `pyproject.toml` as an optional dependency and no
  installer installed it, so the venv every install produces could not run the suite without a
  hand-typed `pip install pytest` — and a suite nobody can run by default is a suite nobody
  runs. The bill came due the same day: a case-fold assertion in
  `test_project_path_safety.py` demanded Windows-only `os.path.normcase` behaviour
  unconditionally, so the suite was **red on every macOS and Linux checkout from the moment it
  landed**, straight through a code-review-remediation cycle, unnoticed.

  Three properties are load-bearing, and `test_installer_parity.py` asserts each. (1) The extra
  is installed **separately and non-fatally** — a machine with the real dependencies cached but
  not pytest must not lose its memory system over a testing convenience; a missing pytest
  degrades to a reported skip. (2) A failing suite **does not abort the install** — the wiring
  still lands, because a red test should not cost the user their Brain — but it **exits 4**, the
  same contract a refused `settings.json` already has, so a scripted install cannot report
  success over a broken checkout. Those two pull in opposite directions and both are required.
  (3) `run_tests()` drops any inherited `BRAIN_VAULT`, so the suite runs against `conftest`'s
  throwaway vault and setup can never write into the user's real memories. `--skip-tests`
  bypasses the step.

  The other three installers do **not** do this yet — a deliberate, known gap, not an
  oversight. If you port it, extend the parametrize in `test_installer_parity.py` rather than
  adding a second copy of the check.

- **`brain-uninstall.py`** — the inverse of `brain-setup.py` (the three `uninstall-*`
  shell scripts were retired with their installers, ROADMAP 3G). It prunes Brain-owned hook entries *and* every Brain `permissions.allow` rule
  from `settings.json` — through `brain_settings_merge.py prune`, the same module and therefore
  the same ownership predicate the installers use — deletes the generated wrappers, removes the
  MCP registration, and
  deletes `CLAUDE.md`/the skill only when they carry the managed-by marker. Install and
  uninstall must stay symmetric: until 2026-08-24 the allow rule was written by the installers
  and removed by none of them, leaving a standing unprompted Bash approval for a deleted path.
  Uninstall keeps its own (safer) policy on unparseable settings: leave it alone and report,
  never fail.

- **`brain-compact`** (`brain_mcp/compact.py`) — rolls old session checkpoints into
  `sessions/daily/` (7-30 days), then `sessions/weekly/` (30-365), then
  `Brain/archive/…` (365+). All transforms are idempotent and merge by source filename.
  This is the mechanism that bounds checkpoint growth, and nothing runs it automatically —
  on 2026-08-24 the vault held 629 checkpoints, 69% of all files. `--dry-run` reports
  without writing; `--project X` scopes it. The layout is load-bearing: the session bundle
  globs `sessions/*.md` non-recursively, so anything moved into a subdirectory becomes
  invisible to the preload, which is exactly the intent.

- **`WINDOWS-SETUP.md`, `LMSTUDIO-SETUP.md`, `PI-SETUP.md`, `LOCAL-HARNESS-SETUP.md`** —
  user-facing install guides for the Windows bring-up, the LMStudio MCP registration, the pi
  (pi.dev) CLI wiring, and llama.cpp/cherryd (bundle sizing plus timer-driven checkpoints for
  harnesses with no hooks). Keep these in sync with `brain-setup.py`, the MCP server
  command/env contract, the `brain` CLI surface, and `brain_mcp/transcript.py` respectively.

## Common commands

```bash
# Re-install into a Claude Code config dir (idempotent), every platform.
# The config dir can be any path. Single-account users typically use ~/.claude;
# multi-account users pick their own names (e.g. ~/.claude-personal, ~/.claude-work).
# This is THE installer — the setup-mac/linux/windows scripts are deprecated (ROADMAP 3G).
python3 ~/src/Ai-Brain/brain-setup.py --non-interactive \
    --vault ~/Vaults/Ai-Brain \
    --claude-dir ~/.claude-personal --claude-dir ~/.claude-work

# ...or run it with no arguments for the interactive wizard.
python3 ~/src/Ai-Brain/brain-setup.py

# Exercise the brain CLI directly (the primary interface; same caps/rendering as MCP)
BRAIN_VAULT=~/Vaults/Ai-Brain ~/src/Ai-Brain/mcp-server/.venv/bin/brain stats
BRAIN_VAULT=~/Vaults/Ai-Brain ~/src/Ai-Brain/mcp-server/.venv/bin/brain recall lmstudio
# Or the pre-approved launcher every platform now uses: "<venv python>" "<config-dir>/brain-agent.py" stats

# Verify the MCP server is registered and connected — only meaningful after a --with-mcp
# install (omit CLAUDE_CONFIG_DIR for the default ~/.claude)
CLAUDE_CONFIG_DIR=~/.claude-personal claude mcp list

# Smoke-test the MCP server over stdio (from any cwd)
BRAIN_VAULT=~/Vaults/Ai-Brain ~/src/Ai-Brain/mcp-server/.venv/bin/python -m brain_mcp <<'EOF'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
EOF

# Dry-run the session_start hook against a fake payload
echo '{"cwd":"/tmp/test","hook_event_name":"SessionStart","source":"startup"}' | \
  BRAIN_VAULT=~/Vaults/Ai-Brain \
  ~/src/Ai-Brain/mcp-server/.venv/bin/python ~/src/Ai-Brain/hooks/session_start.py

# Dump the session-start bundle as markdown (useful for non-tool-calling models)
BRAIN_VAULT=~/Vaults/Ai-Brain \
  ~/src/Ai-Brain/mcp-server/.venv/bin/brain-prep --project MyProject

# Exercise the pi extension end to end (loads, preloads, checkpoints on exit)
BRAIN_VAULT=~/Vaults/Ai-Brain pi -e ~/src/Ai-Brain -p "Say only: brain ok"

# Drain the vector-index backlog (recall only syncs a 5s slice per call)
BRAIN_VAULT=~/Vaults/Ai-Brain ~/src/Ai-Brain/mcp-server/.venv/bin/brain reindex

# Roll old checkpoints into daily/weekly/archive buckets. Nothing runs this
# automatically, and checkpoints are ~69% of the vault's files — check first.
BRAIN_VAULT=~/Vaults/Ai-Brain ~/src/Ai-Brain/mcp-server/.venv/bin/brain-compact --dry-run
BRAIN_VAULT=~/Vaults/Ai-Brain ~/src/Ai-Brain/mcp-server/.venv/bin/brain-compact

# Health check — run anytime, especially when the Brain feels stale or broken
BRAIN_VAULT=~/Vaults/Ai-Brain \
  ~/src/Ai-Brain/mcp-server/.venv/bin/brain-doctor --project MyProject
```

## Rules that will bite you

Each rule below has a case history in `docs/GOTCHAS.md` under the same lead phrase — what
happened, what was measured, why the fix has the shape it has. **Read that entry before
changing anything a rule guards.** The rule is the summary; every one was paid for with a real
incident.

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
- **Installers APPEND hook groups (prune-first) and refuse an unparseable `settings.json`.**
  Everything goes through `brain_settings_merge.py` — never re-inline it. Missing or
  whitespace-only means `{}`; anything else unparseable is a hard stop with a nonzero exit and a
  summary of what did land. Writes are backup + same-dir temp + `os.replace`, only when content
  changes.
- **A `project` value is a directory basename and nothing else.** `vault.validate_project_name()`
  is the predicate (a blacklist, not a whitelist), `vault.project_dir()` the only path builder,
  `vault.projects_root()` the only enumerator. `project_basename()` returns None and never raises;
  doctor downgrades to `PROJECT_NAME_INVALID`; the CLI exits 2; the MCP server returns an error
  result. A test fails on any direct join under `"projects"`.
- **Checkpoint filenames are claimed with `O_EXCL`** (`_reserve_checkpoint_path`), to the second,
  with a zero-padded `_02` disambiguator (underscore so it sorts after `.`). The reservation is a
  real empty `.md`, unlinked on a failed write. `_atomic_write` temp names carry pid + counter and
  never end in `.md`.
- **Frontmatter is built with `vault._frontmatter()` and written with `vault._atomic_write()`**,
  never f-strings + `write_text`. The CLI forces UTF-8 on stdio; `--project` filtering uses
  `vault.path_in_project()`. Doctor flags `MALFORMED_FRONTMATTER`.
- **The preload carries the rule; `**Why:**` is deferred to recall.** `vault.preload_text()`
  replaces it with a marker in elastic sections only — lossless, nothing on disk changes,
  `BRAIN_PRELOAD_DEFER_WHY=0` restores. Project-scoping is not a further lever (measured
  2026-08-25).
- **`vault.is_memory_path()` is the only answer to "is this a memory".** Route every enumeration
  through it; a test fails on literal bookkeeping filenames elsewhere.
- **`brain list` is capped, round-robin across types.** Don't simplify it to a slice — path order
  dropped whole categories. Omissions are reported with the filters that would narrow them.
- **Recall/list output is capped; don't remove the caps.** `top_k` defaults to 3 and clamps at
  `BRAIN_RECALL_MAX_K` (10); `BRAIN_RECALL_PREVIEW_CHARS`, `BRAIN_RECALL_MAX_BODY_CHARS` (6000),
  `BRAIN_RECALL_MAX_TOTAL_CHARS` (20000); sessions excluded unless asked. Both frontends route
  through `render.py`.
- **`embed.py`'s module-top block pins the fastembed cache and HF timeouts before any fastembed
  import.** Machine-local cache, `HF_HUB_OFFLINE` when the model is cached, bounded timeouts,
  ripgrep fallback. Knobs: `BRAIN_EMBED_OFFLINE=0`, `BRAIN_EMBED_CACHE`, `BRAIN_EMBED=0`. Slow
  recall: check the cache dir and the offline flag first.
- **`brain-setup.py` verifies MCP registration by reading the target dir's `.claude.json`
  `mcpServers`**, not `claude mcp list`. Preload present but tools absent means the wrong config
  dir; the default dir's file is `~/.claude.json`.
- **Every file read/write in the installer passes `encoding="utf-8"`** (proved by an AST-parsed
  parity test; `write_windows_launch_cmd` is the deliberate exception). Verify template output
  with a mojibake byte count, never by eye.
- **Never install brain-mcp editable.** Plain `pip install .`; the `.pth` route breaks hooks from
  foreign cwds.
- **User-scoped MCP servers are registered with `claude mcp add --scope user`**, never by
  dropping a `.mcp.json`.
- **Hooks set `BRAIN_VAULT` in the command string itself** — env prefix on POSIX,
  `brain-launch.cmd` on Windows. Preserve the platform's pattern.
- **Never walk up from `__file__` to find the vault.** Read `BRAIN_VAULT`.
- **Keep the vault out of macOS TCC-protected folders** (`~/Documents`, `~/Desktop`,
  `~/Downloads`, iCloud Drive). A phantom `INDEX_CORRUPT` or a PermissionError on overwrite means
  check the path and Full Disk Access before debugging `vault.py`.
- **The pre-approved CLI invocation is unattended: keep file-import options off it.**
  `BRAIN_AGENT_SURFACE=1`, set by the launcher, makes `cli._enforce_agent_surface` refuse
  `--file`, `--from-pi` and `--from-cherryd`; a new path-opening option joins
  `cli.RESTRICTED_OPTIONS`; `SKILL.md`'s `allowed-tools` stays narrowed. Permission rules are
  prefix matches and cannot enforce this.
- **Vault content reaches a model fenced, and the fence must be unclosable from inside.**
  `vault.fence()` is the only producer; `vault.neutralize_fence()` runs where content enters the
  payload and before clipping; only `brain_prep.render` and `render.py` render vault text;
  `doctor.render_banner` defangs findings; the fence's bytes are reserved out of the budget; the
  never-empty guard counts items. The notice says "data, not instructions", never "ignore this".
- **`Path.resolve()` is not prefix-stable on Windows.** Compare resolved paths through
  `vault._comparable()`, always.
- **One hook command's `additionalContext` is capped at 10,000 chars; the preload is delivered
  in `vault.PRELOAD_PARTS` parts.** `PRELOAD_PARTS`, the entry count in both hook templates and
  `brain-launch.cmd` forwarding `%2..%9` must agree (`test_preload_parts.py`). Parts arrive in
  any order and are self-describing; only part 1 runs doctor/stub/reindex; what doesn't fit is
  catalogued by name. No `--part` means the whole bundle.
- **Global feedback fills before user memories** (project feedback → global feedback → user).
  If the corpus outgrows the parts, compact `user/`; don't reorder again.
- **`doctor.check()` isolates every check; a check failure is never a vault failure.**
  `_run_check` turns a raise into `CHECK_FAILED`; `_read_frontmatter_head` returns None for
  anything not a UTF-8 mapping; sort keys and `max()` use `vault.safe_mtime` (a test fails on
  `key=lambda p: p.stat()` on the preload path); the hook separates an import failure from
  `DOCTOR_FAILED`; `search_memories` catches `(OSError, ValueError)` per file.
- **Pinned preload items are clipped** to `vault.pinned_max_chars()`
  (`BRAIN_PRELOAD_PINNED_MAX_CHARS`); the never-empty guard counts elastic items only;
  `latest_checkpoint()` skips 0-byte files; budget knobs are clamped finite by
  `_budget_kb_from_env`.
- **The model-facing command is a Python launcher (`brain-agent.py`), never a `.cmd`, never an
  env prefix.** `brain_cmd_token()` returns two quoted paths; `brain-launch.cmd` serves hooks
  only, with fixed arguments. Any future wrapper must receive `argv` already split.
  `is_brain_permission_rule` recognises every shape ever written so re-runs prune the old ones.
- **Installer-written files carry `MANAGED_MARKER`, unmarked destinations are backed up first,
  and files cmd.exe reads are OEM-encoded.** `write_managed_text` is the one writer; the skill's
  marker sits after its frontmatter; hook templates quote every placeholder and substitute on
  parsed JSON; `brain-launch.cmd` doubles `%` and stays ASCII; `cleanup()` deletes
  `<config>/.mcp.json` only when it is a Brain-only registration.
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
- **Hooks read their payload through `_common.read_payload()` and nothing else** (it pins UTF-8
  first); hooks never create the vault; `append_activity` rotates `activity.md` at 2,000 lines.
- **`stop.py` reads the transcript from the end and learns about saves from the save-event
  record, never from shell commands.** Every model-facing save surface calls
  `vault.record_save_event()` after its write lands; the hook counts events at or after the
  turn's user-message timestamp for the payload's `session_id`, plus structured
  `brain_save`/`brain_checkpoint` tool_use blocks. `test_save_events.py` fails the build if
  `stop.py` looks at a `"Bash"` command again. Promise patterns require a Brain noun;
  emphasis-strip regexes are bounded; `re=Y` rows supersede the row before them;
  `transcript.SYSTEM_TURN_PREFIXES` is the one list of system-turn markers.
- **The pi extension clears `BRAIN_AGENT_SURFACE` per spawn** (`execFile`, `shell:false`,
  explicit env), never in `process.env`. `BRAIN_PI_CMD` must be the venv executable.
- **A save that replaces a memory archives the previous version** under
  `Brain/archive/versions/` (`VERSION_KEEP`=5, monotonic names) and reports it; a byte-identical
  re-save writes nothing; `slugify` transliterates via NFKD and hashes when no ASCII survives;
  caller frontmatter is honoured only as a mapping; `forget_memory` requires a `.md` that
  `is_memory_path` accepts.
- **brain-compact buckets and ages by the date in the filename, never mtime**, merges by
  `<!-- brain-compact source: … -->` sections, reclaims fully-absorbed sources, and merges into
  existing archives. Test fixtures use today-relative stamps.

## Testing

```bash
# From mcp-server/ (pytest config lives in pyproject.toml)
.venv/bin/python -m pytest -q
# Windows: mcp-server\.venv\Scripts\python.exe -m pytest -q

# The pi extension is TypeScript and pytest cannot see it. From the repo root:
npm install        # once; node_modules is gitignored, package-lock.json is not
npm run typecheck  # tsc --noEmit over pi/extensions/**
```

`brain-setup.py` installs the `dev` extra and runs the suite itself as its last shared step, so
a normal install both provides pytest and tells you whether the checkout is green — see the
`brain-setup.py` bullet above for why, and for the exit-4-but-don't-abort contract. The
shell/PowerShell installers do neither, so on a venv built by those, install pytest by hand
(`.venv/bin/pip install "pytest>=8,<9"`) before the command above will work.

`npm run typecheck` is not optional garnish — it earned itself on the first run
(2026-08-25) by finding `ctx.sendMessage(...)` in the `/brain recall` and `/brain list`
command handlers. `sendMessage` lives on `ExtensionAPI` (the `pi` argument), not on
`ExtensionCommandContext`, so both paths threw "is not a function" for every by-hand
invocation. Nothing else in the repo could have caught it: the extension is loaded by
pi at runtime, so there is no import-time error and no Python test that reaches it.

The suite runs against the **source tree**, not the installed copy — `pythonpath` in
`[tool.pytest.ini_options]` puts `mcp-server/` and `hooks/` first. That is load-bearing: the
package is installed non-editable, so without it a run would silently grade whatever was last
`pip install`ed. Tests never touch the real vault; `conftest.py`'s `vault_dir` fixture builds a
throwaway one and points `BRAIN_VAULT` at it.

What the suite is *for*: this repo duplicates every concern across parallel sites — four
installers, two frontends, two hook templates — and every bug cluster so far has been a fix
landing at one of N sites. So the highest-value tests are the **invariant** ones, which assert a
property across all sites at once rather than exercising one path:

- `test_installer_parity.py` — every installer writes the permission rule, no installer installs
  editable, both hook templates wire the same events, every PowerShell native call is guarded.
- `test_doctor_index_locks.py::test_every_index_connection_sets_a_busy_timeout` — no
  `sqlite3.connect` may keep sqlite's 5s default.

When you fix something, ask what the *class* of bug is and assert that, not the instance.

Manual verification still matters for anything crossing a process or a platform boundary — a
test cannot run four operating systems. After a non-trivial change:

1. Re-run `brain-setup.py` for both Claude config dirs (it self-tests as its last step).
2. Sanity-check `BRAIN_VAULT=... .venv/bin/python -c "from brain_mcp import vault, server"` from
   `/tmp` (catches editable-install regressions).
3. Exercise the CLI: `BRAIN_VAULT=... .venv/bin/brain recall <something>` and `... brain stats`.
4. Open a fresh Claude Code session in a real project and confirm the brain context is preloaded
   and `/brain list` works (with a `--with-mcp` install, also confirm the `brain_*` tools appear).
5. Say *"I prefer X over Y"* and confirm a new file appears in `~/Vaults/Ai-Brain/Brain/user/`.

## Memory system notes

The Brain is also available to Claude while you work on this codebase. Proactive
save/recall/checkpoint rules are in `templates/global-CLAUDE.md` (which is installed as your
`~/.claude-*/CLAUDE.md`). If the model feels sluggish about saving or recalling, that template is
the single biggest tunable — tighten the triggers there and re-run setup.
