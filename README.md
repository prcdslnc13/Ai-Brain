# Ai-Brain

Cross-machine, cross-account memory system for Claude Code and local LLMs (LMStudio, Ollama).

## Two-location split

This repo holds the **code**: hooks, MCP server, templates, setup scripts. The actual memory
**content** lives in your Obsidian vault and is propagated between machines via Obsidian Sync.

| Layer | Lives at | Synced via | Contains |
|---|---|---|---|
| Code | `~/src/AiBrain` (this repo) | git push/pull | hooks, MCP server, templates, setup |
| Data | `~/Documents/Vaults/Ai-Brain` (Obsidian vault) | Obsidian Sync | `Brain/user/`, `Brain/feedback/`, `Brain/projects/`, etc. |

The setup script wires the two together: it points the hooks block in your Claude Code
`settings.json` at this repo and registers the MCP server with `BRAIN_VAULT` set to your vault path.

## Architecture

- **Brain MCP server** (`mcp-server/`) — Python stdio MCP server exposing the vault as typed tools:
  `brain_session_start`, `brain_recall`, `brain_save`, `brain_list`, `brain_forget`,
  `brain_checkpoint`, `brain_stats`, `brain_doctor`. Claude Code, LMStudio, and any MCP-aware
  Ollama frontend all connect to the same server.
- **Hooks** (`hooks/`) — Python scripts wired into Claude Code's hook events:
  - `session_start.py` — preloads the vault bundle (user profile, feedback, project context) into
    the system prompt, and prepends a `## Brain Health` banner for any warn/error findings from
    `brain_doctor` so silent failures (unset `BRAIN_VAULT`, Obsidian Sync conflicts, corrupt
    vector index) become visible.
  - `pre_compact.py` / `session_end.py` — write structural session checkpoints from the transcript
    into `Brain/projects/<project>/sessions/`.
  - `stop.py` — appends a one-line breadcrumb to `Brain/activity.md` after every turn.
- **Templates** (`templates/`) — `global-CLAUDE.md` (the proactive memory directives loaded as
  user-level instructions), `settings.hooks.json` (hook block merged into Claude's settings),
  `skills/brain/SKILL.md` (manual `/brain` slash commands).

## Setup

Prerequisites: Python 3.11+, `claude` CLI on PATH, an Obsidian vault for the data side.

```bash
# Clone this repo
git clone git@github.com:<your-github-user>/Ai-Brain.git ~/src/AiBrain
```

### Recommended: cross-platform wizard

```bash
python3 ~/src/AiBrain/brain-setup.py
```

Stdlib-only; works on macOS, Windows, and Linux. Auto-detects every `~/.claude*`
config dir, prompts for the vault path (with `~/Documents/Vaults/Ai-Brain` as the
default), and installs into your selection. Re-run any time to refresh — it's
idempotent.

For scripted installs:

```bash
python3 ~/src/AiBrain/brain-setup.py --non-interactive \
    --vault ~/Documents/Vaults/Ai-Brain \
    --claude-dir ~/.claude-personal --claude-dir ~/.claude-work
```

### Fallback: platform shell scripts

The original shell installers are still here if you prefer them:

```bash
# macOS
~/src/AiBrain/setup-mac.sh ~/.claude-personal ~/Documents/Vaults/Ai-Brain
~/src/AiBrain/setup-mac.sh ~/.claude-work ~/Documents/Vaults/Ai-Brain
```

```bash
# Linux (Debian Trixie, Raspberry Pi OS, Ubuntu 22.04+)
~/src/AiBrain/setup-linux.sh ~/.claude-personal ~/Documents/Vaults/Ai-Brain
```

On Ubuntu 22.04 the default `python3` is 3.10 (too old). Install a newer
interpreter first via the deadsnakes PPA: `sudo apt install python3.11
python3.11-venv`. Debian Trixie / Raspberry Pi OS (2025+) ship Python 3.13
and only need `sudo apt install python3-venv`.

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File C:\src\AiBrain\setup-windows.ps1 `
    "$env:USERPROFILE\.claude-personal" "$env:USERPROFILE\Documents\Vaults\Ai-Brain"
```

All three are idempotent. See `WINDOWS-SETUP.md` for Windows-specific guidance.

## Vault layout

The setup script does NOT create vault content; it expects the vault to exist and have a `Brain/`
directory. A minimal seed (created on first save automatically):

```
<vault-root>/
└── Brain/
    ├── README.md           # human-facing explanation (optional)
    ├── _index.md           # map-of-content; loaded into every session
    ├── user/               # user profile + preferences
    ├── feedback/           # behavior corrections + validated approaches
    ├── references/         # pointers to external systems
    ├── projects/<name>/    # per-project context + session checkpoints
    ├── activity.md         # rolling breadcrumb log
    ├── .index/             # local sqlite vector index — DO NOT sync (machine-local)
    └── archive/            # rolled-up old checkpoints — exclude from sync to save bandwidth
```

Add `Brain/.index/` and `Brain/archive/` to Obsidian's sync-ignore list. The vector
index is rebuilt automatically on the next `brain_recall`, so syncing it across
machines just churns disk and bandwidth. The archive is large but rarely read.

## Local model integration

- **LMStudio**: register the MCP server in LMStudio's settings:
  - command: `~/src/AiBrain/mcp-server/.venv/bin/python`
  - args: `-m brain_mcp`
  - env: `BRAIN_VAULT=<your vault path>`
- **Ollama**: pair with an MCP-capable frontend (Open WebUI, msty) and register the same server.
- **Models without function-calling**: `mcp-server/.venv/bin/brain-prep --project <name>` prints
  the session-start bundle as a system prompt suitable for piping into `ollama run`.
