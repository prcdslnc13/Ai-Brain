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

One core package, three frontends over it, a set of Claude Code hooks, and one installer.
`docs/ARCHITECTURE.md` describes each part and the history behind it; read a component's
section there before changing it.

- **`mcp-server/brain_mcp/`**, the core: `vault.py` (search, write, frontmatter, the session
  bundle), `embed.py` (the machine-local sqlite vector index), `render.py` (recall/list caps and
  rendering, shared by every frontend), `doctor.py` (health checks), `transcript.py`
  (checkpoint parsing and rendering, shared by the hooks and the hookless harnesses), and
  `compact.py` (`brain-compact`). Everything reads `BRAIN_VAULT` and works inside
  `$BRAIN_VAULT/Brain/`.
- **Three frontends over that core:** `cli.py` (the `brain` command, the primary interface, run
  through each harness's shell tool), `server.py` (a stdio MCP server for LM Studio and other MCP
  clients, or Claude Code with `--with-mcp`), and `pi/extensions/brain.ts` (the pi extension,
  which shells out to the CLI). All three are in regular use: simplify by sharing code between
  them, never by dropping one.
- **`hooks/`**, wired into Claude Code by `settings.json`: `session_start.py` preloads the bundle
  in `vault.PRELOAD_PARTS` parts and prepends doctor's `## Brain Health` banner;
  `subagent_start.py` injects a slim bundle into subagents; `pre_compact.py` and
  `session_end.py` write structural checkpoints; `stop.py` blocks a turn that promised a save
  it never made and appends the `activity.md` audit row; `user_prompt_submit.py` nudges on save
  signals.
- **`templates/`**: `global-CLAUDE.md` (the proactive-memory directives installed as each config
  dir's `CLAUDE.md`, and the biggest behavioural tunable), the two hook templates, the `brain`
  skill, and `AGENTS-brain.md` for pi.
- **Install:** `brain-setup.py` is the only installer, on every platform (ROADMAP 3G retired the
  platform scripts; do not add one back). `brain-uninstall.py` is its inverse, and
  `brain_settings_merge.py` is the one `settings.json` merge both use. `brain-setup.py` must run
  on a stock Python 3.9, so no 3.10+ syntax in it.
- **Guides:** `WINDOWS-SETUP.md`, `LMSTUDIO-SETUP.md`, `PI-SETUP.md` and
  `LOCAL-HARNESS-SETUP.md` are for users. Keep them in sync with `brain-setup.py`, the MCP
  server's env contract, the `brain` CLI, and `transcript.py` respectively.

## Common commands

```bash
# Re-install into a Claude Code config dir (idempotent), every platform.
# The config dir can be any path. Single-account users typically use ~/.claude;
# multi-account users pick their own names (e.g. ~/.claude-personal, ~/.claude-work).
# This is THE installer — the setup-mac/linux/windows scripts are retired (ROADMAP 3G).
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

The rules live in `.claude/rules/`, one file per area. Each is scoped with `paths:`, so it loads
when you read a file it guards:

| File | Guards |
|---|---|
| `embedding-index.md` | `embed.py`, recall search, the sqlite index |
| `vault-writes.md` | memory and checkpoint writes, project names, `brain-compact` |
| `preload.md` | the session bundle, its budgets and parts, the trust fence, doctor |
| `installers.md` | `brain-setup.py`, `brain-uninstall.py`, the settings merge, the templates |
| `hooks-and-frontends.md` | the hooks, the CLI agent surface, the MCP server, the pi extension |

Each rule has a case history in `docs/GOTCHAS.md` under the same lead phrase. Read that entry
before changing anything a rule guards. These three apply everywhere:

- **Never install brain-mcp editable.** Plain `pip install .`; the `.pth` route breaks hooks from
  foreign cwds.
- **Never walk up from `__file__` to find the vault.** Read `BRAIN_VAULT`.
- **Keep the vault out of macOS TCC-protected folders** (`~/Documents`, `~/Desktop`,
  `~/Downloads`, iCloud Drive). A phantom `INDEX_CORRUPT` or a PermissionError on overwrite means
  check the path and Full Disk Access before debugging `vault.py`.

## Testing

```bash
# From mcp-server/ (pytest config lives in pyproject.toml)
.venv/bin/python -m pytest -q
# Windows: mcp-server\.venv\Scripts\python.exe -m pytest -q

# The pi extension is TypeScript and pytest cannot see it. From the repo root:
npm install        # once; node_modules is gitignored, package-lock.json is not
npm run typecheck  # tsc --noEmit over pi/extensions/**
```

`npm run typecheck` is not optional: pi loads the extension only at runtime, so nothing else in
the repo catches a type error in it. Its first run (2026-08-25) found two command handlers
calling a method that does not exist on their context object.

`brain-setup.py` installs the `dev` extra and runs this suite as its last step. A failing suite
still installs but exits 4; `docs/ARCHITECTURE.md` has the reasons.

The suite runs against the **source tree**, not the installed copy — `pythonpath` in
`[tool.pytest.ini_options]` puts `mcp-server/` and `hooks/` first. That is load-bearing: the
package is installed non-editable, so without it a run would silently grade whatever was last
`pip install`ed. Tests never touch the real vault; `conftest.py`'s `vault_dir` fixture builds a
throwaway one and points `BRAIN_VAULT` at it, and an autouse fixture clears every inherited
`BRAIN_*` variable (and `CLAUDE_PROJECT_DIR`) first, so a run from a Claude Code session, whose
`settings.json` `env` block reaches the shell, grades the code and not the user's knobs.

What the suite is *for*: this repo duplicates every concern across parallel sites — three
frontends, two hook templates, an installer and its uninstaller — and every bug cluster so far
has been a fix landing at one of N sites. So the highest-value tests are the **invariant** ones, which assert a
property across all sites at once rather than exercising one path:

- `test_installer_parity.py` — the installer routes through the shared merge, never installs
  editable, and names an encoding for every file; both hook templates wire the same events;
  the retired platform scripts stay gone.
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
