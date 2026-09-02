# Windows setup

How to bring the Brain up on a Windows machine. `brain-setup.py` is the installer on every
platform; this file covers what is Windows-specific about running it.

## Prerequisites

1. **Python 3.11+** from [python.org](https://www.python.org/downloads/windows/). The installer
   registers the `py` launcher, which `brain-setup.py` prefers (it tries `py -3.20` down to
   `py -3.11` before falling back to `python`). Anaconda / MS Store Python also work.
   `brain-setup.py` itself runs on any Python 3 — it only needs to *find* a 3.11+ for the venv.
2. **Claude Code CLI** installed and on `PATH`. Verify with `claude --version` in a new
   PowerShell window. If it's missing, `brain-setup.py` will still configure hooks and
   templates but will skip MCP server registration — you can re-run it once Claude is
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

> Common gotcha: `$env:NAME` is PowerShell's syntax for reading environment variables.
> A literal drive path like `D:\Vaults\Ai-Brain` should NOT be prefixed with `$env:` —
> just pass it as a plain double-quoted string.

## What the installer does

1. Creates `mcp-server\.venv` and installs `brain_mcp` into it (non-editable — editable
   installs break imports from foreign cwds, same footgun as Mac).
2. Sanity-checks that `brain_mcp` imports from `$env:TEMP`, to catch editable-install
   regressions before they reach a live session.
3. Ensures `<vault>\Brain\{user,feedback,references,projects}` exists.
4. Generates `<config>\brain-agent.py` — the launcher the model invokes via the Bash tool
   (`"<venv>/Scripts/python.exe" "<config>/brain-agent.py" recall ...`). It bakes in
   `BRAIN_VAULT` and `BRAIN_AGENT_SURFACE=1`, then hands argv to the `brain` CLI. It is a
   Python file rather than a `.cmd` on purpose: cmd.exe re-expands `%*` after Git Bash has
   quoted the arguments, so the old `brain.cmd` wrapper let a query like `x"&echo pwned`
   run a second command through the pre-approved path (CVE-2024-24576 class).
5. Writes `<config>\CLAUDE.md` and `<config>\skills\brain\SKILL.md` from the templates,
   with `__BRAIN_VAULT__` / `__BRAIN_CMD__` substituted. A hand-written file at either
   path (no `managed-by` marker) is backed up as `<name>.brain-backup-<stamp>` first.
6. Generates `<config>\brain-launch.cmd` — a small wrapper that sets `BRAIN_VAULT` via
   `setlocal`, then execs the requested hook with the venv python. This sidesteps the fact
   that Unix-style `VAR=val cmd` env prefixes don't work in Windows shells. It is written
   in the console's OEM codepage (what cmd.exe reads batch files in), so a vault, repo or
   config path that the codepage cannot express makes setup report a failure instead of
   silently wiring hooks to a nonexistent `BRAIN_VAULT`. Hooks are the only caller — their
   arguments are fixed hook names from `settings.json`, never text a model chose.
7. Merges `templates/settings.hooks.win.json` into `<config>\settings.json`, replacing
   `__BRAIN_LAUNCH__` with the full path to the generated `brain-launch.cmd`. Each hook
   command ends up as `"<config>/brain-launch.cmd" <hook-name>` (quoted, so a space in the
   username survives). Also merges one `permissions.allow` rule per agent subcommand
   (`Bash("<venv python>" "<config>/brain-agent.py" recall:*)`, `… save:*`, …) so
   model-initiated `brain` CLI calls never hit permission prompts; stale rules pointing at
   old wrapper paths — including the retired `brain.cmd` — are pruned on re-run.
8. MCP registration: with `--with-mcp`, registers the brain MCP server at user scope via
   `claude mcp add brain --scope user -e BRAIN_VAULT=<vault> -- <venv-python> -m brain_mcp`.
   Without it (the default), removes any existing user-scope `brain` registration.
9. Cleanup: removes a stale `<config>\.mcp.json` left over from earlier setup attempts
   (only if it is a Brain-only registration) and the retired `<config>\brain.cmd` wrapper
   from an older install (only if it says a Brain installer generated it).

## Verify the install

```powershell
# 1. The brain CLI works end to end (paths per your install):
& "$env:USERPROFILE\src\Ai-Brain\mcp-server\.venv\Scripts\python.exe" "$env:USERPROFILE\.claude-personal\brain-agent.py" stats
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

`brain-setup.py` writes **forward-slash** paths into
`settings.json` for exactly this reason (`C:/Users/<you>/.claude/brain-launch.cmd`).
Forward slashes survive bash, work in cmd.exe, and are accepted by `python.exe`.
If you have an old install, re-run the wizard to refresh the
hook block.

### Hooks don't fire at all (no SessionStart preload, no breadcrumbs in `Brain\activity.md`)

If forward-slash paths aren't enough — e.g. your shell doesn't dispatch `.cmd`
files automatically — wrap each command with `cmd.exe /c`:

```jsonc
// Before
"command": "\"C:/Users/<you>/.claude/brain-launch.cmd\" session_start"

// After
"command": "cmd.exe /c \"\"C:/Users/<you>/.claude/brain-launch.cmd\" session_start\""
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

`brain-setup.py` quotes every path it passes. If you still see quoting errors, pass the
config dir and vault path as quoted strings on the command line, not unquoted.

## Multiple Claude Code accounts

Claude Code stores per-account state (login, settings, MCP registrations,
`CLAUDE.md`) in a single config directory. The default is
`%USERPROFILE%\.claude`. To run multiple accounts side-by-side (personal + work,
or one per client), point Claude Code at a **different config directory per
account** using the `CLAUDE_CONFIG_DIR` environment variable.

The naming is up to you — Claude Code and `brain-setup.py` treat
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

Run the wizard once per config dir, or do both in one call:

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
- `brain-setup.py` — the installer itself. It is one cross-platform script: the hooks, the MCP
  server and the templates are platform-independent, and only the bootstrap and the
  hook-launch mechanism (the generated `.cmd` wrappers) differ on Windows.
