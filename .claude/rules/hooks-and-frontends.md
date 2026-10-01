---
paths:
  - "hooks/*.py"
  - "mcp-server/brain_mcp/cli.py"
  - "mcp-server/brain_mcp/server.py"
  - "mcp-server/brain_mcp/transcript.py"
  - "pi/extensions/**"
  - "templates/skills/**"
  - "mcp-server/tests/test_stop_hook.py"
  - "mcp-server/tests/test_save_events.py"
  - "mcp-server/tests/test_agent_surface.py"
  - "mcp-server/tests/test_hook_stdio_utf8.py"
  - "mcp-server/tests/test_mcp_tool_threading.py"
---

# Rules: the hooks, the CLI agent surface, the MCP server, and the pi extension

Each rule has a case history in `docs/GOTCHAS.md` under the same lead phrase: what
happened, what was measured, and why the fix has the shape it has. Read that entry before
changing anything a rule guards.

- **The pre-approved CLI invocation is unattended: keep file-import options off it.**
  `BRAIN_AGENT_SURFACE=1`, set by the launcher, makes `cli._enforce_agent_surface` refuse
  `--file`, `--from-pi` and `--from-cherryd`; a new path-opening option joins
  `cli.RESTRICTED_OPTIONS`; `SKILL.md`'s `allowed-tools` stays narrowed. Permission rules are
  prefix matches and cannot enforce this.
- **Hooks read their payload through `_common.read_payload()` and nothing else** (it pins UTF-8
  first); hooks never create the vault; `append_activity` rotates `activity.md` at 2,000 lines.
- **`stop.py` reads the transcript from the end and learns about saves from the save-event
  record, never from shell commands.** Every model-facing save surface calls
  `vault.record_save_event()` after its write lands; the hook counts events at or after the
  turn's user-message timestamp for the payload's `session_id`, plus structured
  `brain_save`/`brain_checkpoint` tool_use blocks. `test_save_events.py` fails the build if
  `stop.py` looks at a `"Bash"` command again. Promise patterns require a Brain noun;
  emphasis-strip regexes are bounded; `re=Y` rows supersede the row before them;
  `transcript.SYSTEM_TURN_PREFIXES` is the one list of system-turn markers, and `sig` is
  computed on `user_authored_text()`, never the raw entry.
- **MCP tools run in a worker thread, one at a time, and nothing in a tool may wait on
  stdin.** `call_tool` hands `_call_tool_sync` to `asyncio.to_thread` under
  `_TOOL_LOCK`; `run()` calls `_preload_native_modules()` before `stdio_server`; every
  `subprocess` call in `brain_mcp` passes `stdin=` (`test_mcp_tool_threading.py`). On
  Windows a DLL load or an stdin-inheriting spawn in a tool thread hangs behind the
  reader's pending read.
- **The pi extension clears `BRAIN_AGENT_SURFACE` per spawn** (`execFile`, `shell:false`,
  explicit env), never in `process.env`, and only for its own spawns: the `brain_*` tools
  run under the gate (`toolEnv`) and build argv with `toolArgv` (`--name=value`, positionals
  after `--`). `BRAIN_PI_CMD` must be the venv executable.
