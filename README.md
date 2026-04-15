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
  `brain_checkpoint`. Claude Code, LMStudio, and any MCP-aware Ollama frontend all connect to the
  same server.
- **Hooks** (`hooks/`) — Python scripts wired into Claude Code's hook events:
  - `session_start.py` — preloads the vault bundle (user profile, feedback, project context) into
    the system prompt at the start of every session.
  - `pre_compact.py` / `session_end.py` — write structural session checkpoints from the transcript
    into `Brain/projects/<project>/sessions/`.
  - `stop.py` — appends a one-line breadcrumb to `Brain/activity.md` and detects save-signal
    phrases ("remember", "from now on", "I prefer"…) by dropping marker files into
    `Brain/.pending-saves/`.
  - `user_prompt_submit.py` — surfaces those markers to the next model turn.
- **Templates** (`templates/`) — `global-CLAUDE.md` (the proactive memory directives loaded as
  user-level instructions), `settings.hooks.json` (hook block merged into Claude's settings),
  `skills/brain/SKILL.md` (manual `/brain` slash commands).

## Setup

Prerequisites: Python 3.11+, `claude` CLI on PATH, an Obsidian vault for the data side.

```bash
# Clone this repo
git clone git@github.com:<your-github-user>/Ai-Brain.git ~/src/AiBrain

# Run setup for each Claude account (-> registers MCP, installs hooks, drops global CLAUDE.md)
~/src/AiBrain/setup-mac.sh ~/.claude-personal ~/Documents/Vaults/Ai-Brain
~/src/AiBrain/setup-mac.sh ~/.claude-work ~/Documents/Vaults/Ai-Brain
```

The setup script is idempotent — re-run it any time to refresh.

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
    └── .pending-saves/     # transient save-signal markers
```

## Local model integration

- **LMStudio**: register the MCP server in LMStudio's settings:
  - command: `~/src/AiBrain/mcp-server/.venv/bin/python`
  - args: `-m brain_mcp`
  - env: `BRAIN_VAULT=<your vault path>`
- **Ollama**: pair with an MCP-capable frontend (Open WebUI, msty) and register the same server.
- **Models without function-calling**: `mcp-server/.venv/bin/brain-prep --project <name>` prints
  the session-start bundle as a system prompt suitable for piping into `ollama run`.
