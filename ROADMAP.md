# Roadmap

This document exists so that a future Claude session (possibly one with no context from the
planning conversations) can pick up where the previous one left off. Update it when a phase
completes or the plan changes.

## Status at a glance

- **Phase 1 — Mac bring-up: ✅ complete.** Brain MCP server, hooks, templates, setup-mac.sh, and
  per-account install for both `claude-personal` and `claude-work` all work end-to-end. Verified by
  running a real Claude Code session: SessionStart preload fires, `brain_*` tools appear in the
  tool list, proactive `brain_save` and `brain_recall` work without the user typing `/brain`.
  Repo is pushed to `github.com/<your-github-user>/Ai-Brain` (private). Code lives at `~/src/AiBrain`,
  memory content lives in the Obsidian vault at `~/Documents/Vaults/Ai-Brain`.

- **Phase 2 — Local model integration: 🟡 in progress.** 2A (LMStudio) has a setup doc
  (`LMSTUDIO-SETUP.md`) and the MCP server has been smoke-tested standalone; the remaining
  step is a human clicking through LMStudio's UI on each machine. 2B (Windows setup script)
  is implemented but not yet verified on a real Windows machine. 2C (Ollama) still pending.

- **Phase 3 — Hardening + quality: ⏳ not started.** Tuning, smarter checkpoints, optional
  improvements. See below.

---

## Phase 2 — Local model integration

Three independent pieces. Can be tackled in any order. 2A is the smallest win and the most useful
because the user explicitly uses LMStudio; start there if no other preference is stated.

### 2A — LMStudio MCP registration 🟡 doc written, pending UI step

**Status:** `LMSTUDIO-SETUP.md` documents the full install, verification, and troubleshooting
flow for both macOS and Windows. The brain MCP server was smoke-tested standalone via stdio
and confirmed healthy (initialize + tools/list both return the expected six-tool envelope).
The only remaining work is human — clicking through LMStudio's settings UI on each machine
to register the server — because LMStudio doesn't expose an import-from-file CLI.

**Goal:** get the brain `brain_*` tools showing up inside LMStudio chats with tool-capable models.

**Context:** LMStudio has a built-in MCP client (as of 2025). We just need to register our
existing MCP server in LMStudio's settings UI — no code changes needed on our side.

**Steps:**

