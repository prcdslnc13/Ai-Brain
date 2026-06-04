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
    `brain_mcp.doctor.check(project, project_cwd)` and prepends a `## Brain Health` banner for any
    warn/error findings: silent failures like unset `BRAIN_VAULT`, Obsidian Sync conflict files,
    corrupt vector index, accidental editable install, plus two stop-gap checks —
    `STALE_UNCOMMITTED` (project has on-disk changes postdating the last checkpoint; prior session
    likely died before checkpointing — reads `project_cwd` from the hook payload, disable with
    `BRAIN_STALE_CHECK=0`) and `PROMISE_GAP` (recent turns promised saves without fulfilling
    them). Surfacing these at the top of the session forces reconstruction instead of silent
    context loss.
  - `pre_compact.py` / `session_end.py` — share `_checkpoint.py`, which parses the transcript JSONL
    and writes a structural checkpoint to `Brain/projects/<project>/sessions/<timestamp>.md`. No
    LLM call — the next session's model will summarize/integrate when it sees the file.
  - `stop.py` — two jobs. (1) Gate: when the assistant's final message contains a save-promise
    phrase (*"I'll save this to brain"*, *"checkpointing now"*, etc.) and no
    `brain_save`/`brain_checkpoint` tool call occurred in the turn, emit
    `{decision: "block", reason: …}` so Claude Code feeds the reason back to the model and it must
    either fulfill the commitment or recant before ending. Disable per-install with
    `BRAIN_STOP_GATE=0`. Re-entries (payload `stop_hook_active=true`) bypass the gate to avoid
    infinite loops. (2) Audit: append a breadcrumb to `Brain/activity.md` with columns
    `[sig=Y|N sav=Y|N nud=Y|N pro=Y|N too=Y|N]` — save-signal in user message, brain tool call this
    turn, UserPromptSubmit nudge enabled, save-promise in assistant message, and whether the brain
    MCP server was *registered for this session* (i.e. `brain_save`/`brain_checkpoint` were actually
    callable, read from the active config dir's `.claude.json` `mcpServers`). `brain_doctor._check_save_gap`
    and `_check_promise_gap` read the tail to surface signal-without-save and promise-without-save
    trends, **skipping `too=N` rows** — a save-promise in a session where the tools weren't registered
    is physically unsatisfiable (infra failure, not a model bug), so counting it would be a false
    positive (this guards against the 2026-06-03 PROMISE_GAP false alarm where the session
    troubleshooting an unregistered brain promised a save the gate then demanded). Legacy rows with
    no `too=` column still count, for backward compatibility. Promise-gap threshold is 1 (any miss is
    a bug); save-gap threshold is 3 in a 30-turn window.
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
# The config dir can be any path. Single-account users typically use ~/.claude;
# multi-account users pick their own names (e.g. ~/.claude-personal, ~/.claude-work).
~/src/Ai-Brain/setup-mac.sh ~/.claude-personal ~/Documents/Vaults/Ai-Brain
~/src/Ai-Brain/setup-mac.sh ~/.claude-work     ~/Documents/Vaults/Ai-Brain

# Windows equivalent (PowerShell)
# powershell -ExecutionPolicy Bypass -File C:\src\Ai-Brain\setup-windows.ps1 `
#     "$env:USERPROFILE\.claude-personal" "$env:USERPROFILE\Documents\Vaults\Ai-Brain"

# Verify the MCP server is registered and connected (omit CLAUDE_CONFIG_DIR for the default ~/.claude)
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
