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
    stays in `feedback/`. Bundle fill order is project-feedback → user → global feedback, so a
    tight budget drops global feedback first.
  - Core logic lives in `brain_mcp/vault.py` (search, write, frontmatter, session bundle);
    recall/list payload caps and compact-markdown rendering in `brain_mcp/render.py` (shared by
    both frontends — keep it that way); health checks in `brain_mcp/doctor.py`. Everything reads
    `BRAIN_VAULT` from env and operates on files inside `$BRAIN_VAULT/Brain/`.

- **`hooks/`** — Python scripts wired into Claude Code's hook events via `settings.json`:
  - `session_start.py` — preloads the vault bundle as `additionalContext` so the model sees user
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
    model and it must either fulfill the commitment or recant before ending. "A brain save" means
    a `brain_save`/`brain_checkpoint` MCP tool call **or** a Bash/PowerShell tool_use whose
    command invokes the `brain save`/`brain checkpoint` CLI (`is_cli_save_command`, tolerant of
    paths, `.exe`/`.cmd`, and quoting, but deliberately not matching the phrase inside quoted
    arguments). Disable per-install with `BRAIN_STOP_GATE=0`. Re-entries (payload
    `stop_hook_active=true`) bypass the gate to avoid infinite loops. (2) Audit: append a
    breadcrumb to `Brain/activity.md` with columns `[sig=Y|N sav=Y|N nud=Y|N pro=Y|N too=Y|N sys=Y|N]` —
    save-signal in user message, brain save this turn (either interface), UserPromptSubmit nudge
    enabled, save-promise in assistant message, whether a save interface was *available this
    session* (the `brain` CLI exists in the repo venv, or the MCP server is registered in the
    active config dir's `.claude.json` `mcpServers`), and whether the turn's "user message" was
    *system-generated* (task notification, skill/command expansion, local-command output — such
    text can contain arbitrary phrases, so a `sig=Y` on it says nothing about the user; found
    2026-07-28 when ~9% of rows were notification turns and a skill expansion scored a false
    save-signal). `brain_doctor._check_save_gap` and `_check_promise_gap` read the tail to surface
    signal-without-save and promise-without-save trends, **both skipping `too=N` rows** — a
    save-promise in a session with no save interface is physically unsatisfiable (infra failure,
    not a model bug), so counting it would be a false positive (this guards against the 2026-06-03
    PROMISE_GAP false alarm where the session troubleshooting an unregistered brain promised a save
    the gate then demanded). **`sys=Y` rows are skipped by the save-gap check only**: `pro`
    measures assistant text, which is genuinely model-authored whatever triggered the turn, so the
    promise-gap check (and the Stop-gate itself) still applies on system turns. Legacy rows with
    no `too=`/`sys=` columns still count, for backward compatibility. Promise-gap threshold is 1
    (any miss is a bug); save-gap threshold is 3 in a 30-turn window.
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

- **`setup-mac.sh`** — **DEPRECATED 2026-08-25 (ROADMAP 3G); use `brain-setup.py`.** Still works, still warns on stderr, no longer receives new behaviour — and it has no Python >= 3.11 gate at all, so a stock macOS `/usr/bin/python3` (3.9) breaks the install unless a newer one sorts earlier on PATH. Idempotent bootstrap. Installs brain-mcp into `mcp-server/.venv`, writes the
  global CLAUDE.md and brain skill (with `__BRAIN_CMD__` substituted; the skill frontmatter
  pre-approves the brain CLI via `allowed-tools`), merges one `permissions.allow` rule
  **per agent subcommand** (`Bash(<BRAIN_CMD> recall:*)`, `… save:*`, …) so proactive CLI
  saves never hit permission prompts without pre-approving the whole CLI, and merges the hooks block
  into settings.json **via `brain_settings_merge.py`** — it no longer carries its own copy of that
  logic, and a refused merge exits 3 after printing which steps did land. Takes
  `<claude-config-dir> <vault-path> [--with-mcp]`. **MCP registration is
  opt-in**: only `--with-mcp` runs `claude mcp add`; without it, any existing user-scope `brain`
  registration is *removed* so the CLI-first token saving actually lands.

- **`setup-windows.ps1`** — **DEPRECATED 2026-08-25 (ROADMAP 3G); use `brain-setup.py`**, which writes the same two wrappers without the PS 5.1 encoding hazard below. The Windows counterpart to `setup-mac.sh`. Same arguments (`-WithMcp`
  switch), same idempotency guarantee. Generates two per-install wrappers (Unix-style env prefixes
  don't work on Windows): `<config-dir>\brain-launch.cmd` for hook commands in `settings.json`
  (`<launch.cmd> <hook-name>`, no JSON quote-escaping) and `<config-dir>\brain.cmd` — the CLI
  wrapper substituted for `__BRAIN_CMD__`, which bakes in `BRAIN_VAULT` and forwards args to the
  venv's `brain.exe`. Uses `templates/settings.hooks.win.json` as the template, merged by
  `brain_settings_merge.py` — whose call site needs its own try/catch, because the merge writes
  its refusal diagnosis to stderr and PS 5.1 turns any native stderr into a terminating
  `NativeCommandError`. Python hooks and server/CLI code are unchanged between platforms.

- **`brain-setup.py`** — the interactive cross-platform wizard, and **the installer most
  installs actually run**. Same end state as the shell/PowerShell scripts: venv, non-editable
  install, global CLAUDE.md, brain skill, hooks block, `permissions.allow` rule, `--with-mcp`
  opt-in (the settings.json half now comes from `brain_settings_merge.py`, and a refused merge
  makes `main()` exit 1 after reporting the partial install). Runs interactively by default;
  `--non-interactive --vault P --claude-dir D` for
  scripted use. Because it is a fourth hand-maintained copy of the same logic it is the one
  most likely to be missed — the permission rule reached `setup-mac.sh` on 2026-07-28 and only
  got here on 2026-08-24. `mcp-server/tests/test_installer_parity.py` exists to catch that;
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

- **`setup-linux.sh`** — **DEPRECATED 2026-08-25 (ROADMAP 3G); use `brain-setup.py`.** A fork of `setup-mac.sh` for Debian Trixie / Raspberry Pi OS /
  Ubuntu 22.04. Adds a `find_python()` that rejects anything below 3.11 (Ubuntu 22.04 ships
  3.10, which `pyproject.toml` refuses). Otherwise it should stay byte-for-byte equivalent in
  behaviour; when you touch one, touch both.

- **`brain-uninstall.py`** (the three `uninstall-*` shell scripts are **DEPRECATED 2026-08-25**, ROADMAP 3G) —
  the inverses. Each prunes Brain-owned hook entries *and* every Brain `permissions.allow` rule
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
  harnesses with no hooks). Keep these in sync with `setup-windows.ps1`, the MCP server
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
# On Windows, use the generated wrapper instead: <config-dir>/brain.cmd stats

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

## Gotchas that will bite you

- **Recall's index sync is time-boxed on purpose — don't make it unbounded again
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

- **What gets embedded is a slice, not the raw file — and the recipe is versioned
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

- **Index rows are keyed on a vault-*relative*, forward-slashed path — keep the API boundary
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

- **`_MATRIX_CACHE` is keyed on a `vector_epoch` counter, not just row shape — and never
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

- **`embed_text()` reads each file once.** It needs the raw text for its fallback path
  anyway, so it parses via `vault.Memory.from_text(path, raw_text)` rather than
  `from_file()`, which would re-read the same file from disk — doubled I/O across a
  ~900-file rebuild for nothing. `from_file()` is now a one-line wrapper over `from_text()`;
  keep the split.

- **Lexical-only hits get a reserved slot every 3rd position — don't "simplify" that back to
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

- **`BRAIN_INDEX_SESSION_DAYS` still defaults to 0 (off), even though ranking is fixed.** It
  bounds index growth (checkpoints accrue ~1/session forever; hand-written memories don't),
  and an excluded checkpoint is now *findable* — the reserved slot guarantees that. But it is
  only findable **lexically**: a query that is semantically related without literally
  matching won't reach it at all. Measured benefit today is ~15% of files on a one-time
  indexing cost; the cost is a permanent loss of semantic reach over old checkpoints. Turn it
  on deliberately, for a vault where index size actually hurts.

- **The installers must APPEND hook groups, never assign the event — and must refuse an
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

- **A `project` value is a directory basename and nothing else — build every project
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

- **Checkpoint filenames must be claimed with `O_EXCL`, not composed and hoped for
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

- **Frontmatter is machine-written YAML — build it with `vault._frontmatter()` and write with
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

- **The preload carries the rule, not the case history — `**Why:**` is deferred to recall
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

- **`vault.is_memory_path()` is the ONLY answer to "is this a memory" (unified
  2026-08-24).** There were three, and they disagreed: `iter_indexable_md` applied
  `EXCLUDE_DIRS` + `EXCLUDE_FILES`, `list_memories` applied its own `_setup`/leading-underscore
  filter, and `doctor.NON_MEMORY_NAMES` was a third list that alone knew about `README.md`.
  So `brain list` returned `activity.md` — the 224 KB Stop-hook audit log — and the vault's
  README as memories of type `unknown`, while both were correctly absent from recall and from
  the index. Route every vault enumeration through the predicate; a test fails if any module
  names those filenames literally again.

- **`brain list` is capped too, and its truncation is round-robin across types — don't
  "simplify" that to a slice (2026-08-24).** `list` was the one path through `render.py` with
  no cap, in a module whose entire purpose is bounding payloads: 287 entries / 57 KB by
  default, 916 / 140 KB (~35k tokens) with `--include-sessions`, growing by one checkpoint per
  session forever. But capping it in `list_memories`' path order (feedback, projects,
  references, user) exhausted the budget inside the 224-entry project bucket and dropped
  **every** user and reference memory — losing a whole category is much worse than losing a
  slice of each, and user/feedback are the two the model's behaviour depends on. Selection is
  now round-robin by type so the large bucket absorbs the truncation, and the omission is
  always reported with the filters that would narrow it.

- **Recall/list output is deliberately capped — don't "fix" that by removing the caps
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

- **`brain_recall` could hang the whole session on the embedding-model load (fixed 2026-06-03).**
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

- **`brain-setup.py` could report success while leaving brain unregistered (fixed 2026-06-03).**
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

- **`setup-windows.ps1` runs under Windows PowerShell 5.1, not pwsh 7 — read and write every
  template with an explicit UTF-8 encoding (fixed 2026-08-24).** The shebang says `pwsh` and
  the script uses PS7-era idioms, but the documented invocation is
  `powershell -ExecutionPolicy Bypass -File ...`, and `powershell.exe` *is* 5.1. There
  `Get-Content -Raw` defaults to the ANSI codepage, so it decoded the templates' UTF-8 em
  dashes and ellipses as cp1252 and wrote the mojibake straight back out — 43 corrupted
  sequences in the generated global `CLAUDE.md` and 7 in the brain skill, i.e. the two
  load-bearing behavioural files, produced by the one step whose entire job is to produce
  them. The templates themselves were clean, so nothing upstream showed the damage. Both
  reads now go through `[System.IO.File]::ReadAllText(path, [System.Text.Encoding]::UTF8)`
  and both writes pass an explicit `UTF8Encoding($false)` (no BOM). Same failure class as
  the 2026-07-29 CLI-stdin incident; verify with a mojibake byte-count (`\xc3\xa2\xe2\x82\xac`)
  after any change to that step, not by eyeballing the file.

  The same run also exposed that `$ErrorActionPreference = 'Stop'` turns *any* native stderr
  into a terminating `NativeCommandError`. On a CLI-first install, step 6 runs
  `claude mcp remove brain --scope user`, whose "No MCP server named brain in user scope" is
  the **expected** result when there is nothing to remove — so the idempotent path exited 1
  and setup reported failure after all six steps had already succeeded. That call is wrapped
  in its own try/catch now. Watch for the same trap around any other `& $ClaudeBin` call.

- **Never install brain-mcp editable** (`pip install -e .`). The .pth file generated by setuptools
  doesn't reliably activate at startup, so `import brain_mcp` fails from any cwd other than the
  project root. Use plain `pip install .` (non-editable) — the `setup-mac.sh` script already does
  this. If you "fix" it back to editable, hooks will silently break for anyone launching them from
  a foreign cwd (which Claude Code does).

- **User-scoped MCP servers are not registered by dropping a .mcp.json file.** Claude Code only
  reads `.mcp.json` from the current project dir. User scope lives in `~/.claude-*/.claude.json`
  and must be written with `claude mcp add --scope user`. Do not try to hand-write it.

- **Hooks must set `BRAIN_VAULT` in the command string itself**, because the subprocess inherits
  the parent's env but the parent (Claude Code) doesn't export `BRAIN_VAULT`. On macOS, the
  `settings.hooks.json` template wraps each command as
  `BRAIN_VAULT=<vault> <venv python> <hook>.py`. On Windows, Unix-style env prefixes don't work,
  so `setup-windows.ps1` generates a `brain-launch.cmd` wrapper that sets the env and execs the
  hook — `settings.hooks.win.json` just invokes that wrapper with the hook name as the argument.
  Preserve whichever pattern matches the platform.

- **Never walk up from `__file__` to find the vault.** That used to work when hooks lived inside
  the vault itself; now they live in this repo, which has no relationship to the vault path.
  Always read `BRAIN_VAULT` from env.

- **Keep the vault out of macOS TCC-protected folders** (`~/Documents`, `~/Desktop`,
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

- **The pre-approved CLI invocation is an *unattended* one — keep the file-import
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
  boundary is `BRAIN_AGENT_SURFACE=1`, baked into the approved prefix itself (the generated
  `brain.cmd` sets it on Windows; it leads the `BRAIN_CMD` env prefix on POSIX).
  `cli._enforce_agent_surface` refuses `--file`, `--from-pi` and `--from-cherryd` under it
  and exits 2. An invocation *without* the variable is simply not pre-approved, so it
  prompts like any other command — which is why operators, timers and the pi extension
  (which clears the variable explicitly, since `BRAIN_PI_CMD` may point at the wrapper)
  keep the full CLI. Three things must stay in step or the boundary is decoration: all
  four installers must bake the variable in, `SKILL.md`'s own `allowed-tools` must stay
  narrowed (it is a *second* pre-approval door), and any new option that opens a
  caller-supplied path must join `cli.RESTRICTED_OPTIONS` — `test_agent_surface.py`
  asserts all three.

  The other half of that hole — an injected `--content` planting standing instructions —
  was closed the same day by the trust fence below.

- **Vault content reaches a model fenced, and the fence must stay unclosable from inside
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

- **`Path.resolve()` is not prefix-stable on Windows — normalize both operands before
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
