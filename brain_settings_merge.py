#!/usr/bin/env python3
"""The one settings.json merge/prune algorithm, shared by every installer.

WHY THIS FILE EXISTS
    The Brain once had four install paths and four uninstall paths, each with its
    own hand-maintained copy of the hook-merge logic -- three of them as embedded
    Python heredocs. Every bug cluster in this repo's history is "a fix landed at
    one of N sites", and this was the widest N. The shell and PowerShell scripts
    are retired (ROADMAP 3G); `brain-setup.py` and `brain-uninstall.py` both import
    this module, and its `merge`/`prune` CLI still works without the venv.

    Stdlib only, and deliberately runnable by a bare system python3 - the
    uninstallers run it after the venv may already be gone.

WHAT IT GUARANTEES
    - Brain hook groups are APPENDED to each event's surviving groups, never
      assigned over them. A user's own SessionStart/Stop/PreCompact hook used to
      be silently deleted on install (`settings["hooks"][event] = definition`).
    - Re-running is idempotent: Brain-owned entries are pruned before the current
      template is appended, so nothing accumulates.
    - Unparseable settings.json is NEVER rewritten. It used to be caught, treated
      as `{}`, and written back - erasing the user's whole Claude configuration to
      install a hook block.
    - A timestamped backup is taken before any mutation of a file with content,
      and the write is atomic (same-dir temp file + os.replace), so a crash
      mid-write cannot truncate settings.json.

OWNERSHIP DETECTION
    A hook command is Brain-owned when it mentions `BRAIN_VAULT=`, `brain-launch`,
    or the repo's `hooks/` directory (in either slash direction, case-insensitively,
    quoted or not). Install and uninstall share this predicate on purpose - an
    asymmetry here means either orphaned hooks after uninstall or duplicated hooks
    after reinstall. The `BRAIN_VAULT=` clause is deliberately broad: a third-party
    hook that exports our vault variable is, for these purposes, ours.

MANAGED FILES
    The installer also writes files that are not settings.json - the global
    CLAUDE.md, the brain skill, and the generated launchers. `MANAGED_MARKER` is how
    both halves tell a file we wrote from one the user wrote: the installer backs up
    an unmarked file before replacing it, the uninstaller leaves an unmarked file
    alone. `write_managed_text` is the one writer for those files.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

BACKUP_SUFFIX = ".brain-backup-"

# Exit code for "the existing settings.json was not safe to touch". Distinct from
# 1 so a caller can tell a refusal from a crash.
EXIT_SETTINGS_UNSAFE = 3


class SettingsError(Exception):
    """The existing settings.json cannot be safely rewritten."""


# ---------------------------------------------------------------- ownership --

def _path_variants(raw: str) -> list[str]:
    """Both slash directions for a path, lowercased.

    settings.json may hold either form on Windows: the installer writes forward
    slashes (Git Bash eats single backslashes), but a hand-edit or an older
    install may have backslashes.
    """
    if not raw:
        return []
    low = raw.lower()
    return list({low, low.replace("\\", "/"), low.replace("/", "\\")})


def is_brain_command(cmd: object, hooks_dir: str = "", launch_cmd: str = "") -> bool:
    """True when a hook command belongs to the Brain install."""
    if not isinstance(cmd, str):
        return False
    low = cmd.lower()
    if "brain_vault=" in low or "brain-launch" in low:
        return True
    # Substring matching, deliberately: the rendered commands quote every path
    # (a space in a username must not split a hook command), and a quoted path
    # still contains the bare one.
    for candidate in (hooks_dir, launch_cmd):
        for variant in _path_variants(candidate):
            if variant and variant in low:
                return True
    return False


# The subcommands a model may run unattended. `save`/`checkpoint` are here because
# proactive memory is the whole point of the Brain; their file-import options are shut
# off by the CLI's own BRAIN_AGENT_SURFACE gate, not by this list. Absent on purpose:
# `reindex` (a ~300s CPU burn, an operator's call) and anything added later, which has
# to be added here deliberately rather than inherited from a blanket rule.
AGENT_SUBCOMMANDS = (
    "recall", "save", "list", "forget", "checkpoint", "stats", "doctor",
)


def is_brain_permission_rule(rule: object) -> bool:
    """True for a Brain CLI pre-approval, in ANY shape we have ever written.

    Must recognize the superseded blanket rules too, not just what this version
    emits: prune runs before re-add, so a shape this misses is a stale standing
    approval that survives every future re-install. Four generations so far --
    `Bash(BRAIN_VAULT=... /bin/brain:*)`, the Windows `brain.cmd` wrapper path, the
    `BRAIN_AGENT_SURFACE=1`-prefixed POSIX per-subcommand rules, and the current
    `"<venv python>" "<config>/brain-agent.py"` launcher on every platform.

    The `.cmd` and env-prefix shapes are gone for a reason, not just superseded:
    cmd.exe re-expands `%*` after the caller's quoting is done, so a Git Bash
    argument like `x"&echo pwned` ran a second command through the pre-approved
    wrapper (the BatBadBut class, CVE-2024-24576), and Claude Code's rule matcher
    does not reliably match past a leading `VAR=value` assignment. Pruning them on
    re-install is what closes the hole for an existing install.
    """
    if not isinstance(rule, str) or not rule.startswith("Bash("):
        return False
    low = rule.lower()
    if "brain.cmd" in low or "brain-agent.py" in low or "brain_agent_surface=" in low:
        return True
    return rule.startswith("Bash(BRAIN_VAULT=") and "/bin/brain" in rule


# ------------------------------------------------------------------ safe IO --

def load_settings(path: Path) -> dict:
    """Parse settings.json, or raise SettingsError rather than clobber it.

    Missing file and empty/whitespace-only file both mean `{}` - the installers
    used to create `{}` themselves in exactly those cases, and there is nothing
    to lose. Anything else that will not parse into a JSON object is a refusal:
    silently replacing it would delete every permission, env var, and setting the
    user has.
    """
    if not path.exists():
        return {}
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SettingsError(f"cannot read {path}: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SettingsError(
            f"{path} is not valid UTF-8 ({exc}). Refusing to rewrite it."
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SettingsError(
            f"{path} contains a JSON {type(data).__name__}, not an object. "
            f"Claude Code settings must be a top-level JSON object."
        )
    return data


def backup_path_for(path: Path, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(path.name + BACKUP_SUFFIX + stamp)
    n = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}{BACKUP_SUFFIX}{stamp}-{n}")
        n += 1
    return candidate


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write via a same-directory temp file + os.replace.

    Same-directory because os.replace is only atomic within one filesystem. On any
    failure the temp file is removed and the original is left exactly as it was - a
    settings.json half-written by an interrupted installer is unrecoverable.

    `encoding` exists for exactly one caller: the Windows batch launcher, which
    cmd.exe reads in the OEM codepage. Everything else is UTF-8. The encode happens
    BEFORE the temp file is created, so an unencodable path raises a plain
    UnicodeEncodeError and leaves no temp file behind. No newline translation:
    callers spell their own line endings (LF for text, CRLF for batch).
    """
    data = text.encode(encoding)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_settings(path: Path, settings: dict) -> tuple[bool, Path | None]:
    """Persist `settings`. Returns (wrote, backup_path).

    A no-op re-run writes nothing and takes no backup, so repeated `setup` runs do
    not litter the config dir with identical backups.
    """
    # ensure_ascii=False: the file is UTF-8, and a path with an accented username
    # should read as one in the file, not as a `\u00e9` escape.
    text = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    new_bytes = text.encode("utf-8")
    try:
        old_bytes = path.read_bytes() if path.exists() else None
    except OSError as exc:
        raise SettingsError(f"cannot read {path}: {exc}") from exc
    if old_bytes == new_bytes:
        return False, None
    backup = None
    if old_bytes is not None and old_bytes.strip():
        backup = backup_path_for(path)
        try:
            backup.write_bytes(old_bytes)
        except OSError as exc:
            raise SettingsError(f"cannot write backup {backup}: {exc}") from exc
    atomic_write_text(path, text)
    return True, backup


