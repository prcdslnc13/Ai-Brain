---
paths:
  - "brain-setup.py"
  - "brain-uninstall.py"
  - "brain_settings_merge.py"
  - "templates/**"
  - "mcp-server/tests/test_installer_*.py"
  - "mcp-server/tests/test_settings_merge.py"
  - "mcp-server/tests/test_uninstall_*.py"
---

# Rules: the installer, the uninstaller, and the templates they write

Each rule has a case history in `docs/GOTCHAS.md` under the same lead phrase: what
happened, what was measured, and why the fix has the shape it has. Read that entry before
changing anything a rule guards.

- **Installers APPEND hook groups (prune-first) and refuse an unparseable `settings.json`.**
  Everything goes through `brain_settings_merge.py` — never re-inline it. Missing or
  whitespace-only means `{}`; anything else unparseable is a hard stop with a nonzero exit and a
  summary of what did land. Writes are backup + same-dir temp + `os.replace`, only when content
  changes.
- **`brain-setup.py` verifies MCP registration by reading the target dir's `.claude.json`
  `mcpServers`**, not `claude mcp list`. Preload present but tools absent means the wrong config
  dir; the default dir's file is `~/.claude.json`.
- **Every file read/write in the installer passes `encoding="utf-8"`** (proved by an AST-parsed
  parity test; `write_windows_launch_cmd` is the deliberate exception). Verify template output
  with a mojibake byte count, never by eye.
- **User-scoped MCP servers are registered with `claude mcp add --scope user`**, never by
  dropping a `.mcp.json`.
- **Install turns Claude Code's auto memory off, and ownership lives in a sidecar.**
  `brain_settings_merge.disable_auto_memory` writes `autoMemoryEnabled: false`;
  `.brain-auto-memory.json` holds the pre-install state, is written only after `settings.json`
  lands and never over an existing one; uninstall restores it only if the key still reads
  `false`. A user's own `false` gets no marker and is never claimed.
- **Hooks set `BRAIN_VAULT` in the command string itself** — env prefix on POSIX,
  `brain-launch.cmd` on Windows. Preserve the platform's pattern.
- **The model-facing command is a Python launcher (`brain-agent.py`), never a `.cmd`, never an
  env prefix.** `brain_cmd_token()` returns two quoted paths; `brain-launch.cmd` serves hooks
  only, with fixed arguments. Any future wrapper must receive `argv` already split.
  `is_brain_permission_rule` recognises every shape ever written so re-runs prune the old ones.
- **Installer-written files carry `MANAGED_MARKER`, unmarked destinations are backed up first,
  and files cmd.exe reads are OEM-encoded.** `write_managed_text` is the one writer; the skill's
  marker sits after its frontmatter; hook templates quote every placeholder and substitute on
  parsed JSON; `brain-launch.cmd` doubles `%` and stays ASCII; `cleanup()` deletes
  `<config>/.mcp.json` only when it is a Brain-only registration.
