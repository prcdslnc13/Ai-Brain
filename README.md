# Ai-Brain

Cross-machine, cross-account memory system for Claude Code and local LLMs (LMStudio, Ollama).

## Two-location split

This repo holds the **code**: hooks, MCP server, templates, setup scripts. The actual memory
**content** lives in your Obsidian vault and is propagated between machines via Obsidian Sync.

| Layer | Lives at | Synced via | Contains |
|---|---|---|---|
| Code | `~/src/Ai-Brain` (this repo) | git push/pull | hooks, MCP server, templates, setup |
| Data | `~/Vaults/Ai-Brain` (Obsidian vault) | Obsidian Sync | `Brain/user/`, `Brain/feedback/`, `Brain/projects/`, etc. |

> **Choosing a vault location.** Obsidian Sync works from any folder, so pick one no OS
> permission layer sits in front of. On macOS, avoid the TCC-protected folders —
> `~/Documents`, `~/Desktop`, `~/Downloads`, and iCloud Drive. TCC grants access *per host
> application*, and the failure mode is nasty: an ungranted terminal or agent host can still
> *create* files in the vault, but gets `PermissionError` opening pre-existing ones — which
> surfaces as failed overwrites and phantom `INDEX_CORRUPT` warnings while saves appear to
> work. On Windows, avoid a OneDrive-redirected `Documents` folder (two sync engines fighting
> over the same files). Recommended: `~/Vaults/Ai-Brain` (macOS/Linux) or
> `%USERPROFILE%\Vaults\Ai-Brain` (Windows).

The setup script wires the two together: it points the hooks block in your Claude Code
`settings.json` at this repo, installs the `brain` CLI + skill (the default interface), and —
only if you pass `--with-mcp` — registers the MCP server, all with `BRAIN_VAULT` set to your
vault path.

## Architecture

