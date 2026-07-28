#!/usr/bin/env python3
"""brain-setup — cross-platform installer for the Ai-Brain wiring.

Replaces setup-mac.sh and setup-windows.ps1 for users who prefer a single,
prompt-driven install. The shell scripts remain as fallbacks.

Usage:
    python brain-setup.py                  # interactive — prompts for everything
    python brain-setup.py --non-interactive --vault PATH --claude-dir DIR [DIR ...]

Stdlib only — no dependencies on PyPI packages. Works on macOS, Windows, Linux.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
HOOKS_DIR = REPO_DIR / "hooks"
MCP_SERVER_DIR = REPO_DIR / "mcp-server"
TEMPLATES_DIR = REPO_DIR / "templates"
VENV_DIR = MCP_SERVER_DIR / ".venv"

IS_WINDOWS = platform.system() == "Windows"
VENV_PY = VENV_DIR / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
VENV_PIP = VENV_DIR / ("Scripts/pip.exe" if IS_WINDOWS else "bin/pip")
VENV_BRAIN = VENV_DIR / ("Scripts/brain.exe" if IS_WINDOWS else "bin/brain")

# Mirror mcp-server/pyproject.toml `requires-python`. Bump both together.
MIN_PY = (3, 11)
_VERSION_GATE = f"import sys; sys.exit(0 if sys.version_info >= {MIN_PY} else 1)"


# ---------- output helpers ----------

def info(msg: str) -> None:
    print(msg)

def step(n: int, total: int, msg: str) -> None:
    print(f"[{n}/{total}] {msg}")

def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr)

def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------- discovery ----------

def default_vault() -> Path:
    # Prefer a path outside macOS TCC-protected folders (~/Documents, ~/Desktop,
    # ~/Downloads, iCloud Drive): per-host-app permission denials there surface as
    # confusing selective PermissionErrors and phantom index corruption. Fall back
    # to the legacy Documents location only when a vault already lives there.
    preferred = Path.home() / "Vaults" / "Ai-Brain"
    legacy = Path.home() / "Documents" / "Vaults" / "Ai-Brain"
    if not preferred.exists() and legacy.exists():
        return legacy
    return preferred


def discover_claude_dirs() -> list[Path]:
    """Return existing ~/.claude* directories, sorted."""
    home = Path.home()
    found = sorted(p for p in home.glob(".claude*") if p.is_dir())
    return found


def find_python3() -> list[str]:
    """Return a command-prefix that runs Python >= MIN_PY.

    Prefers version-suffixed binaries (python3.14, py -3.14, ...) over generic
    `python3` because system pythons on some platforms are pinned to old releases
    that fail the brain-mcp install — most notably macOS /usr/bin/python3, which
    is still 3.9 and would shadow a perfectly good Homebrew 3.14 on PATH.
    """
    minors = range(20, MIN_PY[1] - 1, -1)
    candidates: list[list[str]] = []
    if IS_WINDOWS:
        candidates += [["py", f"-3.{m}"] for m in minors]
        candidates += [["py", "-3"], ["python3"], ["python"]]
    else:
        candidates += [[f"python3.{m}"] for m in minors]
        candidates += [["python3"], ["python"]]
    for cmd in candidates:
        if not shutil.which(cmd[0]):
            continue
        try:
            res = subprocess.run(
                cmd + ["-c", _VERSION_GATE],
                capture_output=True, check=False, timeout=10,
            )
            if res.returncode == 0:
                return cmd
        except (OSError, subprocess.TimeoutExpired):
            continue
    return []


# ---------- prompts ----------

def clean_path(raw: str) -> str:
    """Strip whitespace and one layer of surrounding quotes.

    Windows Explorer's "Copy as path" wraps the result in double quotes; users on
    any shell often paste single- or double-quoted paths. Treat the quotes as
    decoration, not part of the path.
    """
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return s


def prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            raw = input(f"{label}{suffix}: ").strip()
        except EOFError:
            print()
            return default or ""
        if raw:
            return raw
        if default is not None:
            return default
        print("(value required)")


def prompt_yes_no(label: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        try:
            raw = input(f"{label} [{d}]: ").strip().lower()
        except EOFError:
            print()
            return default
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False


def prompt_vault(initial: Path | None) -> Path:
    while True:
        raw = prompt("Vault root (must contain or will contain a Brain/ subdir)",
                     default=str(initial) if initial else None)
        chosen = Path(clean_path(raw)).expanduser()
        if chosen.exists() and chosen.is_dir():
            return chosen.resolve()
        if not chosen.exists():
            if prompt_yes_no(f"  {chosen} does not exist — create it?", default=False):
                chosen.mkdir(parents=True, exist_ok=True)
                return chosen.resolve()
        else:
            print(f"  {chosen} exists but is not a directory.")


def prompt_claude_dirs(detected: list[Path]) -> list[Path]:
    if detected:
        info("Detected Claude config dirs:")
        for i, d in enumerate(detected, 1):
            info(f"  {i}. {d}")
        info("Enter numbers separated by commas to install into those dirs,")
        info("or type a custom path (or 'all' to install into every detected dir).")
        raw = prompt("Choice", default="all")
    else:
        info("No ~/.claude* directories found. Enter the path you'd like to install into")
        info("(it will be created if it doesn't exist; e.g. ~/.claude or ~/.claude-personal).")
        raw = prompt("Claude config dir", default=str(Path.home() / ".claude"))

    chosen: list[Path] = []
    if raw.strip().lower() == "all" and detected:
        return detected

    for token in raw.split(","):
        token = clean_path(token)
        if not token:
            continue
        if token.isdigit() and detected:
            idx = int(token) - 1
            if 0 <= idx < len(detected):
                chosen.append(detected[idx])
                continue
            warn(f"  out-of-range selection: {token}")
            continue
        p = Path(token).expanduser().resolve()
        if not p.exists():
            if prompt_yes_no(f"  {p} does not exist — create it?", default=True):
                p.mkdir(parents=True, exist_ok=True)
            else:
                continue
        chosen.append(p)

    if not chosen:
        die("no Claude config dirs selected")
    return chosen


# ---------- install steps ----------

def _venv_is_healthy() -> bool:
    """Sanity-check an existing venv: interpreter runs at MIN_PY+ AND pip's shebang is valid.

    After the repo is renamed (e.g. AiBrain → Ai-Brain), console scripts keep the
    old absolute shebang and fail to exec with a confusing FileNotFoundError. We
    detect that here so the venv gets rebuilt instead of poisoning step 2. The
    version check catches a separate trap: a venv built against a too-old Python
    (e.g. macOS /usr/bin/python3 == 3.9) passes a bare `import sys` test but later
    fails `pip install brain-mcp` because pyproject demands >= 3.11.
    """
    if not VENV_PY.exists() or not VENV_PIP.exists():
        return False
    try:
        res = subprocess.run(
            [str(VENV_PY), "-c", _VERSION_GATE],
            capture_output=True, check=False, timeout=10,
        )
        if res.returncode != 0:
            return False
        res = subprocess.run(
            [str(VENV_PIP), "--version"],
            capture_output=True, check=False, timeout=10,
        )
        return res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_venv(num: int, total: int) -> None:
    if _venv_is_healthy():
        return
    if VENV_DIR.exists():
        step(num, total, f"rebuilding stale venv at {VENV_DIR}")
        shutil.rmtree(VENV_DIR)
    else:
        step(num, total, f"creating Python venv at {VENV_DIR}")
    py = find_python3()
    if not py:
        die(f"no Python >= {MIN_PY[0]}.{MIN_PY[1]} found on PATH. Install it and re-run.")
    res = subprocess.run(py + ["-m", "venv", str(VENV_DIR)], check=False)
    if res.returncode != 0:
        die(f"venv creation failed (exit {res.returncode})")
    subprocess.run([str(VENV_PIP), "install", "--quiet", "--upgrade", "pip"], check=False)


def install_brain_mcp(num: int, total: int) -> None:
    step(num, total, "installing brain-mcp into venv")
    # Two-step: `--force-reinstall --no-deps` catches local source edits (the
    # pyproject version doesn't bump on every edit, so pip would otherwise skip).
    # The second plain install pulls mcp/pyyaml/fastembed/numpy on first run and
    # is near-instant on subsequent runs. Collapsing to a single --force-reinstall
    # would re-extract ~300 MB of deps every time.
    subprocess.run(
        [str(VENV_PIP), "install", "--quiet", "--force-reinstall", "--no-deps", str(MCP_SERVER_DIR)],
        check=True,
    )
    subprocess.run([str(VENV_PIP), "install", "--quiet", str(MCP_SERVER_DIR)], check=True)


def sanity_import(num: int, total: int, vault_root: Path) -> None:
    step(num, total, "import smoke test from foreign cwd")
    env = os.environ.copy()
    env["BRAIN_VAULT"] = str(vault_root)
    cwd = os.environ.get("TEMP") if IS_WINDOWS else "/tmp"
    res = subprocess.run(
        [str(VENV_PY), "-c", "from brain_mcp import vault, server, embed, compact"],
        env=env, cwd=cwd or str(REPO_DIR), check=False,
    )
    if res.returncode != 0:
        die("brain_mcp module failed to import from a foreign cwd. Aborting.", code=2)


def warm_embedder(num: int, total: int, vault_root: Path) -> None:
    # embed.py pins a stable, machine-local cache (%LOCALAPPDATA%\Ai-Brain\fastembed on
    # Windows, ~/.cache/ai-brain/fastembed elsewhere) so this one-time download lands
    # there and every later load is offline — no per-recall HuggingFace round-trip, which
    # when slow/unreachable previously hung brain_recall. Override the location with
    # BRAIN_EMBED_CACHE; force-online model updates with BRAIN_EMBED_OFFLINE=0.
    step(num, total, "warming up embedding model (one-time ONNX download, ~65MB)")
    env = os.environ.copy()
    env["BRAIN_VAULT"] = str(vault_root)
    env.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    res = subprocess.run(
        [str(VENV_PY), "-c", "from brain_mcp.embed import EmbedIndex; EmbedIndex.warm()"],
        env=env, check=False,
    )
    if res.returncode != 0:
        warn("embed warm-up failed; vector recall will fall back to ripgrep until resolved.")


def ensure_brain_layout(vault_root: Path) -> None:
    for sub in ("user", "feedback", "references", "projects"):
        (vault_root / "Brain" / sub).mkdir(parents=True, exist_ok=True)


def write_windows_brain_cmd(claude_dir: Path, vault_root: Path) -> Path:
    """Generate the per-install brain.cmd CLI wrapper (Windows).

    This is what the model runs through the Bash tool — it bakes in BRAIN_VAULT
    and forwards all arguments to the venv's brain.exe.
    """
    brain_cmd = claude_dir / "brain.cmd"
    body = (
        "@echo off\r\n"
        "rem Generated by brain-setup.py — do not edit by hand. Re-run brain-setup.py to regenerate.\r\n"
        "setlocal\r\n"
        f'set "BRAIN_VAULT={vault_root}"\r\n'
        f'"{VENV_BRAIN}" %*\r\n'
        "exit /b %ERRORLEVEL%\r\n"
    )
    brain_cmd.write_text(body, encoding="utf-8")
    return brain_cmd


def brain_cmd_token(claude_dir: Path, vault_root: Path) -> str:
    """The invocation substituted for __BRAIN_CMD__ in the rendered templates.

    Windows: the generated brain.cmd wrapper, with forward slashes (Claude Code
    often runs Bash-tool commands through Git Bash, which eats single
    backslashes). POSIX: an env prefix + the venv console script.
    """
    if IS_WINDOWS:
        return str(write_windows_brain_cmd(claude_dir, vault_root)).replace("\\", "/")
    return f'BRAIN_VAULT="{vault_root}" "{VENV_BRAIN}"'


def render_global_claude_md(claude_dir: Path, vault_root: Path, cmd: str) -> None:
    template = (TEMPLATES_DIR / "global-CLAUDE.md").read_text(encoding="utf-8")
    rendered = template.replace("__BRAIN_VAULT__", str(vault_root)).replace("__BRAIN_CMD__", cmd)
    (claude_dir / "CLAUDE.md").write_text(rendered, encoding="utf-8")


def copy_brain_skill(claude_dir: Path, cmd: str) -> None:
    src = TEMPLATES_DIR / "skills" / "brain" / "SKILL.md"
    dst_dir = claude_dir / "skills" / "brain"
    dst_dir.mkdir(parents=True, exist_ok=True)
    template = src.read_text(encoding="utf-8")
    (dst_dir / "SKILL.md").write_text(
        template.replace("__BRAIN_CMD__", cmd), encoding="utf-8"
    )


def write_windows_launch_cmd(claude_dir: Path, vault_root: Path) -> Path:
    """Generate a per-install brain-launch.cmd wrapper that bakes in BRAIN_VAULT."""
    launch_cmd = claude_dir / "brain-launch.cmd"
    body = (
        "@echo off\r\n"
        "rem Generated by brain-setup.py — do not edit by hand. Re-run brain-setup.py to regenerate.\r\n"
        "setlocal\r\n"
        f'set "BRAIN_VAULT={vault_root}"\r\n'
        f'"{VENV_PY}" "{HOOKS_DIR}\\%~1.py"\r\n'
        "exit /b %ERRORLEVEL%\r\n"
    )
    launch_cmd.write_text(body, encoding="utf-8")
    return launch_cmd


def merge_settings_json(claude_dir: Path, vault_root: Path) -> None:
    settings_path = claude_dir / "settings.json"
    if not settings_path.exists():
        settings_path.write_text("{}\n", encoding="utf-8")
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        settings = {}

    if IS_WINDOWS:
        launch_cmd = write_windows_launch_cmd(claude_dir, vault_root)
        template = (TEMPLATES_DIR / "settings.hooks.win.json").read_text(encoding="utf-8")
        # Use forward slashes in the path written to settings.json. Claude Code on
        # Windows often runs hooks through Git Bash (/usr/bin/bash), which strips
        # single backslashes as escape characters — so "C:\Users\…\brain-launch.cmd"
        # becomes "C:Usersbrain-launch.cmd" by the time it reaches the OS. Forward
        # slashes work in cmd.exe, bash, and python.exe equally well on Windows.
        launch_str = str(launch_cmd).replace("\\", "/")
        template = template.replace("__BRAIN_LAUNCH__", launch_str)
    else:
        template = (TEMPLATES_DIR / "settings.hooks.json").read_text(encoding="utf-8")
        template = (
            template
            .replace("__BRAIN_PYTHON__", str(VENV_PY))
            .replace("__BRAIN_HOOKS__", str(HOOKS_DIR))
            .replace("__BRAIN_VAULT__", str(vault_root))
        )

    hooks_block = json.loads(template)["hooks"]
    settings.setdefault("hooks", {})
    for event, definition in hooks_block.items():
        settings["hooks"][event] = definition  # overwrite brain block; preserve others

    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def _is_default_claude_dir(claude_dir: Path) -> bool:
    """Return True when claude_dir resolves to the Claude CLI's default config dir."""
    default = Path.home() / ".claude"
    try:
        return claude_dir.resolve() == default.resolve()
    except (OSError, RuntimeError):
        return str(claude_dir).rstrip("\\/") == str(default).rstrip("\\/")


