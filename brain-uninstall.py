#!/usr/bin/env python3
"""brain-uninstall — cross-platform uninstaller for the Ai-Brain wiring.

Reverses brain-setup.py (and the setup-{mac,linux,windows} shell scripts).
Stdlib only — no PyPI dependencies. Works on macOS, Windows, Linux.

Usage:
    python brain-uninstall.py                              # interactive
    python brain-uninstall.py --non-interactive --claude-dir DIR [--claude-dir DIR ...]

WHAT THIS TOUCHES
    - unregisters the 'brain' user-scope MCP server for each config dir
    - prunes Brain-owned entries from <config>/settings.json
    - removes <config>/CLAUDE.md ONLY if it carries our managed-by marker
      (hand-edited CLAUDE.md without the marker is left alone)
    - removes <config>/skills/brain/ (leaves <config>/skills/ for other skills)
    - removes <config>/brain-launch.cmd (Windows-only generated wrapper)
    - removes <repo>/mcp-server/.venv

WHAT THIS DOES NOT TOUCH
    - the vault (Brain/user, feedback, projects, sessions, activity.md, index)
    - the fastembed ONNX model cache (shared, harmless)
    - the repo itself
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
VENV_DIR = MCP_SERVER_DIR / ".venv"

IS_WINDOWS = platform.system() == "Windows"
VENV_PY = VENV_DIR / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")

MARKER = "<!-- managed-by: ai-brain -->"


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


# ---------- discovery & prompts (same idioms as brain-setup.py) ----------

def discover_claude_dirs() -> list[Path]:
    home = Path.home()
    return sorted(p for p in home.glob(".claude*") if p.is_dir())


def clean_path(raw: str) -> str:
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


def prompt_claude_dirs(detected: list[Path]) -> list[Path]:
    if not detected:
        info("No ~/.claude* directories found. Nothing to uninstall from.")
        info("If you installed into a custom path, pass it with --claude-dir.")
        return []

    info("Detected Claude config dirs:")
    for i, d in enumerate(detected, 1):
        info(f"  {i}. {d}")
    info("Enter numbers separated by commas to uninstall from those dirs,")
    info("or type a custom path (or 'all' to uninstall from every detected dir).")
    raw = prompt("Choice", default="all")

    if raw.strip().lower() == "all":
        return detected

    chosen: list[Path] = []
    for token in raw.split(","):
        token = clean_path(token)
        if not token:
            continue
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(detected):
                chosen.append(detected[idx])
                continue
            warn(f"  out-of-range selection: {token}")
            continue
        p = Path(token).expanduser().resolve()
        if not p.exists():
            warn(f"  {p} does not exist — skipping")
            continue
        chosen.append(p)
    return chosen


# ---------- per-step logic ----------

def _is_default_claude_dir(claude_dir: Path) -> bool:
    default = Path.home() / ".claude"
    try:
        return claude_dir.resolve() == default.resolve()
    except (OSError, RuntimeError):
        return str(claude_dir).rstrip("\\/") == str(default).rstrip("\\/")


def unregister_mcp(claude_dir: Path) -> None:
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    if not shutil.which(claude_bin):
        info(f"       [warn] '{claude_bin}' not on PATH — skipping MCP unregister.")
        info("         If brain is still registered, remove it manually later:")
        if _is_default_claude_dir(claude_dir):
            info(f"           {claude_bin} mcp remove brain --scope user")
        else:
            if IS_WINDOWS:
                info(f"           $env:CLAUDE_CONFIG_DIR = '{claude_dir}'")
                info(f"           {claude_bin} mcp remove brain --scope user")
            else:
                info(f"           CLAUDE_CONFIG_DIR={claude_dir} {claude_bin} mcp remove brain --scope user")
        return

    env = os.environ.copy()
    if _is_default_claude_dir(claude_dir):
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(claude_dir)

    res = subprocess.run(
        [claude_bin, "mcp", "remove", "brain", "--scope", "user"],
        env=env, capture_output=True, text=True, check=False,
    )
    if res.returncode == 0:
        info("       ✓ brain MCP server unregistered")
    else:
        info("       (brain was not registered; nothing to remove)")


def prune_settings_hooks(claude_dir: Path) -> None:
    settings_path = claude_dir / "settings.json"
    if not settings_path.exists():
        info("       (no settings.json — nothing to prune)")
        return
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        info("       (settings.json unparseable — leaving it alone)")
        return

    hooks_dir_str = str(HOOKS_DIR).lower()

    def is_brain_command(cmd: object) -> bool:
        if not isinstance(cmd, str):
            return False
        low = cmd.lower()
        return (
            "brain_vault=" in low
            or hooks_dir_str in low
            or "brain-launch" in low
        )

    existing = settings.get("hooks", {}) or {}
    if not isinstance(existing, dict):
        info("       (hooks block malformed — leaving it alone)")
        return

    removed = 0
    for event in list(existing.keys()):
        groups = existing.get(event) or []
        if not isinstance(groups, list):
            continue
        pruned_groups: list = []
        for group in groups:
            if not isinstance(group, dict):
                pruned_groups.append(group)
                continue
            inner = group.get("hooks") or []
            kept = []
            for h in inner:
                if isinstance(h, dict) and is_brain_command(h.get("command", "")):
                    removed += 1
                else:
                    kept.append(h)
            if kept:
                new_group = dict(group)
                new_group["hooks"] = kept
                pruned_groups.append(new_group)
        if pruned_groups:
            existing[event] = pruned_groups
        else:
            del existing[event]

    if existing:
        settings["hooks"] = existing
    else:
        settings.pop("hooks", None)

    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    word = "entry" if removed == 1 else "entries"
    info(f"       ✓ removed {removed} Brain-owned hook {word}")


def remove_managed_claude_md(claude_dir: Path) -> None:
    claude_md = claude_dir / "CLAUDE.md"
    if not claude_md.exists():
        info("       (no CLAUDE.md to remove)")
        return
    # Only check the first few lines — the marker is line 1 in our template.
    try:
        head = claude_md.read_text(encoding="utf-8", errors="replace").splitlines()[:5]
    except OSError as e:
        info(f"       (could not read CLAUDE.md: {e} — leaving it alone)")
        return
    if any(MARKER in line for line in head):
        try:
            claude_md.unlink()
            info("       ✓ removed (marker present)")
        except OSError as e:
            info(f"       [warn] could not remove {claude_md}: {e}")
    else:
        info("       [warn] CLAUDE.md has no managed-by marker — leaving it in place.")
        info(f"         (If you want it gone, delete it manually: {claude_md})")


def remove_brain_skill(claude_dir: Path) -> None:
    skill_dir = claude_dir / "skills" / "brain"
    if not skill_dir.exists():
        info("       (not present)")
        return
    shutil.rmtree(skill_dir, ignore_errors=True)
    info("       ✓ removed")
    # If skills/ is now empty, tidy it up too. Don't error if it's not.
    skills_root = claude_dir / "skills"
    try:
        if skills_root.is_dir() and not any(skills_root.iterdir()):
            skills_root.rmdir()
    except OSError:
        pass


def remove_launch_cmd(claude_dir: Path) -> None:
    launch_cmd = claude_dir / "brain-launch.cmd"
    if launch_cmd.exists():
        try:
            launch_cmd.unlink()
            info("       ✓ removed")
        except OSError as e:
            info(f"       [warn] could not remove {launch_cmd}: {e}")
    else:
        info("       (not present)")


def _venv_still_referenced(uninstalled: list[Path]) -> Path | None:
    """Return the first ~/.claude* dir (not in `uninstalled`) whose settings.json
    or brain-launch.cmd still references the venv directory. The venv is shared
    across config dirs — we must not remove it out from under a sibling install.
    """
    venv_str = str(VENV_DIR)
    uninstalled_resolved = {p.resolve() for p in uninstalled}
    for cand in sorted(Path.home().glob(".claude*")):
        if not cand.is_dir():
            continue
        try:
            if cand.resolve() in uninstalled_resolved:
                continue
        except OSError:
            pass
        for probe in (cand / "settings.json", cand / "brain-launch.cmd"):
            if not probe.exists():
                continue
            try:
                if venv_str in probe.read_text(encoding="utf-8", errors="replace"):
                    return cand
            except OSError:
                continue
    return None


def remove_venv(uninstalled: list[Path]) -> None:
    if not VENV_DIR.exists():
        info(f"       (not present at {VENV_DIR})")
        return
    still_used = _venv_still_referenced(uninstalled)
    if still_used is not None:
        info(f"       (still referenced by {still_used} — leaving in place)")
        info(f"       Re-run with --claude-dir {still_used} to remove it too.")
        return
    shutil.rmtree(VENV_DIR, ignore_errors=True)
    info(f"       ✓ removed {VENV_DIR}")


# ---------- orchestration ----------

def uninstall_one(claude_dir: Path) -> None:
    info("")
    info(f"━━━ uninstalling from {claude_dir} ━━━")
    total = 5 if not IS_WINDOWS else 6
    step(1, total, "unregistering brain MCP server (user scope)")
    unregister_mcp(claude_dir)

    step(2, total, f"pruning Brain hooks from {claude_dir}/settings.json")
    prune_settings_hooks(claude_dir)

    step(3, total, f"checking {claude_dir}/CLAUDE.md")
    remove_managed_claude_md(claude_dir)

    step(4, total, f"removing {claude_dir}/skills/brain")
    remove_brain_skill(claude_dir)

    if IS_WINDOWS:
        step(5, total, f"removing {claude_dir}/brain-launch.cmd")
        remove_launch_cmd(claude_dir)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove the Ai-Brain wiring from one or more Claude Code config dirs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python brain-uninstall.py\n"
            "  python brain-uninstall.py --claude-dir ~/.claude-personal\n"
            "  python brain-uninstall.py --non-interactive --claude-dir ~/.claude-personal --claude-dir ~/.claude-work\n"
        ),
    )
    parser.add_argument("--claude-dir", action="append", default=[],
                        help="Claude config dir to uninstall from. May be repeated.")
    parser.add_argument("--non-interactive", action="store_true",
                        help="fail rather than prompt for missing values; for scripted use.")
    args = parser.parse_args()

    info("Brain uninstall")
    info(f"  repo: {REPO_DIR}")

    if args.claude_dir:
        claude_dirs = []
        for d in args.claude_dir:
            p = Path(clean_path(d)).expanduser().resolve()
            if not p.exists():
                warn(f"{p} does not exist — skipping")
                continue
            claude_dirs.append(p)
    elif args.non_interactive:
        die("--claude-dir is required in non-interactive mode")
    else:
        claude_dirs = prompt_claude_dirs(discover_claude_dirs())

    if not claude_dirs:
        info("")
        info("Nothing to do for any config dir. Removing shared venv only.")
    else:
        info("  targets: " + ", ".join(str(c) for c in claude_dirs))

    for cd in claude_dirs:
        uninstall_one(cd)

    # Final shared step — venv lives in the repo, not per-config-dir, so it's
    # removed once after all config dirs are processed.
    info("")
    info("━━━ shared cleanup ━━━")
    total = 1
    step(1, total, "removing mcp-server/.venv")
    remove_venv(claude_dirs)

    info("")
    info("✓ Brain uninstalled.")
    info("")
    info("Not touched (by design):")
    info("  - your vault (Brain/user, feedback, projects, sessions, activity.md, index)")
    info("  - the fastembed ONNX model cache (shared, harmless)")
    info("  - this repo")
    info("")
    info("To reinstall later: python brain-setup.py")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\naborted")
        sys.exit(130)
