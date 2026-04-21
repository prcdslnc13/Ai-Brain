# LMStudio setup

How to wire the Brain MCP server into [LMStudio](https://lmstudio.ai) so local,
tool-capable models can call `brain_*` tools the same way Claude Code does.

## Prerequisites

1. **LMStudio 0.3.x or newer** with MCP client support. If you don't see an MCP-related
   section anywhere in Settings, upgrade first.
2. **A tool-capable model** loaded in LMStudio. Known working on Apple Silicon (verified 2026-04-21):
   - Qwen 3.5 family (Qwen3.5-9B-GGUF and larger)
   - Gemma 4 family — `gemma-4-31B-it-GGUF` and `gemma-4-E4B-it-GGUF` both verified; `gemma-4-26B-A4B-it-GGUF` untested
   - Qwen 2.5 family, Llama 3.1 8B+, Mistral Small (22B), Hermes 3 (earlier reports, not re-verified)

   Older models without tool/function-calling (base Llama 2, Gemma 2, pre-2024 Mistral)
   won't see the `brain_*` tools at all. If unsure whether a model is tool-capable, try it —
   a tool-capable model will call `brain_session_start` when asked; a non-tool model will
   respond in prose without invoking any tool. For non-tool models, use the `brain-prep`
   CLI to dump the preload bundle as markdown and paste it into the system prompt.
3. **The Brain MCP server already installed on this machine.** That means you have run
   either `setup-mac.sh` or `setup-windows.ps1` (see `WINDOWS-SETUP.md`) at least once, so
   `mcp-server/.venv` exists and `brain_mcp` imports cleanly.
4. **The Obsidian vault synced down** to this machine. The MCP server reads from the
   vault, so an empty or missing vault means empty `brain_recall` results.

## Values you'll need

The MCP server is a stdio process. LMStudio launches it with a command, args, and environment.
Use the full absolute paths for your machine:

| Field   | macOS                                                              | Windows                                                                      |
|---------|--------------------------------------------------------------------|------------------------------------------------------------------------------|
| Command | `/Users/<you>/src/Ai-Brain/mcp-server/.venv/bin/python`             | `C:\src\Ai-Brain\mcp-server\.venv\Scripts\python.exe`                         |
| Args    | `-m brain_mcp`                                                     | `-m brain_mcp`                                                               |
| Env     | `BRAIN_VAULT=/Users/<you>/Documents/Vaults/Ai-Brain`               | `BRAIN_VAULT=C:\Users\<you>\Documents\Vaults\Ai-Brain`                       |

Both `setup-mac.sh` and `setup-windows.ps1` print these exact values at the end of a
successful run — copy from there rather than retyping.

## Add the server to LMStudio

LMStudio's MCP config is JSON-based and lives in the app's settings. The UI path varies by
version, but the shape of the config is stable. You have two options — pick whichever your
LMStudio build exposes.

### Option A — JSON editor (recommended)

1. Open LMStudio.
2. Go to **Settings** (gear icon, bottom-left) → look for a section labelled **Program**,
   **Developer**, or **Model Context Protocol**. Some builds nest it under **Developer Mode**
   which must be enabled first.
3. Find the "Edit mcp.json" button (or equivalent) and add an entry under `mcpServers`:

```json
{
  "mcpServers": {
    "brain": {
      "command": "/Users/<you>/src/Ai-Brain/mcp-server/.venv/bin/python",
      "args": ["-m", "brain_mcp"],
      "env": {
        "BRAIN_VAULT": "/Users/<you>/Documents/Vaults/Ai-Brain"
      }
    }
  }
}
```

On Windows, use backslash-escaped absolute paths:

```json
{
  "mcpServers": {
    "brain": {
      "command": "C:\\src\\Ai-Brain\\mcp-server\\.venv\\Scripts\\python.exe",
      "args": ["-m", "brain_mcp"],
      "env": {
        "BRAIN_VAULT": "C:\\Users\\<you>\\Documents\\Vaults\\Ai-Brain"
      }
    }
  }
}
```

4. Save the JSON and restart any open chat (some builds require a full app restart to pick
   up new MCP servers).

### Option B — form fields

If your LMStudio build only shows a form (Name / Command / Arguments / Environment) instead
of a JSON editor, fill it in with:

- **Name:** `brain`
- **Command:** the full path to the venv python (see the table above)
- **Arguments:** `-m brain_mcp` (enter as two separate tokens if the form splits them)
- **Environment:** `BRAIN_VAULT` = the full path to your Obsidian vault

Save and restart the chat.

## Verify it works

1. Load a tool-capable model from the Prerequisites list (Qwen3.5-9B-GGUF is a safe first-run default on Apple Silicon).
2. Open a new chat. If LMStudio shows an "available tools" indicator anywhere in the chat UI,
   confirm all eight tools appear: `brain_session_start`, `brain_recall`, `brain_save`,
   `brain_list`, `brain_forget`, `brain_checkpoint`, `brain_stats`, `brain_doctor`.
3. Ask: **"What do you know about me? Call brain_session_start first."**
   The model should call the tool, read the preload bundle, and summarize your profile
   (email, preferred IDEs, multi-machine setup).
4. Ask: **"Search the brain for my preferred IDEs."**
   The model should call `brain_recall` with a query like `"IDE"` or `"preferred IDEs"`
   and return the Qt Creator + VS Code memory written from Claude Code.
5. To close the loop, ask the model to save something: **"Save a user memory that I prefer
   dark mode in all my tools."** Verify a new file appears in
   `<vault>/Brain/user/` and that Obsidian syncs it to your other machines within a minute
   or two.

## Smoke-test the server standalone (no LMStudio)

If LMStudio can't start the server, it's almost always because the command, args, or env
are wrong. Run the same stdio handshake yourself to isolate LMStudio from the MCP server:

macOS:

```bash
BRAIN_VAULT=~/Documents/Vaults/Ai-Brain \
  ~/src/Ai-Brain/mcp-server/.venv/bin/python -m brain_mcp <<'EOF'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
EOF
```

Windows (PowerShell):

```powershell
$env:BRAIN_VAULT = "$env:USERPROFILE\Documents\Vaults\Ai-Brain"
@"
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
"@ | & "C:\src\Ai-Brain\mcp-server\.venv\Scripts\python.exe" -m brain_mcp
```

Expected: two JSON-RPC response lines on stdout. The first reports `serverInfo.name: "brain"`,
the second lists eight tools. If you see that, the server is healthy and the problem is
entirely on the LMStudio side — check the paths in your `mcp.json` byte for byte.

## Troubleshooting

### The `brain_*` tools don't appear in chat

- Is the model tool-capable? Non-tool models silently ignore MCP servers. Try Qwen2.5-7B.
- Did you restart the chat (or the app) after saving `mcp.json`? LMStudio doesn't hot-reload.
- Check LMStudio's log/console for MCP server errors. Wrong path, wrong python, missing
  `BRAIN_VAULT` — they all show up there.

### The server starts but `brain_recall` returns nothing

Your `BRAIN_VAULT` points at a real directory but it's either empty or not the vault. Verify:

```bash
ls ~/Documents/Vaults/Ai-Brain/Brain/user     # macOS
dir "%USERPROFILE%\Documents\Vaults\Ai-Brain\Brain\user"   # Windows
```

You should see at least `profile.md` and `preferred-ides.md`. If the directory is empty,
Obsidian Sync hasn't finished pulling the vault down — let it finish, then retry.

### Editable-install regression

If you see `ModuleNotFoundError: No module named 'brain_mcp'` in the LMStudio log even though
the venv exists, someone re-ran `pip install -e .` on `mcp-server/` somewhere. Re-run
`setup-mac.sh` or `setup-windows.ps1`; both scripts force-reinstall non-editable.

### Permissions on macOS

First-time launches may be blocked by Gatekeeper if the venv python is quarantined. Run it
once from a terminal (`.venv/bin/python --version`) to clear the quarantine, then restart
LMStudio.

## Also see

- `WINDOWS-SETUP.md` — how to get `mcp-server\.venv` populated on Windows in the first place.
- `CLAUDE.md` — architecture overview and the list of `brain_*` tools.
- `ROADMAP.md` — Phase 2A for the broader LMStudio integration plan and Phase 2C for Ollama.