# ------------------------------------------------------------ managed files --

MANAGED_MARKER = "<!-- managed-by: ai-brain -->"

# How far into a file the marker may sit. It is line 1 of the global CLAUDE.md, but
# the brain skill has YAML frontmatter that must start at line 1, so its marker is
# the first line after the closing `---`; the generated launchers carry it in a
# comment near the top. Head-only so a huge unrelated file is not read whole.
MARKER_HEAD_LINES = 12

# What the generated launchers say about themselves. The retired Windows installer
# wrote the second form; an install it produced still has that brain.cmd on disk,
# and the uninstaller has to recognise it to remove it.
GENERATED_BY_LINES = (
    "Generated by brain-setup.py",
    "Generated by setup-windows.ps1",
)


def _head_lines(path: Path) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return [next(fh, "") for _ in range(MARKER_HEAD_LINES)]
    except OSError:
        return []


def has_managed_marker(path: Path) -> bool:
    """True when `path` carries the managed-by marker in its head.

    The predicate both halves use: the installer backs up a file WITHOUT it before
    overwriting, the uninstaller refuses to delete one without it. A hand-written
    global CLAUDE.md is the user's own configuration and must survive both.
    """
    return any(MANAGED_MARKER in line for line in _head_lines(path))


def is_generated_launcher(path: Path) -> bool:
    """True for a launcher this installer (or the retired one) generated."""
    return any(
        MANAGED_MARKER in line or any(tag in line for tag in GENERATED_BY_LINES)
        for line in _head_lines(path)
    )


