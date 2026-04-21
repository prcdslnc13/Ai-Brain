# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This repo is the **code half** of a two-location memory system for Claude Code and local LLMs. The
other half — the memory **content** — lives in an Obsidian vault at `~/Documents/Vaults/Ai-Brain`
and is propagated across machines by Obsidian Sync.

- `~/src/Ai-Brain` (this repo) — hooks, MCP server, templates, setup scripts. Synced via git.
- `~/Documents/Vaults/Ai-Brain` — `Brain/user/`, `Brain/feedback/`, `Brain/projects/`, session
  checkpoints, `_index.md`. Synced via Obsidian Sync.

Do not store memory content in this repo. Do not put code in the vault. The split is the whole
point — each side has the sync mechanism that suits it.

## Architecture

The moving parts fit together as follows:

- **`mcp-server/`** — a Python stdio MCP server (`brain_mcp` package) that exposes the vault as
  typed tools: `brain_session_start`, `brain_recall`, `brain_save`, `brain_list`, `brain_forget`,
  `brain_checkpoint`, `brain_stats`, `brain_doctor`. Claude Code, LMStudio, and any MCP-aware
  Ollama frontend all connect to the *same* server instance on a given machine. The server reads
  `BRAIN_VAULT` from env and operates on files inside `$BRAIN_VAULT/Brain/`. Core logic lives in
  `brain_mcp/vault.py` (search, write, frontmatter, session bundle); health checks in
  `brain_mcp/doctor.py`; MCP tool shims in `brain_mcp/server.py`.

