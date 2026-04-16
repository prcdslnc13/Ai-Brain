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
   `%USERPROFILE%\Documents\Vaults\Ai-Brain`. The vault must exist *before* running setup;
   the script will refuse to continue if the path is missing. If the vault lives somewhere
   else, pass its full path as the second argument.

## Install

```powershell
# 1. Clone the repo (anywhere — C:\src\AiBrain is the convention to match ~/src/AiBrain on Mac)
git clone git@github.com:<your-github-user>/Ai-Brain.git C:\src\AiBrain
```

### Recommended: the cross-platform wizard

```powershell
python C:\src\AiBrain\brain-setup.py
```

It prompts for the vault path and which `~\.claude*` dir(s) to install into, and
sidesteps PowerShell quoting entirely (especially helpful for non-standard vault
locations like `D:\Vaults\Ai-Brain`). Re-run any time to refresh — idempotent.

For scripted installs:

```powershell
python C:\src\AiBrain\brain-setup.py --non-interactive `
    --vault "D:\Vaults\Ai-Brain" `
    --claude-dir "$env:USERPROFILE\.claude-personal" `
    --claude-dir "$env:USERPROFILE\.claude-work"
```

### Fallback: the PowerShell installer

If you prefer a native PowerShell install:

```powershell
# Run setup for each Claude Code config dir you use.
#    Arguments: <claude-config-dir> <vault-path>
powershell -ExecutionPolicy Bypass -File C:\src\AiBrain\setup-windows.ps1 `
    "$env:USERPROFILE\.claude-personal" "$env:USERPROFILE\Documents\Vaults\Ai-Brain"

# Non-standard vault path (no $env: prefix needed for plain paths):
powershell -ExecutionPolicy Bypass -File C:\src\AiBrain\setup-windows.ps1 `
    "$env:USERPROFILE\.claude-personal" "D:\Vaults\Ai-Brain"
```

The script is idempotent — re-running updates the global `CLAUDE.md`, hook block, MCP
registration, and the generated `brain-launch.cmd` wrapper without touching anything else in
`settings.json`.

> Common gotcha: `$env:NAME` is PowerShell's syntax for reading environment variables.
> A literal drive path like `D:\Vaults\Ai-Brain` should NOT be prefixed with `$env:` —
> just pass it as a plain double-quoted string.

## What the script does

1. Creates `mcp-server\.venv` and installs `brain_mcp` into it (non-editable — editable
   installs break imports from foreign cwds, same footgun as Mac).
2. Sanity-checks that `brain_mcp` imports from `$env:TEMP`, to catch editable-install
   regressions before they reach a live session.
3. Ensures `<vault>\Brain\{user,feedback,references,projects}` exists.
4. Writes `<config>\CLAUDE.md` from `templates/global-CLAUDE.md` with `__BRAIN_VAULT__`
   substituted.
5. Copies `templates/skills/brain/SKILL.md` to `<config>\skills\brain\SKILL.md`.
6. Generates `<config>\brain-launch.cmd` — a small wrapper that sets `BRAIN_VAULT` via
   `setlocal`, then execs the requested hook with the venv python. This sidesteps the fact
   that Unix-style `VAR=val cmd` env prefixes don't work in Windows shells, *and* avoids
   having to embed escaped quotes in `settings.json`.
7. Merges `templates/settings.hooks.win.json` into `<config>\settings.json`, replacing
   `__BRAIN_LAUNCH__` with the full path to the generated `brain-launch.cmd`. Each hook
   command ends up as just `<config>\brain-launch.cmd <hook-name>`.
8. Registers the brain MCP server at user scope via
   `claude mcp add brain --scope user -e BRAIN_VAULT=<vault> -- <venv-python> -m brain_mcp`.
9. Removes any stale `<config>\.mcp.json` left over from earlier setup attempts.

## Verify the install

```powershell
# 1. MCP server is registered and reachable.
$env:CLAUDE_CONFIG_DIR = "$env:USERPROFILE\.claude-personal"
claude mcp list
# Expected: "brain: ✓ Connected"

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

### Hooks don't fire at all (no SessionStart preload, no breadcrumbs in `Brain\activity.md`)

The most likely cause is Claude Code's Windows hook runner direct-execing the command
instead of going through `cmd.exe`. If that's the case, `brain-launch.cmd` is never
interpreted as a batch file and the hook silently does nothing.

Fix: edit `templates/settings.hooks.win.json` and wrap each command with `cmd.exe /c`:

```jsonc
// Before
"command": "__BRAIN_LAUNCH__ session_start"

// After
"command": "cmd.exe /c \"__BRAIN_LAUNCH__ session_start\""
```

Then re-run `setup-windows.ps1` so the merged `settings.json` picks up the change. If this
fixes it, please leave a note in `ROADMAP.md` Phase 2B so we can make `cmd.exe /c` the
default on Windows.

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
