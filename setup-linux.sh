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
#     ~/src/Ai-Brain/setup-linux.sh <claude-config-dir> <vault-path> [--with-mcp]
#
# Examples:
#     ~/src/Ai-Brain/setup-linux.sh ~/.claude           ~/Vaults/Ai-Brain
#     ~/src/Ai-Brain/setup-linux.sh ~/.claude-personal  ~/Vaults/Ai-Brain
#     ~/src/Ai-Brain/setup-linux.sh ~/.claude-work      ~/Vaults/Ai-Brain --with-mcp
#
# The config dir can be any path. Single-account users use ~/.claude; multi-account
# users pick their own names (anything starting with .claude is auto-discovered by
# the cross-platform brain-setup.py wizard).
#
# By default the Brain is driven through the `brain` CLI (via the skill and global
# CLAUDE.md) — no MCP server is registered with Claude Code, saving the ~3k tokens
# of tool schemas every session. Pass --with-mcp to also register the MCP server.
# Without --with-mcp, any existing user-scope 'brain' MCP registration is REMOVED
# so the token saving actually lands.
#
# Idempotent: re-running updates the global CLAUDE.md, skill, hook block, and MCP
# registration in place. Other settings.json keys are left alone, and so are
# third-party hooks registered for the same events -- the Brain hook groups are
# APPENDED to whatever is already there, not assigned over the event. If
# settings.json cannot be parsed, the merge REFUSES and this script exits 3 rather
# than replace the file.

set -euo pipefail

if [ $# -lt 2 ]; then
  echo "usage: $0 <claude-config-dir> <vault-path> [--with-mcp]" >&2
  echo "example: $0 ~/.claude-personal ~/Vaults/Ai-Brain" >&2
  exit 1
fi

CLAUDE_DIR="${1/#\~/$HOME}"
VAULT_ROOT="${2/#\~/$HOME}"
shift 2
WITH_MCP=0
for arg in "$@"; do
  case "$arg" in
    --with-mcp) WITH_MCP=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 1 ;;
  esac
done
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOKS_DIR="$REPO_DIR/hooks"
MCP_SERVER_DIR="$REPO_DIR/mcp-server"
TEMPLATES_DIR="$REPO_DIR/templates"
VENV_PYTHON="$MCP_SERVER_DIR/.venv/bin/python"
VENV_BRAIN="$MCP_SERVER_DIR/.venv/bin/brain"
# The exact invocation substituted for __BRAIN_CMD__ in the templates: env
# prefix + absolute path, so the model can run it from any cwd via Bash.
#
# BRAIN_AGENT_SURFACE=1 leads the prefix and is load-bearing. This string is what
# setup pre-approves in permissions.allow, and pre-approval is a PREFIX match --
# so every unattended invocation carries the flag, and the CLI refuses --file,
# --from-pi and --from-cherryd under it. Without that, a prompt-injected model
# could read any local file into the vault and recall it back, with no human in
# the loop. Operators (and pi) call "$VENV_BRAIN" directly and keep the full CLI.
BRAIN_CMD="BRAIN_AGENT_SURFACE=1 BRAIN_VAULT=\"$VAULT_ROOT\" \"$VENV_BRAIN\""

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
#     embed.py pins a stable, machine-local cache (~/.cache/ai-brain/fastembed) so this
#     one-time download lands there and every later load is offline (no per-recall HF
#     round-trip — that uncapped network call caused a recall hang). On a Raspberry Pi
#     this download can take a minute or two on a slow SD card. Override the location
#     with BRAIN_EMBED_CACHE; force-online model updates with BRAIN_EMBED_OFFLINE=0.
echo "      warming up embedding model (one-time ONNX download, ~65MB) into ~/.cache/ai-brain/fastembed…"
BRAIN_VAULT="$VAULT_ROOT" "$VENV_PYTHON" -c "from brain_mcp.embed import EmbedIndex; EmbedIndex.warm()" \
  || echo "WARNING: embed warm-up failed; vector recall will fall back to ripgrep until resolved." >&2

# 3. Ensure the vault has a Brain/ subdir to write into
mkdir -p "$VAULT_ROOT/Brain/user" "$VAULT_ROOT/Brain/feedback" "$VAULT_ROOT/Brain/references" "$VAULT_ROOT/Brain/projects"

mkdir -p "$CLAUDE_DIR/skills/brain"

# 4. Drop the global CLAUDE.md, substituting __BRAIN_VAULT__ and __BRAIN_CMD__
echo "[2/6] writing $CLAUDE_DIR/CLAUDE.md"
sed -e "s|__BRAIN_VAULT__|$VAULT_ROOT|g" -e "s|__BRAIN_CMD__|$BRAIN_CMD|g" \
  "$TEMPLATES_DIR/global-CLAUDE.md" > "$CLAUDE_DIR/CLAUDE.md"