1. Open LMStudio → Settings → Model Context Protocol (or the current equivalent in the UI).
2. Add a new stdio MCP server with these fields:
   - **Name:** `brain`
   - **Command:** `/Users/<you>/src/AiBrain/mcp-server/.venv/bin/python` (or whatever the
     user's local path is — `setup-mac.sh` prints it at the end)
   - **Args:** `-m brain_mcp`
   - **Env:** `BRAIN_VAULT=/Users/<you>/Documents/Vaults/Ai-Brain`
3. Save and restart a chat. Load a tool-capable model (Qwen2.5-7B-Instruct, Llama 3.1+, etc.).
4. In a chat, ask *"what do you know about me?"* — the model should call `brain_session_start`
   and surface the user profile.
5. Document the exact click path in `templates/lmstudio-setup.md` (create it) so the user has a
   reference for future machines. Keep it short — screenshots optional.

**Verification:** LMStudio chat shows `brain_*` tools available; a test `brain_recall` call
returns content from the vault.

**This phase requires human action** (clicking in LMStudio's UI). An agent can only prepare the
instructions and verify the MCP server standalone.

### 2B — Windows setup script (`setup-windows.ps1`) 🟡 implemented, pending verification

**Status:** `setup-windows.ps1` and `templates/settings.hooks.win.json` are in the repo.
Design notes:
- Uses a generated per-install `<config-dir>\brain-launch.cmd` wrapper (the roadmap's
  preferred option) that bakes in `BRAIN_VAULT` and the venv python path. Each hook command
  in `settings.json` is therefore just `<launch.cmd> <hook-name>` — no JSON quote escaping.
- The wrapper uses `setlocal` so `BRAIN_VAULT` doesn't leak into the parent shell, and
  `exit /b %ERRORLEVEL%` to propagate the hook's exit code.
- Python discovery tries `py -3` → `python` → `python3`.
- The JSON merge script escapes backslashes in the launch path before `template.replace` —
  single backslashes would otherwise break `json.loads` of the template.

**What still needs to happen on a real Windows machine:**
1. Clone the repo to `C:\src\AiBrain` (or wherever) and run:
   `powershell -ExecutionPolicy Bypass -File C:\src\AiBrain\setup-windows.ps1 "$env:USERPROFILE\.claude-personal" "$env:USERPROFILE\Documents\Vaults\Ai-Brain"`
2. Confirm `claude mcp list` shows `brain: ✓ Connected`.
3. Open a Claude Code session in a real project → SessionStart preload appears.
4. Say *"what do you know about me?"* → model recalls Mac-written memories (proves Obsidian
   Sync propagated the vault content).
5. If hooks fail to fire, first thing to try: change the command in
   `templates/settings.hooks.win.json` from `__BRAIN_LAUNCH__ <hook>` to
   `cmd.exe /c "__BRAIN_LAUNCH__ <hook>"`. (Claude Code on Windows *should* run hook commands
   through cmd.exe, which handles `.cmd` files natively, but if it direct-execs them we'll
   need the explicit prefix.)
6. Verify `$CLAUDE_PROJECT_DIR` is populated on Windows the same way it is on Mac — the
   hooks read it via `os.environ.get`. If it's missing, the project name falls back to the
   `cwd` key in the hook payload, so this is only a risk if that payload is also absent.

**Goal:** bootstrap the brain on Windows machines with the same idempotent guarantee as
`setup-mac.sh`.

**Context:** The hook scripts and MCP server are pure Python and already OS-agnostic — they run
identically on Windows given the right launcher command. Only `setup-mac.sh` is platform-specific.

**Steps:**

1. Create `setup-windows.ps1` at the repo root. It should mirror `setup-mac.sh` section-for-section:
   - Accept `<claude-config-dir> <vault-path>` as parameters.
   - Resolve repo location via `$PSScriptRoot`.
   - Check for Python (`python` or `py -3` — prefer `py -3.11` or `py -3.12`).
   - Create/update the venv at `mcp-server\.venv`.
   - Install brain-mcp non-editable (`pip install .`), NOT editable — same footgun as Mac.
   - Sanity-check import from `$env:TEMP`.
   - Write `CLAUDE.md` to the config dir, substituting `__BRAIN_VAULT__`.
   - Copy `skills\brain\SKILL.md`.
   - Merge the hooks block into `settings.json`. The `settings.hooks.json` template contains
     `BRAIN_VAULT=__BRAIN_VAULT__ __BRAIN_PYTHON__ __BRAIN_HOOKS__/*.py` — that Unix-style
     env-prefix syntax does NOT work on Windows. On Windows, wrap each command as a cmd.exe
     invocation: `cmd.exe /c "set BRAIN_VAULT=<vault>&& <python> <hook>.py"` or use a tiny
     wrapper script (`hooks\launch.cmd`) that sets the env and execs. The wrapper approach is
     probably cleaner — then the settings.hooks.json template can have a Windows variant
     (`settings.hooks.win.json`) that just calls `hooks\launch.cmd session_start`.
   - Register the MCP server via `claude mcp add --scope user -e BRAIN_VAULT=<vault> -- <python> -m brain_mcp`
     (same as Mac — the `claude` CLI works the same on Windows).

2. Path separators: convert forward slashes to backslashes throughout. PowerShell's
   `Join-Path` handles this naturally.

3. The `$CLAUDE_PROJECT_DIR` env var that hooks read to derive the project name — verify it's
   populated on Windows the same way. If not, add a fallback.

**Verification:**

1. On a Windows machine: `powershell -File C:\src\AiBrain\setup-windows.ps1 $env:USERPROFILE\.claude-personal "C:\Users\<you>\Documents\Vaults\Ai-Brain"`
2. Open a Claude Code session in any project → SessionStart preload appears.
3. Run `claude mcp list` → `brain: ✓ Connected`.
4. Say *"what do you know about me?"* → model recalls the Mac-written memories (proves Obsidian
   Sync propagated the vault content).

**Trap to avoid:** do not try to make the existing `settings.hooks.json` template work on
Windows with clever sh-style escaping. Use a platform-specific template file or a wrapper script.

### 2C — Ollama integration

**Goal:** make the brain accessible from Ollama, which has no native MCP client.

**Context:** Ollama is just an inference runtime; tool use is handled by the *client* talking to
Ollama. Two approaches:

**Option 1 (recommended): use an MCP-capable frontend.**
- **Open WebUI** — actively developed, has MCP client support, connects to Ollama as a backend.
- **msty** — also supports MCP.
- Install whichever, register the same brain MCP server command as for LMStudio. Tool-capable
  Ollama models (Llama 3.1+, Qwen2.5+, Mistral Small) will then see the `brain_*` tools.

**Option 2: `brain-prep` CLI pipe (for models without function-calling).**
- Already shipped in `mcp-server/brain_mcp/brain_prep.py`.
- Usage: `brain-prep --project MyProject | ollama run gemma3`
- Injects the session-start bundle as a system prompt prefix. No tools, so the model can only
  *read* the brain — it cannot save, recall mid-conversation, or checkpoint.
- Verify on a real machine that this actually works: the venv's `brain-prep` script is a
  generated entry point from `pyproject.toml`. Confirm it's executable after `pip install .`
  (should be at `mcp-server/.venv/bin/brain-prep`).

**Steps:**

1. Decide on the frontend (Open WebUI is the safer bet — more active, more docs).
2. Install it via the user's preferred method (Docker, brew, etc. — check the project's README).
3. Register the brain MCP server in the frontend's settings with the same command/args/env as
   for LMStudio.
4. Load a tool-capable Ollama model and run the same recall test.
5. Separately, verify the `brain-prep` pipe approach works with a non-tool-capable model (Gemma,
   older Llama, etc.).
6. Document both flows in `templates/ollama-setup.md`.

**Verification:** a tool-capable Ollama model in the chosen frontend surfaces stored memories via
`brain_recall`; `brain-prep | ollama run <non-tool-model>` produces a response that references the
preloaded bundle.

---

## Phase 3 — Hardening and quality improvements

These are smaller, opportunistic items. Only do them when Phase 2 is complete and there's concrete
evidence they matter.

### 3A — Global CLAUDE.md tuning based on real usage

**Trigger:** when you notice the model failing to save something it should have, or recalling
when it didn't need to, or missing an obvious save-signal phrase.

**What to do:**

1. Find the specific case in a transcript (look in `Brain/projects/<proj>/sessions/` or the
   recent activity log).
2. Decide whether it's a coverage gap (the rule didn't match) or a hallucination (the rule
   matched but the model interpreted it differently).
