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

# 1. Ensure the venv exists and brain-mcp is installed (non-editable)
if [ ! -x "$VENV_PYTHON" ]; then
  echo "[1/6] creating Python venv at $MCP_SERVER_DIR/.venv"
  python3 -m venv "$MCP_SERVER_DIR/.venv"
  "$MCP_SERVER_DIR/.venv/bin/pip" install --quiet --upgrade pip
fi
echo "[1/6] installing brain-mcp into venv"
"$MCP_SERVER_DIR/.venv/bin/pip" install --quiet --force-reinstall --no-deps "$MCP_SERVER_DIR" >/dev/null
"$MCP_SERVER_DIR/.venv/bin/pip" install --quiet "$MCP_SERVER_DIR" >/dev/null

# 2. Sanity check the Python module loads from a foreign cwd
if ! ( cd /tmp && BRAIN_VAULT="$VAULT_ROOT" "$VENV_PYTHON" -c "from brain_mcp import vault, server" 2>/dev/null ); then
  echo "ERROR: brain_mcp module failed to import from a foreign cwd. Aborting." >&2
  exit 2
fi

mkdir -p "$CLAUDE_DIR/skills/brain"

# 3. Drop the global CLAUDE.md, substituting __BRAIN_VAULT__
echo "[2/6] writing $CLAUDE_DIR/CLAUDE.md"
sed "s|__BRAIN_VAULT__|$VAULT_ROOT|g" \
  "$SETUP_DIR/templates/global-CLAUDE.md" > "$CLAUDE_DIR/CLAUDE.md"

# 4. Drop the brain skill
echo "[3/6] writing $CLAUDE_DIR/skills/brain/SKILL.md"
cp "$SETUP_DIR/templates/skills/brain/SKILL.md" "$CLAUDE_DIR/skills/brain/SKILL.md"

# 5. Merge hooks block into settings.json (in-place, preserving other keys)
echo "[4/6] merging hooks into $CLAUDE_DIR/settings.json"
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

# 6. Register the brain MCP server with USER scope via the claude CLI.
#    We can't write .mcp.json by hand for user scope — Claude Code only reads .mcp.json
#    from the current project dir. User-scoped servers live in ~/.claude.json under the
#    config dir, and `claude mcp add --scope user` is the supported way to write them.
echo "[5/6] registering brain MCP server (user scope)"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
  echo "WARNING: '$CLAUDE_BIN' not on PATH; skipping MCP registration." >&2
  echo "         Re-run with CLAUDE_BIN=/path/to/claude $0 $CLAUDE_DIR" >&2
else
  # Remove any previous brain registration in this scope to keep idempotency clean.
  CLAUDE_CONFIG_DIR="$CLAUDE_DIR" "$CLAUDE_BIN" mcp remove brain --scope user >/dev/null 2>&1 || true
  CLAUDE_CONFIG_DIR="$CLAUDE_DIR" "$CLAUDE_BIN" mcp add brain --scope user \
    -e "BRAIN_VAULT=$VAULT_ROOT" \
    -- "$VENV_PYTHON" -m brain_mcp >/dev/null
  echo "       ✓ registered as user-scope MCP server in $CLAUDE_DIR"
fi

# 7. Clean up the obsolete .mcp.json from earlier setup runs (it never worked).
echo "[6/6] cleanup"
rm -f "$CLAUDE_DIR/.mcp.json"

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
