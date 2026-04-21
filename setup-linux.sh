#!/usr/bin/env bash
# setup-linux.sh — install the Brain wiring into a Claude Code config dir on Linux.
#
# Tested on Debian Trixie (Raspberry Pi OS) and Ubuntu 22.04. Requires Python 3.11+.
#
# On Ubuntu 22.04 the default `python3` is 3.10, which is too old. Install a newer
# interpreter first:
#     sudo add-apt-repository ppa:deadsnakes/ppa
#     sudo apt update
#     sudo apt install python3.11 python3.11-venv
#
# On Debian Trixie / Raspberry Pi OS Trixie (2025+), the default python3 is 3.13
# and only `python3-venv` may be missing:
#     sudo apt install python3-venv
#
# Usage:
#     ~/src/Ai-Brain/setup-linux.sh <claude-config-dir> <vault-path>
#
# Examples:
#     ~/src/Ai-Brain/setup-linux.sh ~/.claude-personal ~/Documents/Vaults/Ai-Brain
#     ~/src/Ai-Brain/setup-linux.sh ~/.claude-work ~/Documents/Vaults/Ai-Brain
#
# Idempotent: re-running updates the global CLAUDE.md, hook block, and MCP registration
# in place without disturbing other settings.

set -euo pipefail

if [ $# -lt 2 ]; then
  echo "usage: $0 <claude-config-dir> <vault-path>" >&2
  echo "example: $0 ~/.claude-personal ~/Documents/Vaults/Ai-Brain" >&2
  exit 1
fi

CLAUDE_DIR="${1/#\~/$HOME}"
VAULT_ROOT="${2/#\~/$HOME}"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOKS_DIR="$REPO_DIR/hooks"
MCP_SERVER_DIR="$REPO_DIR/mcp-server"
TEMPLATES_DIR="$REPO_DIR/templates"
VENV_PYTHON="$MCP_SERVER_DIR/.venv/bin/python"

if [ ! -d "$VAULT_ROOT" ]; then
  echo "ERROR: vault path does not exist: $VAULT_ROOT" >&2
  exit 1
fi

# Find a Python >= 3.11. Ubuntu 22.04's default python3 is 3.10, which
# brain-mcp rejects (pyproject.toml: requires-python = ">=3.11").
find_python() {
  local candidate ver major minor
  for candidate in python3.13 python3.12 python3.11 python3; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    ver=$("$candidate" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null) || continue
    major="${ver%.*}"
    minor="${ver#*.}"
    if [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

echo "Brain setup"
echo "  repo:         $REPO_DIR"
echo "  vault:        $VAULT_ROOT"
echo "  config dir:   $CLAUDE_DIR"
echo

# 1. Ensure the venv exists and brain-mcp is installed (non-editable; editable installs
#    use a .pth that doesn't always activate at startup, breaking imports from foreign cwds).
if [ ! -x "$VENV_PYTHON" ]; then
  if ! PY=$(find_python); then
    echo "ERROR: no python >= 3.11 found on PATH." >&2
    echo "       On Ubuntu 22.04, install from deadsnakes:" >&2
    echo "         sudo add-apt-repository ppa:deadsnakes/ppa" >&2
    echo "         sudo apt update && sudo apt install python3.11 python3.11-venv" >&2
    echo "       On Debian Trixie / Raspberry Pi OS, install the default:" >&2
    echo "         sudo apt install python3 python3-venv" >&2
    exit 2
  fi
  echo "[1/6] creating Python venv at $MCP_SERVER_DIR/.venv (using $PY)"
  VENV_ERR="$(mktemp)"
  trap 'rm -f "$VENV_ERR"' EXIT
  if ! "$PY" -m venv "$MCP_SERVER_DIR/.venv" 2>"$VENV_ERR"; then
    cat "$VENV_ERR" >&2
    if grep -qiE "ensurepip|python3-venv" "$VENV_ERR"; then
      echo "       ↑ this usually means the venv package isn't installed. Install it with:" >&2
      echo "         sudo apt install $(basename "$PY")-venv" >&2
    fi
    exit 2
  fi
  "$MCP_SERVER_DIR/.venv/bin/pip" install --quiet --upgrade pip
fi
echo "[1/6] installing brain-mcp into venv"
"$MCP_SERVER_DIR/.venv/bin/pip" install --quiet --force-reinstall --no-deps "$MCP_SERVER_DIR" >/dev/null
"$MCP_SERVER_DIR/.venv/bin/pip" install --quiet "$MCP_SERVER_DIR" >/dev/null

# 2. Sanity check the Python module loads from a foreign cwd
if ! ( cd /tmp && BRAIN_VAULT="$VAULT_ROOT" "$VENV_PYTHON" -c "from brain_mcp import vault, server, embed, compact" 2>/dev/null ); then
  echo "ERROR: brain_mcp module failed to import from a foreign cwd. Aborting." >&2
  exit 2
fi

# 2b. Warm up the fastembed model so the first brain_recall isn't a 30s stall.
#     On a Raspberry Pi this download can take a minute or two on a slow SD card;
#     it's a one-time cost cached under ~/.cache/fastembed.
echo "      warming up embedding model (one-time ONNX download, ~130MB)…"
BRAIN_VAULT="$VAULT_ROOT" "$VENV_PYTHON" -c "from brain_mcp.embed import EmbedIndex; EmbedIndex.warm()" \
  || echo "WARNING: embed warm-up failed; vector recall will fall back to ripgrep until resolved." >&2

# 3. Ensure the vault has a Brain/ subdir to write into
mkdir -p "$VAULT_ROOT/Brain/user" "$VAULT_ROOT/Brain/feedback" "$VAULT_ROOT/Brain/references" "$VAULT_ROOT/Brain/projects"

mkdir -p "$CLAUDE_DIR/skills/brain"

# 4. Drop the global CLAUDE.md, substituting __BRAIN_VAULT__
echo "[2/6] writing $CLAUDE_DIR/CLAUDE.md"
sed "s|__BRAIN_VAULT__|$VAULT_ROOT|g" \
  "$TEMPLATES_DIR/global-CLAUDE.md" > "$CLAUDE_DIR/CLAUDE.md"

# 5. Drop the brain skill
echo "[3/6] writing $CLAUDE_DIR/skills/brain/SKILL.md"
cp "$TEMPLATES_DIR/skills/brain/SKILL.md" "$CLAUDE_DIR/skills/brain/SKILL.md"

# 6. Merge hooks block into settings.json (in-place, preserving other keys)
echo "[4/6] merging hooks into $CLAUDE_DIR/settings.json"
SETTINGS_FILE="$CLAUDE_DIR/settings.json"
[ -f "$SETTINGS_FILE" ] || echo '{}' > "$SETTINGS_FILE"

HOOKS_TEMPLATE="$TEMPLATES_DIR/settings.hooks.json"

"$VENV_PYTHON" - "$SETTINGS_FILE" "$HOOKS_TEMPLATE" "$VENV_PYTHON" "$HOOKS_DIR" "$VAULT_ROOT" <<'PY'
import json, sys
settings_path, template_path, brain_python, brain_hooks, brain_vault = sys.argv[1:6]

with open(settings_path, "r", encoding="utf-8") as f:
    try:
        settings = json.load(f)
    except json.JSONDecodeError:
        settings = {}

with open(template_path, "r", encoding="utf-8") as f:
    template = f.read()

template = (
    template.replace("__BRAIN_PYTHON__", brain_python)
            .replace("__BRAIN_HOOKS__", brain_hooks)
            .replace("__BRAIN_VAULT__", brain_vault)
)
hooks_block = json.loads(template)["hooks"]


def _is_brain_command(cmd: str) -> bool:
    """Detect hook commands we own, so stale entries (e.g. a UserPromptSubmit
    pointing at a deleted script) get pruned when the template shrinks."""
    if not isinstance(cmd, str):
        return False
    return (
        f"BRAIN_VAULT={brain_vault}" in cmd
        or "BRAIN_VAULT=" in cmd and brain_hooks in cmd
        or brain_hooks in cmd
    )


# Prune any Brain-owned hook entries left over from older template versions
# before re-applying the current template. Without this, settings.json
# accumulates orphan events whose commands exec scripts that no longer exist.
existing = settings.get("hooks", {}) or {}
if not isinstance(existing, dict):
    existing = {}
for event in list(existing.keys()):
    groups = existing.get(event) or []
    if not isinstance(groups, list):
        continue
    pruned_groups = []
    for group in groups:
        if not isinstance(group, dict):
            pruned_groups.append(group)
            continue
        inner = group.get("hooks") or []
        kept = [h for h in inner if not (isinstance(h, dict) and _is_brain_command(h.get("command", "")))]
        if kept:
            new_group = dict(group)
            new_group["hooks"] = kept
            pruned_groups.append(new_group)
    if pruned_groups:
        existing[event] = pruned_groups
    else:
        del existing[event]
settings["hooks"] = existing

for event, definition in hooks_block.items():
    settings["hooks"][event] = definition

with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
PY

# 7. Register the brain MCP server with USER scope via the claude CLI.
#    User-scoped MCP servers live in ~/.claude.json under the config dir, and
#    `claude mcp add --scope user` is the supported way to write them. Dropping a
#    .mcp.json file in the config dir does NOT work — that file is only read from
#    the current project dir.
echo "[5/6] registering brain MCP server (user scope)"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
MCP_REGISTERED=0
MCP_FAIL_REASON=""

# `claude mcp add --scope user` writes to $CLAUDE_CONFIG_DIR/.claude.json when
# the env var is set, but to $HOME/.claude.json when it isn't — two different
# files. When $CLAUDE_DIR is the default location, we MUST leave the env var
# unset so the write lands where a plain `claude` invocation later reads from.
# For custom config dirs (~/.claude-personal, ~/.claude-work) each has its own
# sibling .claude.json inside it, so the env var is correct and required.
_canonical_path() {
  if [ -d "$1" ]; then ( cd "$1" && pwd -P ); else printf '%s' "${1%/}"; fi
}
if [ "$(_canonical_path "$CLAUDE_DIR")" = "$(_canonical_path "$HOME/.claude")" ]; then
  IS_DEFAULT_TARGET=1
else
  IS_DEFAULT_TARGET=0
fi
claude_cli() {
  if [ "$IS_DEFAULT_TARGET" = "1" ]; then
    "$CLAUDE_BIN" "$@"
  else
    CLAUDE_CONFIG_DIR="$CLAUDE_DIR" "$CLAUDE_BIN" "$@"
  fi
}

if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
  MCP_FAIL_REASON="'$CLAUDE_BIN' not on PATH"
else
  claude_cli mcp remove brain --scope user >/dev/null 2>&1 || true
  # Capture both streams so a silent CLI failure doesn't vanish into /dev/null.
  MCP_ADD_OUT="$(claude_cli mcp add brain --scope user \
      -e "BRAIN_VAULT=$VAULT_ROOT" \
      -- "$VENV_PYTHON" -m brain_mcp 2>&1)" || MCP_ADD_RC=$?
  MCP_ADD_RC="${MCP_ADD_RC:-0}"
  if [ "$MCP_ADD_RC" -ne 0 ]; then
    MCP_FAIL_REASON="'claude mcp add' exited $MCP_ADD_RC: $MCP_ADD_OUT"
  elif ! claude_cli mcp list 2>/dev/null | grep -q "^brain"; then
    MCP_FAIL_REASON="'claude mcp add' returned success but 'brain' not in 'claude mcp list'"
  else
    MCP_REGISTERED=1
    echo "       ✓ registered as user-scope MCP server in $CLAUDE_DIR"
  fi
fi

# 8. Clean up any obsolete .mcp.json from earlier setup runs (it never worked).
echo "[6/6] cleanup"
rm -f "$CLAUDE_DIR/.mcp.json"

echo
if [ "$MCP_REGISTERED" -eq 1 ]; then
  echo "✓ Brain installed in $CLAUDE_DIR"
else
  echo "✓ Brain files installed in $CLAUDE_DIR"
  echo
  echo "✗ MCP SERVER NOT REGISTERED — brain_* tools will NOT appear in Claude Code."
  echo "   reason: $MCP_FAIL_REASON"
  echo
  echo "   To fix, ensure Claude Code is installed and on PATH, then register manually:"
  if [ "$IS_DEFAULT_TARGET" = "1" ]; then
    echo "     $CLAUDE_BIN mcp add brain --scope user \\"
    echo "         -e BRAIN_VAULT=$VAULT_ROOT -- $VENV_PYTHON -m brain_mcp"
  else
    echo "     CLAUDE_CONFIG_DIR=$CLAUDE_DIR $CLAUDE_BIN mcp add brain --scope user \\"
    echo "         -e BRAIN_VAULT=$VAULT_ROOT -- $VENV_PYTHON -m brain_mcp"
  fi
  echo "   Or re-run this script with CLAUDE_BIN pointing at the claude binary:"
  echo "     CLAUDE_BIN=\$(which claude) $0 $CLAUDE_DIR $VAULT_ROOT"
fi
echo
echo "Next steps:"
echo "  1. Open a new Claude Code session in any project."
echo "  2. The SessionStart hook should preload the brain context automatically."
echo "  3. The brain_* MCP tools should appear in your tool list."
echo "  4. To register with LMStudio, point its MCP settings at:"
echo "       command: $VENV_PYTHON"
echo "       args:    -m brain_mcp"
echo "       env:     BRAIN_VAULT=$VAULT_ROOT"