# 5. Drop the brain skill, substituting __BRAIN_CMD__
echo "[3/6] writing $CLAUDE_DIR/skills/brain/SKILL.md"
sed "s|__BRAIN_CMD__|$BRAIN_CMD|g" \
  "$TEMPLATES_DIR/skills/brain/SKILL.md" > "$CLAUDE_DIR/skills/brain/SKILL.md"

# 6. Merge the Brain hook block into settings.json.
#    The merge itself lives in brain_settings_merge.py — the ONE implementation
#    shared by all four installers and all four uninstallers, so this algorithm
#    cannot fork again. It APPENDS our hook groups to whatever third-party hooks
#    already exist for the same events (assigning over the event silently deleted
#    a user's own SessionStart/Stop/PreCompact hook), REFUSES to rewrite an
#    unparseable settings.json instead of replacing it with {}, takes a timestamped
#    backup, and writes atomically. Exit 3 means "refused, file untouched".
echo "[4/6] merging hooks into $CLAUDE_DIR/settings.json"
SETTINGS_FILE="$CLAUDE_DIR/settings.json"
HOOKS_TEMPLATE="$TEMPLATES_DIR/settings.hooks.json"

MERGE_ARGS=(merge
  --settings "$SETTINGS_FILE"
  --template "$HOOKS_TEMPLATE"
  --brain-cmd "$BRAIN_CMD"
  --brain-python "$VENV_PYTHON"
  --brain-hooks "$HOOKS_DIR"
  --brain-vault "$VAULT_ROOT")
if ! "$VENV_PYTHON" "$REPO_DIR/brain_settings_merge.py" "${MERGE_ARGS[@]}"; then
  echo >&2
  echo "✗ PARTIAL INSTALLATION in $CLAUDE_DIR" >&2
  echo "   These steps DID succeed:" >&2
  echo "     - the venv and brain-mcp install" >&2
  echo "     - $CLAUDE_DIR/CLAUDE.md" >&2
  echo "     - $CLAUDE_DIR/skills/brain/SKILL.md" >&2
  echo "   These did NOT:" >&2
  echo "     - the hook wiring (no preload, no checkpoints, no stop-gate)" >&2
  echo "     - the Bash(<brain cmd>:*) permission rule (proactive saves will prompt)" >&2
  echo "     - MCP registration / cleanup (not attempted)" >&2
  echo "   Repair $SETTINGS_FILE and re-run this script." >&2
  exit 3
fi

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
# For custom config dirs (e.g. ~/.claude-personal, ~/.claude-work) each has
# its own sibling .claude.json inside it, so the env var is correct and required.
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

if [ "$WITH_MCP" -eq 1 ]; then
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
else
  # CLI-first default: the skill + global CLAUDE.md drive the `brain` CLI, so
  # the MCP server would only cost ~3k tokens of schemas per session. Remove
  # any stale registration so the saving actually lands.
  if command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
    claude_cli mcp remove brain --scope user >/dev/null 2>&1 || true
    echo "       skipped (CLI-first default); removed any stale user-scope 'brain' entry."
  else
    echo "       skipped (CLI-first default; '$CLAUDE_BIN' not on PATH, nothing to remove)."
  fi
  echo "       pass --with-mcp to register the brain_* MCP tools instead."
fi

# 8. Clean up any obsolete .mcp.json from earlier setup runs (it never worked).
echo "[6/6] cleanup"
rm -f "$CLAUDE_DIR/.mcp.json"

echo
if [ "$WITH_MCP" -eq 1 ] && [ "$MCP_REGISTERED" -ne 1 ]; then
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
  echo "     CLAUDE_BIN=\$(which claude) $0 $CLAUDE_DIR $VAULT_ROOT --with-mcp"
else
  echo "✓ Brain installed in $CLAUDE_DIR"
fi
echo
echo "Next steps:"
echo "  1. Open a new Claude Code session in any project."
echo "  2. The SessionStart hook should preload the brain context automatically."
if [ "$WITH_MCP" -eq 1 ]; then
  echo "  3. The brain_* MCP tools should appear in your tool list."
else
  echo "  3. The model drives the Brain via the CLI: $BRAIN_CMD <recall|save|...>"
  echo "     (try /brain list in a session, or run the command above yourself)."
fi
echo "  4. To register with LMStudio or another MCP client, point its MCP settings at:"
echo "       command: $VENV_PYTHON"
echo "       args:    -m brain_mcp"
echo "       env:     BRAIN_VAULT=$VAULT_ROOT"