3. Edit `templates/global-CLAUDE.md` to add or tighten the relevant trigger. Be concrete —
   vague rules like "save important things" don't work; specific rules like "save when the user
   says *'I prefer X'*" do.
4. Re-run `setup-mac.sh` for both accounts so the updated CLAUDE.md propagates.
5. Commit with a message like *"Tighten proactive-save trigger: add 'my default is X' phrasing"*.

The goal is zero manual `/brain save` calls in normal use. Every manual save is a failure of the
global CLAUDE.md and should prompt a tightening pass.

### 3B — Smarter session checkpoints

**Current state:** `_checkpoint.py` produces a structural extract from the transcript JSONL
(user messages, tool call counts, last assistant message). Good enough as a safety net, but not
a real summary.

**Upgrade path:** spawn `claude -p` headless at PreCompact/SessionEnd time to produce a real
summary. The original plan had this; we went structural-first to avoid the token cost for
rarely-read checkpoints. Revisit if:

- Recall quality on old projects is weak (the structural extract doesn't capture decisions).
- The user explicitly asks for better summaries.
- There's budget to spend tokens on checkpoints.

**How:** in `_checkpoint.py`, after `parse_transcript`, if a user-set env var like
`BRAIN_CHECKPOINT_MODE=headless` is present, spawn `claude -p` with a prompt like
*"Summarize this transcript into a session checkpoint: attempts, outcomes, decisions, open threads"*
and the transcript content. Fall back to structural on failure. Keep structural as the default.

### 3C — Stale-memory verification helper

**Problem:** memories written weeks ago may name functions, files, or flags that no longer
exist. The global CLAUDE.md tells Claude to verify before acting, but an automated helper would
be more reliable.

**Idea:** a `brain_verify` MCP tool that takes a memory path, extracts any `.py`/`.ts`/`.go`
identifiers or file paths from its body, and runs quick existence checks (ripgrep / file stat).
Returns a list of stale references. The model can then call `brain_save` to update or
`brain_forget` to delete.

Low priority — do this only when there's evidence of stale-memory bugs in practice.

### 3D — `claude-work` parity

Currently `claude-work` has the same brain setup as `claude-personal`. If over time it becomes clear
that work and personal memories should be partitioned (e.g., project memories should not
cross-pollinate), consider:

- A `BRAIN_ACCOUNT` scope that filters `brain_recall` results.
- Or a second vault for work, with a different `BRAIN_VAULT` env var per account.

Don't build this speculatively — only if the user asks for it.

### 3E — Activity log cleanup

`Brain/activity.md` grows unbounded (one line per assistant turn). Add a monthly log-rotation
helper that moves lines older than 90 days into `Brain/activity-archive/YYYY-MM.md`. Low urgency.

---

## Quick reference for "what do I do next"

If you're a fresh Claude session reading this and the user asks "what's next on the brain"
without more specifics:

1. Check this file for the current phase statuses.
2. Phase 2A (LMStudio) is the smallest next win.
3. Phase 2B (Windows) is the biggest next win if the user is about to switch to a Windows
   machine.
4. Phase 2C (Ollama) is a nice-to-have — only tackle if the user asks for it or there's time.
5. Phase 3 items are opportunistic — don't do them preemptively.

Always update this file's status line when completing a phase.