- **`hooks/`** — Python scripts wired into Claude Code's hook events via `settings.json`:
  - `session_start.py` — preloads the vault bundle as `additionalContext` so the model sees user
    profile + feedback + project context in its system prompt at every session start. Also runs
    `brain_mcp.doctor.check()` and prepends a `## Brain Health` banner for any warn/error findings
    (silent failures like unset `BRAIN_VAULT`, Obsidian Sync conflict files, corrupt vector index,
    accidental editable install) so the user sees them at the top of the session instead of
    experiencing them as unexplained forgetfulness.
  - `pre_compact.py` / `session_end.py` — share `_checkpoint.py`, which parses the transcript JSONL
    and writes a structural checkpoint to `Brain/projects/<project>/sessions/<timestamp>.md`. No
    LLM call — the next session's model will summarize/integrate when it sees the file.
  - `stop.py` — appends an audited one-line breadcrumb to `Brain/activity.md` after every turn.
    Each line ends with `[sig=Y|N sav=Y|N nud=Y|N]` columns: whether the user message matched a
    save-signal pattern, whether the assistant called `brain_save`/`brain_checkpoint` this turn,
    and whether the UserPromptSubmit nudge was enabled. `brain_doctor._check_save_gap` reads these
    columns and WARNs when recent turns show signal-without-save — that's the feedback loop that
    tells you whether the proactive-save directives in `templates/global-CLAUDE.md` are actually
    firing.
  - `user_prompt_submit.py` — optional soft nudge. If the incoming prompt matches a save-signal
    regex (same patterns as stop.py's audit, kept in `_savesig.py`) and `BRAIN_NUDGE` is not `0`,
    injects a one-line `additionalContext` reminder telling the model to call `brain_save`.
    Stateless, no marker files, no pending-saves dir. Disable per-install with `BRAIN_NUDGE=0` in
    the hook env (e.g., to keep prompts tight for local-model sessions, though hooks only fire
    under Claude Code anyway).
  - `_common.py` / `_checkpoint.py` / `_savesig.py` — shared helpers. All read `BRAIN_VAULT` from
    env, never from the filesystem layout. `_savesig.py` is named with a prefix because `_signal`
    is a CPython builtin module that shadows local imports.

- **`templates/`**:
  - `global-CLAUDE.md` — the load-bearing proactive-memory directives. Copied to
    `~/.claude-*/CLAUDE.md` by setup with `__BRAIN_VAULT__` substituted. This is what makes the
    model save/recall/checkpoint automatically instead of waiting for `/brain` commands.
  - `settings.hooks.json` — the hooks block merged into `~/.claude-*/settings.json`. Each command
    is wrapped with `BRAIN_VAULT=<vault> <venv python> <repo hook>.py` so the env is set at launch.
  - `skills/brain/SKILL.md` — the `/brain save|recall|checkpoint|forget|list` slash commands.
    These are manual escape hatches; the primary path is the model calling tools proactively.

- **`setup-mac.sh`** — idempotent bootstrap. Installs brain-mcp into `mcp-server/.venv`, writes the
  global CLAUDE.md, drops the brain skill, merges the hooks block into settings.json, and
  registers the MCP server with user scope via `claude mcp add`. Takes
  `<claude-config-dir> <vault-path>` as arguments.

- **`setup-windows.ps1`** — the Windows counterpart to `setup-mac.sh`. Same arguments, same
  idempotency guarantee. Generates a per-install `<config-dir>\brain-launch.cmd` wrapper that
  bakes in `BRAIN_VAULT` and the venv python path, so hook commands in `settings.json` are just
  `<launch.cmd> <hook-name>` with no JSON quote-escaping. Uses `templates/settings.hooks.win.json`
  as the template. Python hooks and MCP server code are unchanged between platforms.

- **`WINDOWS-SETUP.md`, `LMSTUDIO-SETUP.md`** — user-facing install guides for the Windows
  bring-up and the LMStudio MCP registration. Keep these in sync with `setup-windows.ps1` and
  the MCP server command/env contract respectively.

## Common commands

```bash
# Re-install into a Claude Code config dir (idempotent) — macOS
~/src/Ai-Brain/setup-mac.sh ~/.claude-personal ~/Documents/Vaults/Ai-Brain
~/src/Ai-Brain/setup-mac.sh ~/.claude-work ~/Documents/Vaults/Ai-Brain

# Windows equivalent (PowerShell)
# powershell -ExecutionPolicy Bypass -File C:\src\Ai-Brain\setup-windows.ps1 `
#     "$env:USERPROFILE\.claude-personal" "$env:USERPROFILE\Documents\Vaults\Ai-Brain"

# Verify the MCP server is registered and connected
CLAUDE_CONFIG_DIR=~/.claude-personal claude mcp list

# Smoke-test the MCP server over stdio (from any cwd)
BRAIN_VAULT=~/Documents/Vaults/Ai-Brain ~/src/Ai-Brain/mcp-server/.venv/bin/python -m brain_mcp <<'EOF'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
EOF

# Dry-run the session_start hook against a fake payload
echo '{"cwd":"/tmp/test","hook_event_name":"SessionStart","source":"startup"}' | \
  BRAIN_VAULT=~/Documents/Vaults/Ai-Brain \
  ~/src/Ai-Brain/mcp-server/.venv/bin/python ~/src/Ai-Brain/hooks/session_start.py

# Dump the session-start bundle as markdown (useful for non-tool-calling models)
BRAIN_VAULT=~/Documents/Vaults/Ai-Brain \
  ~/src/Ai-Brain/mcp-server/.venv/bin/brain-prep --project MyProject

# Health check — run anytime, especially when the Brain feels stale or broken
BRAIN_VAULT=~/Documents/Vaults/Ai-Brain \
  ~/src/Ai-Brain/mcp-server/.venv/bin/brain-doctor --project MyProject
```

## Gotchas that will bite you

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

## Testing

There is no test suite yet. Verification is manual and lives in the README's verification matrix.
When making a non-trivial change:

1. Re-run `setup-mac.sh` for both Claude config dirs.
2. Sanity-check `BRAIN_VAULT=... .venv/bin/python -c "from brain_mcp import vault, server"` from
   `/tmp` (catches editable-install regressions).
3. Open a fresh Claude Code session in a real project and confirm the brain context is preloaded
   and the `brain_*` tools appear in the tool list.
4. Say *"I prefer X over Y"* and confirm a new file appears in `~/Documents/Vaults/Ai-Brain/Brain/user/`.

## Memory system notes

The Brain is also available to Claude while you work on this codebase. Proactive
save/recall/checkpoint rules are in `templates/global-CLAUDE.md` (which is installed as your
`~/.claude-*/CLAUDE.md`). If the model feels sluggish about saving or recalling, that template is
the single biggest tunable — tighten the triggers there and re-run setup.