def backup_file(path: Path) -> Path:
    """Copy `path` to a timestamped sibling (`<name>.brain-backup-<stamp>`)."""
    backup = backup_path_for(path)
    backup.write_bytes(path.read_bytes())
    return backup


def write_managed_text(path: Path, text: str, encoding: str = "utf-8") -> Path | None:
    """Write a file the installer owns, backing up a user-written one first.

    Returns the backup path when one was taken, else None. Two rules:
      - an existing file WITHOUT the managed marker is somebody's hand-written
        configuration; it is backed up before being replaced, never silently lost;
      - a file WITH the marker is ours from a previous run and is replaced in place.
    The write is atomic either way. `text` itself must carry the marker, or the next
    run would back it up as a user file - asserted here so a template cannot drift
    out of the scheme unnoticed.
    """
    if not any(MANAGED_MARKER in line for line in text.splitlines()[:MARKER_HEAD_LINES]):
        raise ValueError(
            f"refusing to write {path.name} without {MANAGED_MARKER!r} in its first "
            f"{MARKER_HEAD_LINES} lines: the next run could not tell it from a user file"
        )
    backup = None
    if path.exists() and not has_managed_marker(path):
        backup = backup_file(path)
    atomic_write_text(path, text, encoding=encoding)
    return backup


# -------------------------------------------------------------------- merge --

def _hooks_map(settings: dict) -> dict:
    existing = settings.get("hooks") or {}
    if not isinstance(existing, dict):
        # A non-dict `hooks` is not something we can merge into; start clean rather
        # than crash. (Claude Code would reject such a block anyway.)
        return {}
    return existing


def prune_brain_hooks(settings: dict, hooks_dir: str = "", launch_cmd: str = "") -> int:
    """Remove Brain-owned hook entries, leaving every third-party entry in place.

    Non-dict groups and non-list inner blocks are passed through untouched - they
    are somebody else's malformed config, not ours to normalize. Returns the count
    removed.
    """
    if not isinstance(settings.get("hooks"), dict):
        return 0
    existing = settings["hooks"]
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
            inner = group.get("hooks")
            if not isinstance(inner, list):
                pruned_groups.append(group)
                continue
            kept = []
            for hook in inner:
                if isinstance(hook, dict) and is_brain_command(
                    hook.get("command", ""), hooks_dir, launch_cmd
                ):
                    removed += 1
                else:
                    kept.append(hook)
            if kept:
                new_group = dict(group)
                new_group["hooks"] = kept
                pruned_groups.append(new_group)
        if pruned_groups:
            existing[event] = pruned_groups
        else:
            del existing[event]
    return removed


def append_brain_hooks(settings: dict, hooks_block: dict) -> None:
    """Append our groups to each event's SURVIVING groups.

    The bug this replaces: `settings["hooks"][event] = definition`, which threw away
    every third-party group registered for that event. Claude Code runs all groups of
    an event, so appending is both correct and non-destructive.
    """
    existing = _hooks_map(settings)
    for event, definition in hooks_block.items():
        surviving = existing.get(event)
        if surviving is None:
            surviving = []
        elif not isinstance(surviving, list):
            # Malformed but non-Brain - preserve the payload by wrapping it rather
            # than dropping it on the floor.
            surviving = [surviving]
        existing[event] = list(surviving) + list(definition)
    settings["hooks"] = existing


