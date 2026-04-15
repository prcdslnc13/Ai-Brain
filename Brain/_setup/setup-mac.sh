#!/usr/bin/env bash
# setup-mac.sh — install the Brain wiring into a Claude Code config dir on macOS.
#
# Usage:
#     Brain/_setup/setup-mac.sh ~/.claude-personal
#     Brain/_setup/setup-mac.sh ~/.claude-work
#
# Idempotent: re-running updates the global CLAUDE.md, hook block, and MCP registration
# in place without disturbing other settings.

set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: $0 <claude-config-dir>" >&2
  echo "examples:" >&2
  echo "  $0 ~/.claude-personal" >&2
  echo "  $0 ~/.claude-work" >&2
  exit 1
fi

CLAUDE_DIR="${1/#\~/$HOME}"
SETUP_DIR="$(cd "$(dirname "$0")" && pwd)"
VAULT_ROOT="$(cd "$SETUP_DIR/../.." && pwd)"
BRAIN_DIR="$VAULT_ROOT/Brain"
HOOKS_DIR="$BRAIN_DIR/_setup/hooks"
MCP_SERVER_DIR="$BRAIN_DIR/_setup/mcp-server"
VENV_PYTHON="$MCP_SERVER_DIR/.venv/bin/python"

echo "Brain setup"
echo "  vault:        $VAULT_ROOT"
echo "  config dir:   $CLAUDE_DIR"
echo

# 1. Ensure the venv exists and brain-mcp is installed
if [ ! -x "$VENV_PYTHON" ]; then
  echo "[1/5] creating Python venv at $MCP_SERVER_DIR/.venv"
  python3 -m venv "$MCP_SERVER_DIR/.venv"
  "$MCP_SERVER_DIR/.venv/bin/pip" install --quiet --upgrade pip
  "$MCP_SERVER_DIR/.venv/bin/pip" install --quiet -e "$MCP_SERVER_DIR"
else
  echo "[1/5] venv already exists; ensuring brain-mcp is installed"
  "$MCP_SERVER_DIR/.venv/bin/pip" install --quiet -e "$MCP_SERVER_DIR" >/dev/null 2>&1 || true
fi

# 2. Sanity check the Python module loads
if ! BRAIN_VAULT="$VAULT_ROOT" "$VENV_PYTHON" -c "from brain_mcp import vault, server" 2>/dev/null; then
  echo "ERROR: brain_mcp module failed to import. Aborting." >&2
  exit 2
fi

mkdir -p "$CLAUDE_DIR/skills/brain"

# 3. Drop the global CLAUDE.md, substituting __BRAIN_VAULT__
echo "[2/5] writing $CLAUDE_DIR/CLAUDE.md"
sed "s|__BRAIN_VAULT__|$VAULT_ROOT|g" \
  "$SETUP_DIR/templates/global-CLAUDE.md" > "$CLAUDE_DIR/CLAUDE.md"

# 4. Drop the brain skill
echo "[3/5] writing $CLAUDE_DIR/skills/brain/SKILL.md"
cp "$SETUP_DIR/templates/skills/brain/SKILL.md" "$CLAUDE_DIR/skills/brain/SKILL.md"

# 5. Merge hooks block into settings.json (in-place, preserving other keys)
echo "[4/5] merging hooks into $CLAUDE_DIR/settings.json"
SETTINGS_FILE="$CLAUDE_DIR/settings.json"
[ -f "$SETTINGS_FILE" ] || echo '{}' > "$SETTINGS_FILE"

HOOKS_TEMPLATE="$SETUP_DIR/templates/settings.hooks.json"

"$VENV_PYTHON" - "$SETTINGS_FILE" "$HOOKS_TEMPLATE" "$VENV_PYTHON" "$HOOKS_DIR" <<'PY'
import json, sys
settings_path, template_path, brain_python, brain_hooks = sys.argv[1:5]

with open(settings_path, "r", encoding="utf-8") as f:
    try:
        settings = json.load(f)
    except json.JSONDecodeError:
        settings = {}

with open(template_path, "r", encoding="utf-8") as f:
    template = f.read()

template = template.replace("__BRAIN_PYTHON__", brain_python).replace("__BRAIN_HOOKS__", brain_hooks)
hooks_block = json.loads(template)["hooks"]

settings.setdefault("hooks", {})
for event, definition in hooks_block.items():
    settings["hooks"][event] = definition  # overwrite the brain block; leave other events alone

with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
PY

# 6. Merge MCP registration into .mcp.json
echo "[5/5] merging brain MCP server into $CLAUDE_DIR/.mcp.json"
MCP_FILE="$CLAUDE_DIR/.mcp.json"
[ -f "$MCP_FILE" ] || echo '{}' > "$MCP_FILE"

MCP_TEMPLATE="$SETUP_DIR/templates/mcp.json"

"$VENV_PYTHON" - "$MCP_FILE" "$MCP_TEMPLATE" "$VENV_PYTHON" "$VAULT_ROOT" <<'PY'
import json, sys
mcp_path, template_path, brain_python, brain_vault = sys.argv[1:5]

with open(mcp_path, "r", encoding="utf-8") as f:
    try:
        existing = json.load(f)
    except json.JSONDecodeError:
        existing = {}

with open(template_path, "r", encoding="utf-8") as f:
    template = f.read()

template = template.replace("__BRAIN_PYTHON__", brain_python).replace("__BRAIN_VAULT__", brain_vault)
new_block = json.loads(template)

existing.setdefault("mcpServers", {})
for name, definition in new_block["mcpServers"].items():
    existing["mcpServers"][name] = definition

with open(mcp_path, "w", encoding="utf-8") as f:
    json.dump(existing, f, indent=2)
    f.write("\n")
PY

echo
echo "✓ Brain installed in $CLAUDE_DIR"
echo
echo "Next steps:"
echo "  1. Open a new Claude Code session in any project."
echo "  2. The SessionStart hook should preload the brain context automatically."
echo "  3. The brain_* MCP tools should appear in your tool list."
echo "  4. To register with LMStudio, point its MCP settings at:"
echo "       command: $VENV_PYTHON"
echo "       args:    -m brain_mcp"
echo "       env:     BRAIN_VAULT=$VAULT_ROOT"
