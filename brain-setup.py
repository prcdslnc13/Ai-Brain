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
import codecs
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

# The settings.json merge is shared with the three shell/PowerShell installers
# (which invoke this same file as a script) so the algorithm cannot fork again —
# see brain_settings_merge.py's docstring. sys.path[0] is already REPO_DIR when
# this file is run as a script; the insert covers every other invocation shape.
sys.path.insert(0, str(REPO_DIR))
import brain_settings_merge  # noqa: E402  (must follow the REPO_DIR sys.path setup)

IS_WINDOWS = platform.system() == "Windows"
VENV_PY = VENV_DIR / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
VENV_PIP = VENV_DIR / ("Scripts/pip.exe" if IS_WINDOWS else "bin/pip")

# Per-config-dir files this installer generates. The launcher is the model-facing
# one (every platform); the batch file is hook-facing (Windows only); brain.cmd is
# the retired model-facing wrapper, named so the upgrade path can remove it.
AGENT_LAUNCHER_NAME = "brain-agent.py"
LAUNCH_CMD_NAME = "brain-launch.cmd"
LEGACY_BRAIN_CMD_NAME = "brain.cmd"

MANAGED_MARKER = brain_settings_merge.MANAGED_MARKER

# Mirror mcp-server/pyproject.toml `requires-python`. Bump both together.
MIN_PY = (3, 11)
_VERSION_GATE = f"import sys; sys.exit(0 if sys.version_info >= {MIN_PY} else 1)"


# ---------- output helpers ----------

# Windows consoles and pipes still default to cp1252, and this script prints
# box-drawing rules, arrows and a check mark. On 2026-08-18 (Windows 11,
# Python 3.14) a bare print() of the "installing into ..." rule died with
# UnicodeEncodeError *after* the venv install succeeded but *before* any config
# dir was written -- a half-done install that reported a traceback instead of a
# reason. Force UTF-8 where the stream allows it and degrade to replacement
# characters otherwise; an installer must never abort over a glyph.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass


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

    # The `dev` extra (pytest) is installed SEPARATELY and non-fatally, on purpose.
    # It has to be installed for run_tests() to have anything to run -- until
    # 2026-08-25 no installer installed it, so a fresh venv could not run the suite
    # at all, and a test that had never passed on macOS or Linux sat red through a
    # whole review cycle without anyone seeing it.
    #
    # But it must not be able to FAIL the install: a machine that has the real
    # dependencies in its pip cache and not pytest would otherwise lose its memory
    # system over a testing convenience. A missing pytest degrades to a skipped
    # self-test, which run_tests() reports honestly.
    res = subprocess.run(
        [str(VENV_PIP), "install", "--quiet", f"{MCP_SERVER_DIR}[dev]"],
        check=False, capture_output=True, text=True,
    )
    if res.returncode != 0:
        warn("could not install the 'dev' extra (pytest); the self-test step will be skipped.")


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


def run_tests(num: int, total: int, skip: bool) -> tuple[bool, str]:
    """Run the package's own test suite against the source tree.

    Returns (ok, reason). A failure never blocks the install -- by this point the
    venv is built and the wiring still needs to land -- but it is reported loudly
    and makes the process exit nonzero, the same contract a refused settings.json
    already has. Silence is what this step exists to end.
    """
    if skip:
        step(num, total, "self-test skipped (--skip-tests)")
        return True, "skipped"
    tests_dir = MCP_SERVER_DIR / "tests"
    if not tests_dir.is_dir():
        step(num, total, "self-test skipped (no tests/ in this checkout)")
        return True, "no tests"

    step(num, total, "running the test suite")
    env = os.environ.copy()
    # conftest.py builds a throwaway vault and points BRAIN_VAULT at it. Drop any
    # inherited value so the suite cannot be steered at -- or write into -- the
    # user's real vault, and so it runs under the same env developers run it under.
    env.pop("BRAIN_VAULT", None)
    try:
        res = subprocess.run(
            [str(VENV_PY), "-m", "pytest", "-q"],
            cwd=str(MCP_SERVER_DIR), env=env,
            capture_output=True, text=True, check=False, timeout=900,
        )
    except subprocess.TimeoutExpired:
        warn("the test suite timed out after 15 minutes; skipping.")
        return False, "timed out"
    except OSError as exc:
        warn(f"could not run the test suite: {exc}")
        return False, str(exc)

    out = (res.stdout or "") + (res.stderr or "")
    if "No module named pytest" in out:
        step(num, total, "self-test skipped (pytest not installed)")
        return True, "pytest missing"
    if res.returncode == 5:  # pytest's "no tests collected"
        warn("the test suite collected no tests.")
        return True, "no tests collected"
    if res.returncode != 0:
        info("")
        for line in out.strip().splitlines()[-25:]:
            info(f"       {line}")
        info("")
        return False, f"pytest exited {res.returncode}"

    summary = next(
        (ln.strip() for ln in reversed(out.strip().splitlines()) if "passed" in ln or "skipped" in ln),
        "",
    )
    if summary:
        info(f"       {summary}")
    return True, ""