def merge_permission_rule(settings: dict, brain_cmd: str) -> None:
    """Pre-approve the brain CLI so proactive saves never hit a permission prompt.

    An unanswered prompt is indistinguishable from the model deciding not to save, so
    without this the Brain looks like it is quietly ignoring the user. Brain-owned
    rules are pruned first so a re-run (or a moved vault/venv) cannot accumulate them.

    One rule PER SUBCOMMAND rather than a single `Bash(<brain_cmd>:*)`. The blanket
    rule pre-approved the entire CLI including subcommands a model has no business
    running unattended, and it would silently pre-approve every subcommand added in
    future. This is defence in depth only -- prefix rules cannot see option flags, so
    `Bash(<cmd> save:*)` still matches `save --file <anything>`. The enforceable half
    is the CLI's BRAIN_AGENT_SURFACE gate, which `brain_cmd` carries.
    """
    perms = settings.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
    allow = perms.get("allow")
    if not isinstance(allow, list):
        allow = []
    allow = [r for r in allow if not is_brain_permission_rule(r)]
    allow.extend(f"Bash({brain_cmd} {sub}:*)" for sub in AGENT_SUBCOMMANDS)
    perms["allow"] = allow
    settings["permissions"] = perms


def prune_permission_rules(settings: dict) -> int:
    """Uninstall counterpart of merge_permission_rule.

    Install and uninstall must stay symmetric: until 2026-08-24 the allow rule was
    written by the installers and removed by none of them, leaving a standing
    unprompted Bash approval for a deleted path.
    """
    perms = settings.get("permissions")
    if not isinstance(perms, dict):
        return 0
    allow = perms.get("allow")
    if not isinstance(allow, list):
        return 0
    kept = [r for r in allow if not is_brain_permission_rule(r)]
    removed = len(allow) - len(kept)
    if not removed:
        return 0
    if kept:
        perms["allow"] = kept
    else:
        perms.pop("allow", None)
    if not perms:
        settings.pop("permissions", None)
    return removed


AUTO_MEMORY_KEY = "autoMemoryEnabled"
# Sidecar next to settings.json recording what the key held before install turned
# it off. settings.json itself cannot carry an ownership marker, and without one
# uninstall could not tell our `false` from a user's own.
AUTO_MEMORY_MARKER = ".brain-auto-memory.json"


def auto_memory_marker_path(settings_path: Path) -> Path:
    return settings_path.with_name(AUTO_MEMORY_MARKER)


def disable_auto_memory(settings: dict) -> dict | None:
    """Turn off Claude Code's built-in auto memory; return the prior state if changed.

    Auto memory is on by default, and its system-prompt section tells the model to
    save user/feedback/project/reference notes under `<config>/projects/*/memory/` --
    the Brain's own taxonomy, machine-local, invisible to every other agent, and in
    direct contradiction of the global CLAUDE.md. The system prompt outranks CLAUDE.md
    (which arrives as a user message), so two memory systems compete in every session.
    Returns None when the key was already `false`, else `{"present": bool, "value": v}`.
    """
    present = AUTO_MEMORY_KEY in settings
    previous = settings.get(AUTO_MEMORY_KEY)
    if present and previous is False:
        return None
    settings[AUTO_MEMORY_KEY] = False
    return {"present": present, "value": previous}


def restore_auto_memory(settings: dict, prior: object) -> bool:
    """Uninstall counterpart: put back what install replaced, if it is still ours.

    Only a key that still reads `false` is touched -- if the user has turned auto
    memory back on since, that is their choice and it stays.
    """
    if not isinstance(prior, dict) or settings.get(AUTO_MEMORY_KEY) is not False:
        return False
    if prior.get("present"):
        settings[AUTO_MEMORY_KEY] = prior.get("value")
    else:
        settings.pop(AUTO_MEMORY_KEY, None)
    return True

INSTALLS_FILE = ".brain-installs.json"


def _installs_path(repo_dir: Path) -> Path:
    return Path(repo_dir) / INSTALLS_FILE


