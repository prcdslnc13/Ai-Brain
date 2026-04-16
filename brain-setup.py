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
    return Path.home() / "Documents" / "Vaults" / "Ai-Brain"


def discover_claude_dirs() -> list[Path]:
    """Return existing ~/.claude* directories, sorted."""
    home = Path.home()
    found = sorted(p for p in home.glob(".claude*") if p.is_dir())
    return found


def find_python3() -> list[str]:
    """Return a command-prefix that runs Python 3 (e.g. ['py','-3'] or ['python3'])."""
    candidates: list[list[str]] = []
    if IS_WINDOWS:
        candidates += [["py", "-3"], ["python"], ["python3"]]
    else:
        candidates += [["python3"], ["python"]]
    for cmd in candidates:
        if not shutil.which(cmd[0]):
            continue
        try:
            out = subprocess.run(cmd + ["--version"], capture_output=True, text=True, check=False)
            if out.returncode == 0 and "Python 3" in (out.stdout + out.stderr):
                return cmd
        except OSError:
            continue
    return []


# ---------- prompts ----------

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
        chosen = Path(prompt("Vault root (must contain or will contain a Brain/ subdir)",
                             default=str(initial) if initial else None)).expanduser()
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
        token = token.strip()
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

def ensure_venv(num: int, total: int) -> None:
    if VENV_PY.exists():
        return
    step(num, total, f"creating Python venv at {VENV_DIR}")
    py = find_python3()
    if not py:
        die("no Python 3 interpreter found. Install Python 3.11+ and re-run.")
    res = subprocess.run(py + ["-m", "venv", str(VENV_DIR)], check=False)
    if res.returncode != 0:
        die(f"venv creation failed (exit {res.returncode})")
    subprocess.run([str(VENV_PIP), "install", "--quiet", "--upgrade", "pip"], check=False)


def install_brain_mcp(num: int, total: int) -> None:
    step(num, total, "installing brain-mcp into venv")
    # Force-reinstall the package itself (no deps) first to catch source changes,
    # then install with deps to ensure mcp/pyyaml/fastembed/numpy are present.
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
    step(num, total, "warming up embedding model (one-time ONNX download, ~130MB)")
    env = os.environ.copy()
    env["BRAIN_VAULT"] = str(vault_root)
    res = subprocess.run(
        [str(VENV_PY), "-c", "from brain_mcp.embed import EmbedIndex; EmbedIndex.warm()"],
        env=env, check=False,
    )
    if res.returncode != 0:
        warn("embed warm-up failed; vector recall will fall back to ripgrep until resolved.")


def ensure_brain_layout(vault_root: Path) -> None:
    for sub in ("user", "feedback", "references", "projects"):
        (vault_root / "Brain" / sub).mkdir(parents=True, exist_ok=True)


def render_global_claude_md(claude_dir: Path, vault_root: Path) -> None:
    template = (TEMPLATES_DIR / "global-CLAUDE.md").read_text(encoding="utf-8")
    rendered = template.replace("__BRAIN_VAULT__", str(vault_root))
    (claude_dir / "CLAUDE.md").write_text(rendered, encoding="utf-8")


def copy_brain_skill(claude_dir: Path) -> None:
    src = TEMPLATES_DIR / "skills" / "brain" / "SKILL.md"
    dst_dir = claude_dir / "skills" / "brain"
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst_dir / "SKILL.md")


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
        # JSON-escape the backslashes before substitution, since the template is
        # parsed as JSON after the replace and single backslashes would break it.
        template = template.replace("__BRAIN_LAUNCH__", str(launch_cmd).replace("\\", "\\\\"))
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


def register_mcp(claude_dir: Path, vault_root: Path) -> bool:
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    if not shutil.which(claude_bin):
        warn(f"'{claude_bin}' not on PATH; skipping MCP registration.")
        warn("Install Claude Code (or set CLAUDE_BIN=/path/to/claude) and re-run this wizard.")
        return False

    env = os.environ.copy()
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
        warn(f"claude mcp add failed (exit {res.returncode}): {res.stderr.strip()}")
        return False
    return True


def cleanup(claude_dir: Path) -> None:
    """Remove obsolete .mcp.json from earlier setup attempts (it never worked at user scope)."""
    stale = claude_dir / ".mcp.json"
    if stale.exists():
        try:
            stale.unlink()
        except OSError:
            pass


# ---------- orchestration ----------

def install_one(claude_dir: Path, vault_root: Path) -> None:
    info("")
    info(f"━━━ installing into {claude_dir} ━━━")
    claude_dir.mkdir(parents=True, exist_ok=True)
    ensure_brain_layout(vault_root)

    step(1, 5, f"writing {claude_dir}/CLAUDE.md")
    render_global_claude_md(claude_dir, vault_root)

    step(2, 5, f"writing {claude_dir}/skills/brain/SKILL.md")
    copy_brain_skill(claude_dir)

    step(3, 5, f"merging hooks into {claude_dir}/settings.json")
    merge_settings_json(claude_dir, vault_root)

    step(4, 5, "registering brain MCP server (user scope)")
    if register_mcp(claude_dir, vault_root):
        info(f"       ✓ registered as user-scope MCP server in {claude_dir}")

    step(5, 5, "cleanup")
    cleanup(claude_dir)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install the Ai-Brain wiring into one or more Claude Code config dirs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python brain-setup.py\n"
            "  python brain-setup.py --vault D:\\Vaults\\Ai-Brain --claude-dir %USERPROFILE%\\.claude-personal\n"
            "  python brain-setup.py --non-interactive --vault ~/Documents/Vaults/Ai-Brain \\\n"
            "                        --claude-dir ~/.claude-personal --claude-dir ~/.claude-work\n"
        ),
    )
    parser.add_argument("--vault", help="vault root (the directory containing or that will contain Brain/).")
    parser.add_argument("--claude-dir", action="append", default=[],
                        help="Claude config dir to install into. May be repeated.")
    parser.add_argument("--non-interactive", action="store_true",
                        help="fail rather than prompt for missing values; for scripted use.")
    args = parser.parse_args()

    info("Brain setup")
    info(f"  repo:     {REPO_DIR}")

    # ---- vault ----
    if args.vault:
        vault_root = Path(args.vault).expanduser().resolve()
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
            p = Path(d).expanduser().resolve()
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
    for cd in claude_dirs:
        install_one(cd, vault_root)

    info("")
    info("✓ Brain installed.")
    info("")
    info("Next steps:")
    info("  1. Open a new Claude Code session in any project.")
    info("  2. The SessionStart hook should preload the brain context automatically.")
    info("  3. The brain_* MCP tools should appear in your tool list.")
    info("  4. To register with LMStudio, point its MCP settings at:")
    info(f"       command: {VENV_PY}")
    info("       args:    -m brain_mcp")
    info(f"       env:     BRAIN_VAULT={vault_root}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\naborted")
        sys.exit(130)