def _mcp_config_file(claude_dir: Path) -> Path:
    """Return the .claude.json that `claude mcp` reads/writes for `claude_dir`.

    The default config dir (~/.claude) is special: its user-scope config file
    lives at ~/.claude.json (home), NOT inside ~/.claude/. Custom config dirs
    (set via CLAUDE_CONFIG_DIR) keep a sibling .claude.json inside the dir.
    """
    if _is_default_claude_dir(claude_dir):
        return Path.home() / ".claude.json"
    return claude_dir / ".claude.json"


def register_mcp(claude_dir: Path, vault_root: Path) -> tuple[bool, str]:
    """Register brain as a user-scope MCP server for `claude_dir`.

    Returns (ok, failure_reason). reason is '' on success.
    """
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    if not shutil.which(claude_bin):
        return False, f"'{claude_bin}' not on PATH (install Claude Code, or set CLAUDE_BIN)"

    # `claude mcp add --scope user` writes to $CLAUDE_CONFIG_DIR/.claude.json
    # when the env var is set, but to $HOME/.claude.json when it isn't - two
    # different files. When claude_dir is the default location, leave the env
    # var unset so the write lands where a plain `claude` invocation later
    # reads from. For custom dirs each has its own sibling .claude.json.
    env = os.environ.copy()
    if _is_default_claude_dir(claude_dir):
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(claude_dir)

    # Idempotent: drop any existing 'brain' user-scope server first.
    subprocess.run([claude_bin, "mcp", "remove", "brain", "--scope", "user"],
                   env=env, capture_output=True, check=False)

    res = subprocess.run(
        [claude_bin, "mcp", "add", "brain", "--scope", "user",
         "-e", f"BRAIN_VAULT={vault_root}",
         "--", str(VENV_PY), "-m", "brain_mcp"],
        env=env, capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        return False, f"'claude mcp add' exited {res.returncode}: {err}"

    # Verify the entry actually PERSISTED to the config file `claude` will read
    # from when launched against this dir — not a transient `claude mcp list`
    # snapshot. The old check grepped `claude mcp list`, which passed whenever a
    # 'brain' line appeared at that instant; it silently missed the case where
    # the entry landed in a different config file than the one this dir launches
    # with (e.g. registered the default dir's ~/.claude.json but the user runs
    # Claude under CLAUDE_CONFIG_DIR=.claude-foo). Read mcpServers directly.
    config_file = _mcp_config_file(claude_dir)
    try:
        cfg = json.loads(config_file.read_text(encoding="utf-8")) if config_file.exists() else {}
    except (OSError, json.JSONDecodeError) as e:
        return False, f"could not read {config_file} to verify registration: {e}"
    if "brain" not in (cfg.get("mcpServers") or {}):
        return False, (
            f"'claude mcp add' returned success but 'brain' is not in "
            f"mcpServers of {config_file}"
        )

    return True, ""


def cleanup(claude_dir: Path) -> None:
    """Remove obsolete .mcp.json from earlier setup attempts (it never worked at user scope)."""
    stale = claude_dir / ".mcp.json"
    if stale.exists():
        try:
            stale.unlink()
        except OSError:
            pass


# ---------- orchestration ----------

def deregister_mcp(claude_dir: Path) -> None:
    """Best-effort removal of a stale user-scope 'brain' MCP registration.

    The CLI-first default means the MCP schemas would just cost ~3k tokens per
    session — remove any registration left by an older setup run so the token
    saving actually lands.
    """
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    if not shutil.which(claude_bin):
        return
    env = os.environ.copy()
    if _is_default_claude_dir(claude_dir):
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    subprocess.run([claude_bin, "mcp", "remove", "brain", "--scope", "user"],
                   env=env, capture_output=True, check=False)


def install_one(claude_dir: Path, vault_root: Path, with_mcp: bool) -> tuple[bool, str]:
    """Install brain wiring into one Claude config dir. Returns (mcp_ok, reason)."""
    info("")
    info(f"━━━ installing into {claude_dir} ━━━")
    claude_dir.mkdir(parents=True, exist_ok=True)
    ensure_brain_layout(vault_root)

    cmd = brain_cmd_token(claude_dir, vault_root)

    step(1, 5, f"writing {claude_dir}/CLAUDE.md")
    render_global_claude_md(claude_dir, vault_root, cmd)

    step(2, 5, f"writing {claude_dir}/skills/brain/SKILL.md")
    copy_brain_skill(claude_dir, cmd)

    step(3, 5, f"merging hooks into {claude_dir}/settings.json")
    merge_settings_json(claude_dir, vault_root)

    if with_mcp:
        step(4, 5, "registering brain MCP server (user scope)")
        ok, reason = register_mcp(claude_dir, vault_root)
        if ok:
            info(f"       ✓ registered as user-scope MCP server in {claude_dir}")
    else:
        step(4, 5, "MCP registration skipped (CLI-first default; pass --with-mcp to enable)")
        deregister_mcp(claude_dir)
        ok, reason = True, ""

    step(5, 5, "cleanup")
    cleanup(claude_dir)
    return ok, reason


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install the Ai-Brain wiring into one or more Claude Code config dirs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python brain-setup.py\n"
            "  python brain-setup.py --vault D:\\Vaults\\Ai-Brain --claude-dir %USERPROFILE%\\.claude-personal\n"
            "  python brain-setup.py --non-interactive --vault ~/Vaults/Ai-Brain \\\n"
            "                        --claude-dir ~/.claude-personal --claude-dir ~/.claude-work\n"
            "\n"
            "The --claude-dir value can be any path. Single-account users use ~/.claude;\n"
            "multi-account users pick their own names (anything starting with .claude is\n"
            "picked up by auto-discovery when --claude-dir is omitted).\n"
        ),
    )
    parser.add_argument("--vault", help="vault root (the directory containing or that will contain Brain/).")
    parser.add_argument("--claude-dir", action="append", default=[],
                        help="Claude config dir to install into. May be repeated.")
    parser.add_argument("--non-interactive", action="store_true",
                        help="fail rather than prompt for missing values; for scripted use.")
    parser.add_argument("--with-mcp", action="store_true",
                        help="also register the brain MCP server with Claude Code "
                             "(default: CLI-first — no MCP registration, and any stale "
                             "user-scope 'brain' entry is removed).")
    args = parser.parse_args()

    info("Brain setup")
    info(f"  repo:     {REPO_DIR}")

    # ---- vault ----
    if args.vault:
        vault_root = Path(clean_path(args.vault)).expanduser().resolve()
        if not vault_root.exists():
            if args.non_interactive:
                die(f"vault path does not exist: {vault_root}")
            if not prompt_yes_no(f"vault path {vault_root} does not exist — create it?", default=False):
                die("aborted")
            vault_root.mkdir(parents=True, exist_ok=True)
    elif args.non_interactive:
        die("--vault is required in non-interactive mode")
    else:
        vault_root = prompt_vault(default_vault() if default_vault().exists() else None)
    info(f"  vault:    {vault_root}")

    # ---- claude dirs ----
    if args.claude_dir:
        claude_dirs = []
        for d in args.claude_dir:
            p = Path(clean_path(d)).expanduser().resolve()
            if not p.exists():
                if args.non_interactive:
                    p.mkdir(parents=True, exist_ok=True)
                elif not prompt_yes_no(f"claude config dir {p} does not exist — create it?", default=True):
                    continue
                else:
                    p.mkdir(parents=True, exist_ok=True)
            claude_dirs.append(p)
        if not claude_dirs:
            die("no Claude config dirs to install into")
    elif args.non_interactive:
        die("--claude-dir is required in non-interactive mode")
    else:
        claude_dirs = prompt_claude_dirs(discover_claude_dirs())

    info("  config:   " + ", ".join(str(c) for c in claude_dirs))
    info("")

    # ---- shared install (venv, deps, warm-up — done once regardless of #claude dirs) ----
    info("Preparing brain-mcp")
    ensure_venv(1, 4)
    install_brain_mcp(2, 4)
    sanity_import(3, 4, vault_root)
    warm_embedder(4, 4, vault_root)

    # ---- per-claude-dir wiring ----
    results: list[tuple[Path, bool, str]] = []
    for cd in claude_dirs:
        ok, reason = install_one(cd, vault_root, args.with_mcp)
        results.append((cd, ok, reason))

    info("")
    failures = [(cd, reason) for cd, ok, reason in results if not ok]
    if not failures:
        info("✓ Brain installed.")
    else:
        info("✓ Brain files installed.")
        info("")
        info("✗ MCP SERVER NOT REGISTERED for these config dir(s) — brain_* tools will NOT appear in Claude Code:")
        for cd, reason in failures:
            info(f"    {cd}")
            info(f"      reason: {reason}")
        info("")
        info("   To fix, ensure Claude Code is installed and on PATH, then for each failed dir above run:")
        for cd, _ in failures:
            if _is_default_claude_dir(cd):
                info(f'     claude mcp add brain --scope user -e "BRAIN_VAULT={vault_root}" -- "{VENV_PY}" -m brain_mcp')
            elif IS_WINDOWS:
                info(f'     $env:CLAUDE_CONFIG_DIR = "{cd}"')
                info(f'     claude mcp add brain --scope user -e "BRAIN_VAULT={vault_root}" -- "{VENV_PY}" -m brain_mcp')
            else:
                info(f'     CLAUDE_CONFIG_DIR="{cd}" claude mcp add brain --scope user -e BRAIN_VAULT="{vault_root}" -- "{VENV_PY}" -m brain_mcp')

    info("")
    info("Next steps:")
    info("  1. Open a new Claude Code session in any project.")
    info("  2. The SessionStart hook should preload the brain context automatically.")
    if args.with_mcp:
        info("  3. The brain_* MCP tools should appear in your tool list.")
    else:
        info("  3. The model drives the Brain via the `brain` CLI (see the installed skill:")
        info("     <config-dir>/skills/brain/SKILL.md — try /brain list in a session).")
    info("  4. To register with LMStudio or another MCP client, point its MCP settings at:")
    info(f"       command: {VENV_PY}")
    info("       args:    -m brain_mcp")
    info(f"       env:     BRAIN_VAULT={vault_root}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\naborted")
        sys.exit(130)