- **`brain` CLI** (`mcp-server/brain_mcp/cli.py`) — the primary interface. Any agent with a
  shell tool (Claude Code via the brain skill, [pi](https://pi.dev), plain scripts) runs
  `brain recall|save|list|forget|checkpoint|stats|doctor`. Costs zero context tokens until
  invoked, unlike MCP tool schemas which load into every session (~3k tokens).
- **Brain MCP server** (`mcp-server/brain_mcp/server.py`) — the same operations as typed MCP
  tools, for clients that want MCP (LMStudio, MCP-aware Ollama frontends, or Claude Code with
  `--with-mcp`). Both frontends share the same core (`vault.py`, `render.py`), so recall output
  and payload caps are identical either way.
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
git clone https://github.com/<your-github-user>/Ai-Brain.git ~/src/Ai-Brain
```

If you plan to run multiple Claude Code accounts on the same machine (personal + work,
for example), see [Multiple Claude Code accounts](#multiple-claude-code-accounts) before
running setup — pick your config-dir naming first, then install into each.

### Recommended: cross-platform wizard

```bash
python3 ~/src/Ai-Brain/brain-setup.py
```

Stdlib-only; works on macOS, Windows, and Linux. Auto-detects every `~/.claude*`
config dir, prompts for the vault path (with `~/Vaults/Ai-Brain` as the
default), and installs into your selection. Re-run any time to refresh — it's
idempotent.

For scripted installs:

```bash
python3 ~/src/Ai-Brain/brain-setup.py --non-interactive \
    --vault ~/Vaults/Ai-Brain \
    --claude-dir ~/.claude-personal --claude-dir ~/.claude-work
```

### Fallback: platform shell scripts

The original shell installers are still here if you prefer them. Each one takes
`<claude-config-dir> <vault-path>` — the config dir can be any name you like
(e.g. `~/.claude`, `~/.claude-personal`, `~/.claude-work`, `~/.claude-projectX`):

```bash
# macOS — single account (default config dir)
~/src/Ai-Brain/setup-mac.sh ~/.claude ~/Vaults/Ai-Brain

# macOS — multiple accounts (re-run once per config dir)
~/src/Ai-Brain/setup-mac.sh ~/.claude-personal ~/Vaults/Ai-Brain
~/src/Ai-Brain/setup-mac.sh ~/.claude-work     ~/Vaults/Ai-Brain
```

```bash
# Linux (Debian Trixie, Raspberry Pi OS, Ubuntu 22.04+)
~/src/Ai-Brain/setup-linux.sh ~/.claude ~/Vaults/Ai-Brain
```

On Ubuntu 22.04 the default `python3` is 3.10 (too old). Install a newer
interpreter first via the deadsnakes PPA: `sudo apt install python3.11
python3.11-venv`. Debian Trixie / Raspberry Pi OS (2025+) ship Python 3.13
and only need `sudo apt install python3-venv`.

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File C:\src\Ai-Brain\setup-windows.ps1 `
    "$env:USERPROFILE\.claude" "$env:USERPROFILE\Vaults\Ai-Brain"
```

All three are idempotent. See `WINDOWS-SETUP.md` for Windows-specific guidance.

**MCP is opt-in.** By default no MCP server is registered with Claude Code — the model drives
the Brain through the `brain` CLI (via the installed skill and global CLAUDE.md), which avoids
loading ~3k tokens of tool schemas into every session. Re-running setup without the flag also
*removes* any existing user-scope `brain` registration. Pass `--with-mcp` (shell scripts and
`brain-setup.py`) or `-WithMcp` (`setup-windows.ps1`) to register the MCP server for Claude
Code as well. LMStudio/Ollama registration is separate (in that app's own config) and is
unaffected by this flag.

## Multiple Claude Code accounts

Claude Code stores per-account state (login, settings, MCP registrations, global
`CLAUDE.md`) in a single config directory. The default is `~/.claude` on
macOS/Linux and `%USERPROFILE%\.claude` on Windows. To run multiple accounts
side-by-side (e.g. personal + work, or one account per client), point Claude
Code at a **different config directory per account** using the
`CLAUDE_CONFIG_DIR` environment variable.

The naming is entirely up to you — Claude Code and the Ai-Brain installer both
treat `CLAUDE_CONFIG_DIR` as an opaque path. `~/.claude-personal` and
`~/.claude-work` are used throughout these docs as examples, but
`~/.claude-acme` or `~/.claude-client-foo` work equally well. The Ai-Brain
installer's auto-discovery (in `brain-setup.py` and the uninstallers) finds
every `~/.claude*` directory, so any name starting with `.claude` is picked up.

### macOS / Linux

Set the env var before launching `claude`:

```bash
# Personal account
CLAUDE_CONFIG_DIR=~/.claude-personal claude

# Work account
CLAUDE_CONFIG_DIR=~/.claude-work claude
```

First launch in a fresh config dir will walk you through login. Each config dir
is its own fully isolated Claude Code install — login, settings.json,
projects/, and MCP registrations are all separate.

For convenience, add shell aliases to your `~/.zshrc` / `~/.bashrc`:

```bash
alias claude-personal='CLAUDE_CONFIG_DIR=$HOME/.claude-personal claude'
alias claude-work='CLAUDE_CONFIG_DIR=$HOME/.claude-work claude'
```

Then install the Brain wiring into each:

```bash
~/src/Ai-Brain/setup-mac.sh ~/.claude-personal ~/Vaults/Ai-Brain
~/src/Ai-Brain/setup-mac.sh ~/.claude-work     ~/Vaults/Ai-Brain
```

Or do both in one call with the cross-platform wizard:

```bash
python3 ~/src/Ai-Brain/brain-setup.py \
    --vault ~/Vaults/Ai-Brain \
    --claude-dir ~/.claude-personal \
    --claude-dir ~/.claude-work
```

### Windows

Same idea with PowerShell:

```powershell
# Personal account (one-off)
$env:CLAUDE_CONFIG_DIR = "$env:USERPROFILE\.claude-personal"
claude

# Work account (one-off)
$env:CLAUDE_CONFIG_DIR = "$env:USERPROFILE\.claude-work"
claude
```

Make it persistent across PowerShell sessions by adding functions to your
`$PROFILE` (run `notepad $PROFILE` to create/edit it):

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

Then install the Brain wiring into each:

```powershell
powershell -ExecutionPolicy Bypass -File C:\src\Ai-Brain\setup-windows.ps1 `
    "$env:USERPROFILE\.claude-personal" "$env:USERPROFILE\Vaults\Ai-Brain"
powershell -ExecutionPolicy Bypass -File C:\src\Ai-Brain\setup-windows.ps1 `
    "$env:USERPROFILE\.claude-work" "$env:USERPROFILE\Vaults\Ai-Brain"
```

### Notes

- **Same vault across all accounts.** All examples above point every Claude Code
  account at the same `BRAIN_VAULT`. That's the whole point — a memory written
  from your work account is readable from your personal account, and vice versa.
  If you want partitioned memories instead, pass a different vault path per
  install (e.g. `~/Vaults/Ai-Brain-Work`).
- **The default `~/.claude` still works.** You don't have to use `CLAUDE_CONFIG_DIR`
  at all — single-account users can run `setup-mac.sh ~/.claude <vault>` and
  launch `claude` with no env var. The installer auto-detects whether the target
  is the default config dir and writes the MCP registration to the right
  `.claude.json` either way.
- **Verifying which account is active.** Each config dir gets its own rendered
  `CLAUDE.md` and skill, so an easy check is whether the session's brain context
  preloads. If you installed with `--with-mcp`, `claude mcp list` additionally
  shows the `brain` server for the current `CLAUDE_CONFIG_DIR` (or `~/.claude`
  if unset).

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

- **llama.cpp / cherryd / any harness without hooks**: see `LOCAL-HARNESS-SETUP.md`. Two
  problems, two fixes — size the preload to the context window (`brain-prep --slim --budget-kb N`),
  and checkpoint from the harness's own session log on a timer
  (`brain checkpoint --from-cherryd <db> --all-sessions`) so a context overflow can't take the
  session with it. Nothing there depends on the model remembering to save.
- **pi** ([pi.dev](https://pi.dev)): pi has no MCP support by design — use the `brain` CLI via
  pi's shell tool. See `PI-SETUP.md`.
- **LMStudio**: register the MCP server in LMStudio's settings (see `LMSTUDIO-SETUP.md`):
  - command: `~/src/Ai-Brain/mcp-server/.venv/bin/python`
  - args: `-m brain_mcp`
  - env: `BRAIN_VAULT=<your vault path>`
- **Ollama**: pair with an MCP-capable frontend (Open WebUI, msty) and register the same server.
- **Models without function-calling**: `mcp-server/.venv/bin/brain-prep --project <name>` prints
  the session-start bundle as a system prompt suitable for piping into `ollama run`.

Recall/list output is server-capped for every client (local models used to receive unbounded
payloads — one project recall returned 200k+ tokens). Defaults: 3 hits, ~300-char previews,
6k-char bodies with `full_body`, 20k chars total, session checkpoints excluded unless asked.
Tune with `BRAIN_RECALL_MAX_K`, `BRAIN_RECALL_PREVIEW_CHARS`, `BRAIN_RECALL_MAX_BODY_CHARS`,
`BRAIN_RECALL_MAX_TOTAL_CHARS`.