def recorded_installs(repo_dir: Path) -> list[Path]:
    """Config dirs setup has installed into, as recorded at the repo root.

    The venv is shared by every config dir, so uninstall must find all of them before
    deleting it -- and a config dir can be any path, not just `~/.claude*`. A missing
    or unreadable record is an empty list: it only ever adds candidates to check.
    """
    try:
        data = json.loads(_installs_path(repo_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    dirs = data.get("config_dirs") if isinstance(data, dict) else None
    return [Path(d) for d in dirs if isinstance(d, str)] if isinstance(dirs, list) else []


def _write_installs(repo_dir: Path, dirs: list[Path]) -> None:
    unique = sorted({str(d) for d in dirs})
    atomic_write_text(_installs_path(repo_dir), json.dumps({"config_dirs": unique}, indent=2) + "\n")


def record_install(repo_dir: Path, claude_dir: Path) -> None:
    dirs = recorded_installs(repo_dir)
    if str(claude_dir) not in {str(d) for d in dirs}:
        _write_installs(repo_dir, dirs + [Path(claude_dir)])


def forget_installs(repo_dir: Path, claude_dirs: list[Path]) -> None:
    gone = {str(d) for d in claude_dirs}
    dirs = recorded_installs(repo_dir)
    kept = [d for d in dirs if str(d) not in gone]
    if len(kept) != len(dirs):
        _write_installs(repo_dir, kept)


def render_hooks_template(
    template_text: str,
    *,
    brain_python: str = "",
    brain_hooks: str = "",
    brain_vault: str = "",
    brain_launch: str = "",
) -> dict:
    """Substitute the placeholders and return the template's `hooks` block.

    Substitution happens on the PARSED structure, never on the JSON text. A
    backslash path spliced into JSON source is an escape sequence, not a path:
    `D:\\new\\tab` became a newline and a tab, `D:\\Users\\x` failed to parse at all.
    Walking the parsed strings makes any path a plain string value.

    `brain_launch` is forward-slashed here, not by the caller: Claude Code on Windows
    often runs hooks through Git Bash, which strips single backslashes as escapes, so
    a backslashed brain-launch.cmd path would reach the OS with its separators eaten.
    Forward slashes work in cmd.exe, bash and python.exe alike.
    """
    substitutions = {
        "__BRAIN_PYTHON__": brain_python,
        "__BRAIN_HOOKS__": brain_hooks,
        "__BRAIN_VAULT__": brain_vault,
        "__BRAIN_LAUNCH__": brain_launch.replace("\\", "/") if brain_launch else "",
    }

    def walk(node: object) -> object:
        if isinstance(node, str):
            for placeholder, value in substitutions.items():
                if value:
                    node = node.replace(placeholder, value)
            return node
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        return node

    try:
        data = json.loads(template_text)
    except json.JSONDecodeError as exc:
        raise SettingsError(f"hooks template is not valid JSON: {exc}") from exc
    block = walk(data.get("hooks") if isinstance(data, dict) else None)
    if not isinstance(block, dict):
        raise SettingsError("hooks template does not contain a `hooks` object")
    return block


def _assert_block_is_ownable(block: dict, hooks_dir: str, launch_cmd: str) -> None:
    """Every command we install must be recognized by our own ownership predicate.

    If it isn't, the next install cannot prune what this one wrote and the user
    accumulates a duplicate set of Brain hooks per run - the exact silent breakage
    this module exists to prevent. Fail loudly at install time instead.
    """
    orphans = []
    for event, groups in block.items():
        for group in groups if isinstance(groups, list) else []:
            inner = (group.get("hooks") or []) if isinstance(group, dict) else []
            for hook in inner:
                cmd = hook.get("command", "") if isinstance(hook, dict) else ""
                if not is_brain_command(cmd, hooks_dir, launch_cmd):
                    orphans.append(f"{event}: {cmd}")
    if orphans:
        raise SettingsError(
            "hook commands are not detectable as Brain-owned, so a re-install would "
            "duplicate them: " + "; ".join(orphans)
        )


def merge(
    settings_path: Path,
    template_path: Path,
    *,
    brain_cmd: str,
    brain_python: str = "",
    brain_hooks: str = "",
    brain_vault: str = "",
    brain_launch: str = "",
) -> dict:
    """Install path: prune Brain entries, append the current template, save.

    Returns a small report dict. Raises SettingsError when the existing file is not
    safe to rewrite - the caller must treat that as a failed installation step.
    """
    settings = load_settings(settings_path)
    template_text = template_path.read_text(encoding="utf-8")
    block = render_hooks_template(
        template_text,
        brain_python=brain_python,
        brain_hooks=brain_hooks,
        brain_vault=brain_vault,
        brain_launch=brain_launch,
    )
    _assert_block_is_ownable(block, brain_hooks, brain_launch)

    removed = prune_brain_hooks(settings, brain_hooks, brain_launch)
    append_brain_hooks(settings, block)
    prune_permission_rules(settings)
    merge_permission_rule(settings, brain_cmd)
    prior = disable_auto_memory(settings)

    wrote, backup = save_settings(settings_path, settings)
    # The marker is written only after settings.json landed, and never over an
    # existing one: a re-install after the user flipped the key back must keep the
    # state from before the *first* install, which is what uninstall restores.
    marker = auto_memory_marker_path(settings_path)
    if prior is not None and not marker.exists():
        atomic_write_text(marker, json.dumps(prior) + "\n")
    return {
        "pruned": removed,
        "events": sorted(block),
        "wrote": wrote,
        "backup": str(backup) if backup else "",
        "auto_memory": "disabled" if prior is not None else "already off",
    }


def prune(settings_path: Path, *, brain_hooks: str = "", brain_launch: str = "") -> dict:
    """Uninstall path: remove Brain-owned hooks and the allow rule, restore auto memory, save."""
    settings = load_settings(settings_path)
    removed = prune_brain_hooks(settings, brain_hooks, brain_launch)
    if isinstance(settings.get("hooks"), dict) and not settings["hooks"]:
        settings.pop("hooks", None)
    removed += prune_permission_rules(settings)

    marker = auto_memory_marker_path(settings_path)
    auto_memory = ""
    if marker.exists():
        try:
            prior = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prior = None
        if prior is None:
            auto_memory = "left as is (ownership marker unreadable)"
        elif restore_auto_memory(settings, prior):
            auto_memory = "restored to its pre-install value"
        else:
            auto_memory = "left as is (changed since install)"

    wrote, backup = save_settings(settings_path, settings)
    if marker.exists():
        marker.unlink()
    return {
        "removed": removed,
        "wrote": wrote,
        "backup": str(backup) if backup else "",
        "auto_memory": auto_memory,
    }


# ---------------------------------------------------------------------- CLI --

REPAIR_HINT = (
    "       Refusing to touch it: rewriting an unreadable settings.json would erase\n"
    "       every permission, env var and hook you have configured.\n"
    "       Fix the JSON (or move the file aside so a fresh one is created), then\n"
    "       re-run setup."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge or prune the Brain hook block in a Claude settings.json.",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    m = sub.add_parser("merge", help="install: prune Brain entries, append current template")
    m.add_argument("--settings", required=True)
    m.add_argument("--template", required=True)
    m.add_argument("--brain-cmd", required=True)
    m.add_argument("--brain-python", default="")
    m.add_argument("--brain-hooks", default="")
    m.add_argument("--brain-vault", default="")
    m.add_argument("--brain-launch", default="")

    p = sub.add_parser("prune", help="uninstall: remove Brain entries only")
    p.add_argument("--settings", required=True)
    p.add_argument("--brain-hooks", default="")
    p.add_argument("--brain-launch", default="")

    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

    settings_path = Path(args.settings)

    if args.mode == "prune":
        if not settings_path.exists():
            print("       (no settings.json - nothing to prune)")
            return 0
        try:
            report = prune(
                settings_path,
                brain_hooks=args.brain_hooks,
                brain_launch=args.brain_launch,
            )
        except (SettingsError, OSError) as exc:
            # Uninstall's policy is the safe one and always has been: an unparseable
            # file is left exactly as found, and that is not a failure.
            print(f"       (leaving settings.json alone: {exc})")
            return 0
        word = "entry" if report["removed"] == 1 else "entries"
        print(f"       [ok] removed {report['removed']} Brain-owned {word}")
        if report["auto_memory"]:
            print(f"       [ok] Claude Code auto memory setting {report['auto_memory']}")
        if report["backup"]:
            print(f"       backup: {report['backup']}")
        return 0

    try:
        report = merge(
            settings_path,
            Path(args.template),
            brain_cmd=args.brain_cmd,
            brain_python=args.brain_python,
            brain_hooks=args.brain_hooks,
            brain_vault=args.brain_vault,
            brain_launch=args.brain_launch,
        )
    except SettingsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(REPAIR_HINT, file=sys.stderr)
        return EXIT_SETTINGS_UNSAFE
    except OSError as exc:
        print(f"ERROR: could not write {settings_path}: {exc}", file=sys.stderr)
        print("       The original file was left unchanged.", file=sys.stderr)
        return EXIT_SETTINGS_UNSAFE

    if report["wrote"]:
        print(f"       [ok] hooks merged ({len(report['events'])} events)")
        if report["auto_memory"] == "disabled":
            print("       [ok] Claude Code auto memory disabled (the Brain replaces it)")
        if report["backup"]:
            print(f"       backup: {report['backup']}")
    else:
        print("       [ok] hooks already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
