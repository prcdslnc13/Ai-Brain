# Windows setup

How to bring the Brain up on a Windows machine. The Mac install is documented in `CLAUDE.md`
and `setup-mac.sh`; this file is the Windows counterpart.

## Prerequisites

1. **Python 3.10+** from [python.org](https://www.python.org/downloads/windows/). The installer
   registers the `py` launcher, which `setup-windows.ps1` prefers. Anaconda / MS Store Python
   also work but `py -3` must resolve to a 3.10+ interpreter.
2. **Claude Code CLI** installed and on `PATH`. Verify with `claude --version` in a new
   PowerShell window. If it's missing, `setup-windows.ps1` will still configure hooks and
   templates but will skip MCP server registration — you can re-run the script once Claude is
   on `PATH`.
3. **Git** (for cloning — any recent version is fine).
4. **Obsidian + Obsidian Sync** already set up and synced down to
   `%USERPROFILE%\Vaults\Ai-Brain`. The vault must exist *before* running setup;
   the script will refuse to continue if the path is missing. If the vault lives somewhere
   else, pass its full path as the second argument. Avoid a OneDrive-redirected `Documents`
   folder — OneDrive and Obsidian Sync fighting over the same files causes conflict copies —
   which is why the recommended location is `%USERPROFILE%\Vaults`, not
   `%USERPROFILE%\Documents`.

## Install

```powershell
# 1. Clone the repo (anywhere — C:\src\Ai-Brain is the convention to match ~/src/Ai-Brain on Mac)
git clone https://github.com/<your-github-user>/Ai-Brain.git C:\src\Ai-Brain
```

If you plan to run multiple Claude Code accounts on this machine (e.g. personal +
work), read [Multiple Claude Code accounts](#multiple-claude-code-accounts) at
the bottom of this file before running setup — pick your config-dir naming
first, then install into each.

### Recommended: the cross-platform wizard

```powershell
python C:\src\Ai-Brain\brain-setup.py
```

It prompts for the vault path and which `~\.claude*` dir(s) to install into, and
sidesteps PowerShell quoting entirely (especially helpful for non-standard vault
locations like `D:\Vaults\Ai-Brain`). Re-run any time to refresh — idempotent.

For scripted installs:

```powershell
python C:\src\Ai-Brain\brain-setup.py --non-interactive `
    --vault "D:\Vaults\Ai-Brain" `
    --claude-dir "$env:USERPROFILE\.claude-personal" `
    --claude-dir "$env:USERPROFILE\.claude-work"
```

The `--claude-dir` values can be any path you like — `brain-setup.py` treats them as
opaque strings. `~\.claude-personal` /
`~\.claude-work` are used as examples throughout, but any name that starts
with `.claude` (e.g. `.claude-clientX`) is picked up by the installer's
auto-discovery.

### Deprecated: the PowerShell installer

**Use `brain-setup.py` above.** `setup-windows.ps1` is deprecated as of 2026-08-25 and prints
a warning when run. It still works, but it accepts any `py -3` / `python` / `python3` it finds
with no `>= 3.11` gate (its error text still says "3.10+", which `pyproject.toml` refuses), it
does not install the pytest extra or self-test after installing, and it runs under Windows
PowerShell 5.1 — whose ANSI-default file reads have silently corrupted the generated
`CLAUDE.md` before. `brain-setup.py` has none of those failure modes and writes the same two
`brain.cmd` / `brain-launch.cmd` wrappers.

Kept documented only so an existing muscle-memory invocation still resolves:

```powershell
# Run setup for each Claude Code config dir you use.
#    Arguments: <claude-config-dir> <vault-path>
# Single account using the default config dir:
powershell -ExecutionPolicy Bypass -File C:\src\Ai-Brain\setup-windows.ps1 `
    "$env:USERPROFILE\.claude" "$env:USERPROFILE\Vaults\Ai-Brain"

# Multiple accounts — re-run once per config dir:
powershell -ExecutionPolicy Bypass -File C:\src\Ai-Brain\setup-windows.ps1 `
    "$env:USERPROFILE\.claude-personal" "$env:USERPROFILE\Vaults\Ai-Brain"
powershell -ExecutionPolicy Bypass -File C:\src\Ai-Brain\setup-windows.ps1 `
    "$env:USERPROFILE\.claude-work" "$env:USERPROFILE\Vaults\Ai-Brain"

# Non-standard vault path (no $env: prefix needed for plain paths):
powershell -ExecutionPolicy Bypass -File C:\src\Ai-Brain\setup-windows.ps1 `
    "$env:USERPROFILE\.claude-personal" "D:\Vaults\Ai-Brain"
```

The script is idempotent — re-running updates the global `CLAUDE.md`, skill, hook block, MCP
registration, and the generated `brain-launch.cmd` / `brain.cmd` wrappers without touching
anything else in `settings.json`.

By default no MCP server is registered with Claude Code — the model drives the Brain through
the generated `brain.cmd` CLI wrapper instead (saving ~3k tokens of tool schemas per session),
and any stale user-scope `brain` MCP entry is removed. Pass `-WithMcp` to register the MCP
server as before.

> Common gotcha: `$env:NAME` is PowerShell's syntax for reading environment variables.
> A literal drive path like `D:\Vaults\Ai-Brain` should NOT be prefixed with `$env:` —
> just pass it as a plain double-quoted string.

## What the script does

1. Creates `mcp-server\.venv` and installs `brain_mcp` into it (non-editable — editable
   installs break imports from foreign cwds, same footgun as Mac).
2. Sanity-checks that `brain_mcp` imports from `$env:TEMP`, to catch editable-install
   regressions before they reach a live session.
3. Ensures `<vault>\Brain\{user,feedback,references,projects}` exists.
4. Generates `<config>\brain.cmd` — the CLI wrapper the model invokes via the Bash tool
   (`<config>/brain.cmd recall ...`). Bakes in `BRAIN_VAULT` and forwards all arguments to
   the venv's `brain.exe`.
5. Writes `<config>\CLAUDE.md` and `<config>\skills\brain\SKILL.md` from the templates,
   with `__BRAIN_VAULT__` / `__BRAIN_CMD__` substituted.
6. Generates `<config>\brain-launch.cmd` — a small wrapper that sets `BRAIN_VAULT` via
   `setlocal`, then execs the requested hook with the venv python. This sidesteps the fact
   that Unix-style `VAR=val cmd` env prefixes don't work in Windows shells, *and* avoids
   having to embed escaped quotes in `settings.json`.
7. Merges `templates/settings.hooks.win.json` into `<config>\settings.json`, replacing
   `__BRAIN_LAUNCH__` with the full path to the generated `brain-launch.cmd`. Each hook
   command ends up as just `<config>\brain-launch.cmd <hook-name>`. Also merges a
   `permissions.allow` rule (`Bash(<config>/brain.cmd:*)`) so model-initiated `brain`
   CLI calls never hit permission prompts; stale rules pointing at old wrapper paths
   are pruned on re-run.
8. MCP registration: with `-WithMcp`, registers the brain MCP server at user scope via
   `claude mcp add brain --scope user -e BRAIN_VAULT=<vault> -- <venv-python> -m brain_mcp`.
   Without it (the default), removes any existing user-scope `brain` registration.
9. Removes any stale `<config>\.mcp.json` left over from earlier setup attempts.

## Verify the install

```powershell
# 1. The brain CLI works end to end (paths per your install):
& "$env:USERPROFILE\.claude-personal\brain.cmd" stats
# Expected: one line of JSON with total_items / by_type counts.
# (If you installed with -WithMcp, also check: claude mcp list → "brain: ✓ Connected")

# 2. Open a Claude Code session in any project dir. The SessionStart hook should
#    preload the brain bundle into context (user profile, feedback, project overview,
#    most recent session checkpoint).

# 3. In that session, ask: "what do you know about me?"
#    The model should surface memories written on the Mac side — this proves Obsidian Sync
#    propagated the vault content end to end.

# 4. Say: "I prefer <something> over <something else>"
#    A new file should appear in <vault>\Brain\user\ within a few seconds.
```

## Troubleshooting

### Hooks fail with `command not found` and a path missing its backslashes

Symptom: Claude Code logs an error like
`/usr/bin/bash: line 1: C:Users<you>.claudebrain-launch.cmd: command not found`.
Notice the path has no backslashes — they were eaten by Git Bash, which Claude
Code uses to run hooks on many Windows setups.

`brain-setup.py` (and the deprecated `setup-windows.ps1`) write **forward-slash** paths into
`settings.json` for exactly this reason (`C:/Users/<you>/.claude/brain-launch.cmd`).
Forward slashes survive bash, work in cmd.exe, and are accepted by `python.exe`.
If you have an old install, re-run the wizard to refresh the
hook block.

### Hooks don't fire at all (no SessionStart preload, no breadcrumbs in `Brain\activity.md`)

If forward-slash paths aren't enough — e.g. your shell doesn't dispatch `.cmd`
files automatically — wrap each command with `cmd.exe /c`:

```jsonc
// Before
"command": "C:/Users/<you>/.claude/brain-launch.cmd session_start"

// After
"command": "cmd.exe /c \"C:/Users/<you>/.claude/brain-launch.cmd session_start\""
```

Edit `templates/settings.hooks.win.json` to bake this in, then re-run setup so the
merged `settings.json` picks it up. If this fixes it for you, please leave a note in
`ROADMAP.md` Phase 2B so we can make `cmd.exe /c` the default on Windows.

### `claude` not on PATH

The setup script prints a warning and skips MCP registration but otherwise completes. Install
Claude Code, ensure `claude --version` works in a new PowerShell window, and re-run the
setup script. No harm done — the second run just overwrites what the first run wrote.

### `py` launcher missing

Install Python from python.org with "Add Python to PATH" *and* "Install launcher for all
users" both checked. The script falls back to `python` and `python3` on PATH if `py` is
missing, but `py -3` is the most reliable way to hit a modern interpreter on Windows.

### `$CLAUDE_PROJECT_DIR` not populated

If hooks fire but session checkpoints land in the wrong project folder (or no folder at all),
the hook is failing to resolve the project name. The hook reads `cwd` from the payload first,
then `CLAUDE_PROJECT_DIR` from the environment. If both are missing, open an issue with the
exact Claude Code version — this is a regression worth fixing at the source.

### Path with spaces

`setup-windows.ps1` quotes every path it passes. If you still see quoting errors, pass the
config dir and vault path as quoted strings on the PowerShell command line, not unquoted.

## Multiple Claude Code accounts

Claude Code stores per-account state (login, settings, MCP registrations,
`CLAUDE.md`) in a single config directory. The default is
`%USERPROFILE%\.claude`. To run multiple accounts side-by-side (personal + work,
or one per client), point Claude Code at a **different config directory per
account** using the `CLAUDE_CONFIG_DIR` environment variable.

The naming is up to you — Claude Code and `setup-windows.ps1` treat
`CLAUDE_CONFIG_DIR` as an opaque path. `~\.claude-personal` and `~\.claude-work`
are the examples used throughout these docs, but anything starting with
`.claude` (e.g. `.claude-acme`, `.claude-clientX`) is picked up by the Ai-Brain
installer's auto-discovery.

### One-off from a PowerShell prompt

```powershell
# Personal account
$env:CLAUDE_CONFIG_DIR = "$env:USERPROFILE\.claude-personal"
claude

# Work account
$env:CLAUDE_CONFIG_DIR = "$env:USERPROFILE\.claude-work"
claude
```

The first launch against a fresh config dir walks you through login. Each
config dir is fully isolated — login, settings.json, projects/, and MCP
registrations are all separate.

### Persistent, across every PowerShell window

Add functions to your PowerShell `$PROFILE`. Run `notepad $PROFILE` to open (or
create) it, then add:

```powershell
function claude-personal {
    $env:CLAUDE_CONFIG_DIR = "$env:USERPROFILE\.claude-personal"
    claude @args
}
function claude-work {
    $env:CLAUDE_CONFIG_DIR = "$env:USERPROFILE\.claude-work"
    claude @args
}
```

Save, close, and reload (`. $PROFILE`) or open a new PowerShell window.
`claude-personal` and `claude-work` now launch Claude Code against the right
config dir automatically.

### Install the Brain into each account

Run `setup-windows.ps1` (or the wizard) once per config dir:

```powershell
powershell -ExecutionPolicy Bypass -File C:\src\Ai-Brain\setup-windows.ps1 `
    "$env:USERPROFILE\.claude-personal" "$env:USERPROFILE\Vaults\Ai-Brain"
powershell -ExecutionPolicy Bypass -File C:\src\Ai-Brain\setup-windows.ps1 `
    "$env:USERPROFILE\.claude-work" "$env:USERPROFILE\Vaults\Ai-Brain"
```

Or in one `brain-setup.py` call:

```powershell
python C:\src\Ai-Brain\brain-setup.py `
    --vault "$env:USERPROFILE\Vaults\Ai-Brain" `
    --claude-dir "$env:USERPROFILE\.claude-personal" `
    --claude-dir "$env:USERPROFILE\.claude-work"
```

All accounts share the same vault by default, so a memory saved in one account
is recalled from the others. If you want partitioned memories, point each
install at a different `BRAIN_VAULT` instead.

## Sync hygiene

Add `Brain\.index\` and `Brain\archive\` to Obsidian's sync-ignore list. The vector
index is machine-local sqlite — syncing it just churns bandwidth and the index will
self-heal on the next `brain_recall` regardless. The archive directory holds rolled-up
old session checkpoints and is rarely read.

## Also see

- `ROADMAP.md` — Phase 2B for design notes and the list of Windows-specific verification
  steps.
- `CLAUDE.md` — architecture, gotchas, and testing guidance shared across platforms.
- `setup-mac.sh` — the macOS counterpart. Diffs between the two are deliberate: hooks and MCP
  server are cross-platform; only the bootstrap and hook-launch mechanism differ.