def ensure_brain_layout(vault_root: Path) -> None:
    for sub in ("user", "feedback", "references", "projects"):
        (vault_root / "Brain" / sub).mkdir(parents=True, exist_ok=True)


def _slashed(path: Path) -> str:
    """Forward slashes on Windows: Claude Code often runs Bash-tool commands through
    Git Bash, which eats single backslashes. Forward slashes work in cmd.exe, bash
    and python.exe alike. POSIX paths are returned untouched."""
    return str(path).replace("\\", "/") if IS_WINDOWS else str(path)


def render_agent_launcher(vault_root: Path) -> str:
    """The model-facing entry point: a Python file, on purpose, on every platform.

    No shell ever re-parses the model's arguments on the way in. The previous
    Windows shape, a `brain.cmd` wrapper forwarding `%*`, was a command-injection
    hole: cmd.exe re-expands `%*` after Git Bash has finished quoting, so a query
    argument of `x"&echo pwned` ran `echo pwned` as a second command through the
    pre-approved wrapper (the BatBadBut class, CVE-2024-24576) -- and cmd expanded
    any `%VAR%` inside arguments too. The previous POSIX shape,
    `BRAIN_AGENT_SURFACE=1 BRAIN_VAULT="..." "/venv/bin/brain"`, had the opposite
    problem: Claude Code's allow-rule matcher does not reliably match past a
    leading `VAR=value` assignment, so the rule may never have applied. A Python
    launcher run by the venv interpreter has neither: python.exe receives argv
    already split, the two variables are set from inside the process, and the
    approved prefix is two plain quoted paths.

    The vault path is embedded with repr(), so backslashes and quotes are Python
    literals rather than escape sequences. The launcher must be run by the venv's
    python -- that is what the pre-approved command names -- so `brain_mcp` imports
    without any sys.path manipulation.
    """
    return (
        "#!/usr/bin/env python3\n"
        f"# {MANAGED_MARKER}\n"
        "# Generated by brain-setup.py - do not edit by hand. Re-run brain-setup.py to regenerate.\n"
        '"""The pre-approved Brain CLI entry point for this Claude config dir.\n'
        "\n"
        "Run by the venv's python (the approved command is `\"<venv python>\" \"<this file>\"`),\n"
        "so `brain_mcp` imports with no sys.path games. It is a Python file rather than a\n"
        "batch or shell wrapper so that no shell re-parses the model's arguments: cmd.exe\n"
        "re-expands `%*` after the caller has quoted it, which turned `x\"&echo pwned` into a\n"
        "second command (BatBadBut, CVE-2024-24576). Python receives argv already split,\n"
        "and nothing here reads it.\n"
        "\n"
        "BRAIN_AGENT_SURFACE=1 makes the CLI refuse --file, --from-pi and --from-cherryd:\n"
        "this invocation runs unattended, so it may not read arbitrary local files into the\n"
        "vault. Operators run the venv's `brain` executable directly and keep the full CLI.\n"
        '"""\n'
        "import os\n"
        "\n"
        f"os.environ[\"BRAIN_VAULT\"] = {str(vault_root)!r}\n"
        'os.environ["BRAIN_AGENT_SURFACE"] = "1"\n'
        "\n"
        "from brain_mcp.cli import main  # noqa: E402  (the environment must be set first)\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    main()\n"
    )


def write_agent_launcher(claude_dir: Path, vault_root: Path) -> Path:
    launcher = claude_dir / AGENT_LAUNCHER_NAME
    _write_managed(launcher, render_agent_launcher(vault_root))
    return launcher


