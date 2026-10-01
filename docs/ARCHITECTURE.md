# Architecture

How the Ai-Brain code fits together, and the history behind each design. `CLAUDE.md` has
the short map; the rules each part must keep are in `.claude/rules/`, with their case
histories in `docs/GOTCHAS.md`. Moved out of `CLAUDE.md` on 2026-10-01.

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
    The preload picks the newest checkpoint by mtime, with the name breaking ties, and
    `brain-compact` reads the date from the front of the name: keep the stamp first.
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
    chars (see `.claude/rules/preload.md`) — so the model sees user
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
    ignoring past corrections. Default is now 72 KB (more than `PRELOAD_PARTS` hook parts can
    carry, so the per-part cap usually binds first and `PRELOAD_OVERFLOW` names what did not
    fit), `BUNDLE_SATURATED` warns whenever anything is skipped, and `OVERSIZED_MEMORIES` (info) flags bodies over
    `doctor.MEMORY_BODY_SOFT_LIMIT` so the corpus gets compacted rather than the budget raised
    forever. The subagent path has its own, much smaller `BRAIN_SUBAGENT_BUDGET_KB` —
    doctor sizes that bundle too (`SUBAGENT_BUNDLE_SATURATED`), because on 2026-08-06 the
    corpus outgrew the subagent budget and 3 feedback rules were silently dropped from
    every subagent while the session-budget check reported OK.
  - `subagent_start.py` — injects a *slim* bundle (index + user + feedback, no project
    overview/checkpoint) into every subagent via the SubagentStart event. Claude 5-era models
    delegate heavily, and the SessionStart preload reaches only the main session — without this,
    delegated work runs without the user's behavioral rules. The budget is
    `vault.SUBAGENT_BUDGET_DEFAULT_KB` (56 KB). It has silently saturated more than once, so
    treat a new feedback memory as something that can push the corpus over; `brain doctor`
    reports the current fill (`SUBAGENT_BUNDLE_SATURATED`, `PRELOAD_OVERFLOW`).
    `BRAIN_SUBAGENT_BUDGET_KB` tunes it. The bundle fills feedback before `user/`, so lowering
    it drops user context first; until 2026-09-01 it was the other way round, and the old 12 KB
    default delivered 11 user entries and zero feedback (found 2026-07-30). `BRAIN_SUBAGENT_PRELOAD=0` disables. Verified
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
  `brain-setup.py` and `brain-uninstall.py`, which import it (2026-08-25). Its `merge`/`prune`
  subcommands, from when the retired shell installers ran it as a script, still work. Stdlib only and runnable by a bare system `python3`, because
  the uninstallers may run after the venv is gone. It owns four things that must never diverge
  again: (1) Brain hook groups are **appended** to each event's surviving groups, never assigned
  over them; (2) Brain-owned entries are pruned first, so a re-run is idempotent; (3) an
  unparseable `settings.json` is **refused**, never rewritten; (4) mutations are backed up
  (`settings.json.brain-backup-<ts>`) and written atomically. The ownership predicate
  (`is_brain_command`: `BRAIN_VAULT=`, `brain-launch`, or the repo `hooks/` dir, either slash
  direction) is deliberately shared across the install/uninstall boundary — a narrower predicate
  in the uninstaller strands orphan hooks, a wider one deletes a third-party hook.
  `_assert_block_is_ownable()` fails the install if a template command isn't detectable as ours,
  because such a command could never be pruned and would duplicate on every run. It also sets
  `autoMemoryEnabled: false` and records what the key held before in `.brain-auto-memory.json`
  beside `settings.json`; `prune` restores that value only while the key still reads `false`.

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
  the F2 gotcha). On Windows it additionally generates `<config-dir>\brain-launch.cmd` for the
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
  still lands, because a red test should not cost the user their Brain — but it **exits 4**
  (a refused `settings.json` exits 1, a failed `--with-mcp` registration 5), so a scripted
  install cannot report success over a broken checkout. Those two pull in opposite directions
  and both are required. (3) `run_tests()` drops every inherited `BRAIN_*` variable and
  `CLAUDE_PROJECT_DIR`, so the suite runs against `conftest`'s throwaway vault and setup can
  never write into the user's real memories, nor fail over a green checkout because of a user's
  tuning knob. `--skip-tests` bypasses the step.

- **`brain-uninstall.py`** — the inverse of `brain-setup.py` (the three `uninstall-*`
  shell scripts were retired with their installers, ROADMAP 3G). It prunes Brain-owned hook entries *and* every Brain `permissions.allow` rule
  from `settings.json` — through `brain_settings_merge.py prune`, the same module and therefore
  the same ownership predicate the installers use — deletes the generated wrappers, removes the
  MCP registration, and
  deletes `CLAUDE.md`/the skill only when they carry the managed-by marker. Install and
  uninstall must stay symmetric: until 2026-08-24 the allow rule was written by the installers
  and removed by none of them, leaving a standing unprompted Bash approval for a deleted path.
  Uninstall keeps its own (safer) policy on unparseable settings: leave it alone and report,
  never fail. The shared venv is deleted only when no other config dir references it; the candidates
  are `~/.claude*`, `$CLAUDE_CONFIG_DIR`, and every dir setup recorded in the gitignored
  `.brain-installs.json` at the repo root, because a config dir can be any path.

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

## Why the pi typecheck is required

`npm run typecheck` is not optional garnish — it earned itself on the first run
(2026-08-25) by finding `ctx.sendMessage(...)` in the `/brain recall` and `/brain list`
command handlers. `sendMessage` lives on `ExtensionAPI` (the `pi` argument), not on
`ExtensionCommandContext`, so both paths threw "is not a function" for every by-hand
invocation. Nothing else in the repo could have caught it: the extension is loaded by
pi at runtime, so there is no import-time error and no Python test that reaches it.