def brain_cmd_token(claude_dir: Path, vault_root: Path) -> str:
    """The invocation substituted for __BRAIN_CMD__ in the rendered templates.

    `"<venv python>" "<config-dir>/brain-agent.py"` on every platform: two quoted
    paths, no env-var prefix, no `.cmd`. Writing the launcher is a side effect on
    purpose -- the token must never name a file that does not exist. This string is
    also what `permissions.allow` pre-approves (one rule per agent subcommand), so
    everything it runs is unattended; the launcher carries BRAIN_AGENT_SURFACE=1 for
    exactly that reason, and an invocation that bypasses it is simply not
    pre-approved and prompts like any other command.
    """
    launcher = write_agent_launcher(claude_dir, vault_root)
    return f'"{_slashed(VENV_PY)}" "{_slashed(launcher)}"'


def _write_managed(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write a file we own; back up a hand-written one first and say so.

    A global CLAUDE.md the user wrote themselves is their configuration, not ours
    to replace silently. The marker is how we tell the difference -- see
    brain_settings_merge.write_managed_text.
    """
    backup = brain_settings_merge.write_managed_text(path, text, encoding=encoding)
    if backup is not None:
        info(f"       existing {path.name} had no managed-by marker (not ours) -- backed it up to")
        info(f"         {backup}")


def render_global_claude_md(claude_dir: Path, vault_root: Path, cmd: str) -> None:
    template = (TEMPLATES_DIR / "global-CLAUDE.md").read_text(encoding="utf-8")
    rendered = template.replace("__BRAIN_VAULT__", str(vault_root)).replace("__BRAIN_CMD__", cmd)
    _write_managed(claude_dir / "CLAUDE.md", rendered)


def copy_brain_skill(claude_dir: Path, cmd: str) -> None:
    src = TEMPLATES_DIR / "skills" / "brain" / "SKILL.md"
    dst_dir = claude_dir / "skills" / "brain"
    dst_dir.mkdir(parents=True, exist_ok=True)
    template = src.read_text(encoding="utf-8")
    _write_managed(dst_dir / "SKILL.md", template.replace("__BRAIN_CMD__", cmd))


class LauncherError(Exception):
    """A generated launcher cannot be written for this install's paths."""


def batch_file_encoding() -> str:
    """The codepage cmd.exe reads a batch file in.

    That is the OEM codepage (cp437, cp850, cp936, ...), not UTF-8 and not the
    ANSI codepage Python's locale reports. A `.cmd` written as UTF-8 with a
    non-ASCII path in it sets BRAIN_VAULT to a directory that does not exist, and
    every hook then fails silently. Falls back to the ANSI codepage when the OEM
    query is unavailable -- still a real Windows codepage -- and to ASCII off
    Windows, where a batch file is never executed anyway.
    """
    if not IS_WINDOWS:
        return "ascii"
    try:
        import ctypes

        oem = ctypes.windll.kernel32.GetOEMCP()  # type: ignore[attr-defined]
        if oem:
            return codecs.lookup(f"cp{oem}").name
    except (AttributeError, OSError, LookupError, ValueError):
        pass
    return "mbcs"


def _batch_literal(path: Path) -> str:
    """A path baked into a batch line: `%` doubled so cmd does not expand it.

    A bare `%` in a batch file is a variable reference, so a vault at `D:\\v%1x`
    would splice the hook's first argument into BRAIN_VAULT. Only applied to the
    paths we bake in -- `%*`, `%~1` and `%ERRORLEVEL%` are meant to expand.
    """
    return str(path).replace("%", "%%")


def write_windows_launch_cmd(
    claude_dir: Path, vault_root: Path, encoding: str | None = None
) -> Path:
    """Generate the per-install brain-launch.cmd the hook commands run (Windows).

    HOOK-facing only, and it stays a batch file for that reason: its arguments are
    fixed hook names from settings.json, never text a model chose. The model-facing
    launcher is `brain-agent.py`. Raises LauncherError, with a message a user can
    act on, when a path cannot be expressed in the OEM codepage.

    Arguments after the hook name are forwarded (`%2`..`%9`): the preload hooks
    are registered as `<launch.cmd> session_start --part I --parts N` (see
    vault.PRELOAD_PARTS) and a launcher that dropped them would silently deliver
    the whole bundle from every entry, N times over.
    """
    encoding = encoding or batch_file_encoding()
    launch_cmd = claude_dir / LAUNCH_CMD_NAME
    body = (
        "@echo off\r\n"
        f"rem {MANAGED_MARKER}\r\n"
        "rem Generated by brain-setup.py - do not edit by hand. Re-run brain-setup.py to regenerate.\r\n"
        "setlocal\r\n"
        f'set "BRAIN_VAULT={_batch_literal(vault_root)}"\r\n'
        f'"{_batch_literal(VENV_PY)}" "{_batch_literal(HOOKS_DIR)}\\%~1.py" %2 %3 %4 %5 %6 %7 %8 %9\r\n'
        "exit /b %ERRORLEVEL%\r\n"
    )
    try:
        _write_managed(launch_cmd, body, encoding=encoding)
    except UnicodeEncodeError as exc:
        bad = exc.object[exc.start:exc.end]
        raise LauncherError(
            f"cannot write {launch_cmd}: cmd.exe reads batch files in the OEM codepage "
            f"({encoding}), and {bad!r} in one of the baked-in paths (vault, repo, or "
            f"config dir) cannot be encoded there. The hooks would set BRAIN_VAULT to a "
            f"path that does not exist. Move that directory to a path this codepage can "
            f"express (ASCII is always safe) and re-run."
        ) from exc
    return launch_cmd


def merge_settings_json(claude_dir: Path, vault_root: Path, cmd: str) -> tuple[bool, str]:
    """Merge the Brain hook block + permission rule into settings.json.

    Returns (ok, reason). The merge itself — pruning our old entries, APPENDING our
    groups to whatever third-party hooks already exist for the same events, the
    refusal to rewrite an unparseable file, the backup and the atomic write — lives
    in brain_settings_merge so all four installers share one implementation.
    """
    settings_path = claude_dir / "settings.json"
    if IS_WINDOWS:
        try:
            launch_cmd = write_windows_launch_cmd(claude_dir, vault_root)
        except LauncherError as exc:
            warn(str(exc))
            return False, str(exc)
        template_path = TEMPLATES_DIR / "settings.hooks.win.json"
        kwargs = {"brain_launch": str(launch_cmd)}
    else:
        template_path = TEMPLATES_DIR / "settings.hooks.json"
        kwargs = {
            "brain_python": str(VENV_PY),
            "brain_hooks": str(HOOKS_DIR),
            "brain_vault": str(vault_root),
        }

    try:
        report = brain_settings_merge.merge(
            settings_path,
            template_path,
            brain_cmd=cmd,
            **kwargs,
        )
    except brain_settings_merge.SettingsError as exc:
        warn(str(exc))
        for line in brain_settings_merge.REPAIR_HINT.splitlines():
            warn(line.strip())
        return False, str(exc)
    except OSError as exc:
        warn(f"could not write {settings_path}: {exc} (original left unchanged)")
        return False, f"could not write {settings_path}: {exc}"

    if report["backup"]:
        info(f"       backup of previous settings.json: {report['backup']}")
    return True, ""


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


def _is_brain_only_mcp_json(path: Path) -> bool:
    """True when `.mcp.json` holds nothing but a Brain registration.

    The file is only ours to delete if we are the only thing in it: a user who
    keeps their own project-scope servers in a `.mcp.json` inside the config dir
    would otherwise lose them to a "cleanup" that never looked.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    if not isinstance(data, dict) or set(data) - {"mcpServers"}:
        return False
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or set(servers) != {"brain"}:
        return False
    return "brain_mcp" in json.dumps(servers.get("brain"))


def cleanup(claude_dir: Path) -> None:
    """Remove leftovers of earlier install shapes.

    - `.mcp.json`: an early setup wrote one here, but Claude Code only reads that
      file from a project dir, never a config dir, so it never worked. Deleted only
      when it is a Brain-only registration.
    - `brain.cmd`: the retired model-facing wrapper (command injection through
      `%*`, see render_agent_launcher). Its permission rules were pruned by the
      settings merge; the file itself is deleted so nothing can be pointed back at
      it. Only when it says it was generated -- a file we cannot identify is not
      ours to remove.
    """
    stale = claude_dir / ".mcp.json"
    if stale.exists():
        if _is_brain_only_mcp_json(stale):
            try:
                stale.unlink()
                info(f"       removed obsolete {stale.name} (a Brain-only registration that never worked here)")
            except OSError as exc:
                warn(f"could not remove {stale}: {exc}")
        else:
            info(f"       leaving {stale} alone: it is not a Brain-only registration")

    legacy = claude_dir / LEGACY_BRAIN_CMD_NAME
    if legacy.exists():
        if brain_settings_merge.is_generated_launcher(legacy):
            try:
                legacy.unlink()
                info(f"       removed legacy {legacy.name} (superseded by {AGENT_LAUNCHER_NAME}; "
                     f"its permission rules were pruned)")
            except OSError as exc:
                warn(f"could not remove legacy {legacy}: {exc} -- delete it by hand; it is no longer pre-approved")
        else:
            warn(f"{legacy} exists but does not say it was generated by a Brain installer; leaving it alone")


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


def install_one(claude_dir: Path, vault_root: Path, with_mcp: bool) -> dict:
    """Install brain wiring into one Claude config dir.

    Returns a per-step result so the summary can be accurate about which parts of
    the install actually landed — a settings.json we refuse to touch leaves the
    CLAUDE.md and skill correctly installed but no hooks, and saying "installed"
    for that would be a lie.
    """
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
    settings_ok, settings_reason = merge_settings_json(claude_dir, vault_root, cmd)

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
    return {
        "claude_dir": claude_dir,
        "mcp_ok": ok,
        "mcp_reason": reason,
        "settings_ok": settings_ok,
        "settings_reason": settings_reason,
    }


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
    parser.add_argument("--skip-tests", action="store_true",
                        help="skip the self-test step. The suite runs by default and a "
                             "failure makes this installer exit nonzero (the install "
                             "itself still completes).")
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

    # ---- shared install (venv, deps, warm-up, self-test — done once regardless of #claude dirs) ----
    info("Preparing brain-mcp")
    ensure_venv(1, 5)
    install_brain_mcp(2, 5)
    sanity_import(3, 5, vault_root)
    warm_embedder(4, 5, vault_root)
    tests_ok, tests_reason = run_tests(5, 5, args.skip_tests)

    # ---- per-claude-dir wiring ----
    results: list[dict] = []
    for cd in claude_dirs:
        results.append(install_one(cd, vault_root, args.with_mcp))

    info("")
    settings_failures = [r for r in results if not r["settings_ok"]]
    if settings_failures:
        info("✗ PARTIAL INSTALLATION — settings.json was NOT modified for:")
        for r in settings_failures:
            info(f"    {r['claude_dir']}")
            info(f"      reason: {r['settings_reason']}")
        info("")
        info("   These steps DID succeed for those dirs:")
        info("     - the venv and brain-mcp install")
        info("     - <config-dir>/CLAUDE.md")
        info("     - <config-dir>/skills/brain/SKILL.md")
        info(f"     - <config-dir>/{AGENT_LAUNCHER_NAME}")
        info("   These did NOT:")
        info("     - the SessionStart/Stop/PreCompact/... hook wiring (no preload, no")
        info("       checkpoints, no stop-gate)")
        info("     - the Bash(<brain cmd> <subcommand>:*) permission rules (proactive saves will prompt)")
        info("")
        info("   Repair the settings.json listed above and re-run this installer.")
        info("")

    if not tests_ok:
        info(f"✗ SELF-TEST FAILED ({tests_reason}) — the wiring above still installed.")
        info("")
        info("   The Brain will work, but this checkout is not behaving as its own tests")
        info("   expect, so trust it accordingly. Re-run the suite yourself with:")
        info(f"     cd {MCP_SERVER_DIR} && {VENV_PY} -m pytest")
        info("   Re-run this installer with --skip-tests to bypass this step.")
        info("")

    failures = [(r["claude_dir"], r["mcp_reason"]) for r in results if not r["mcp_ok"]]
    if not failures:
        if not settings_failures:
            info("✓ Brain installed." if tests_ok else "✓ Brain installed (self-test failed — see above).")
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

    # A refused settings.json means no hooks: the Brain will not preload, checkpoint,
    # or gate save-promises. Exit nonzero so a scripted install can't call that a win.
    # A failing self-test gets the same treatment for the same reason: the one thing
    # that must not happen to a red suite is that it passes unnoticed.
    if settings_failures:
        sys.exit(1)
    if not tests_ok:
        sys.exit(4)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\naborted")
        sys.exit(130)
