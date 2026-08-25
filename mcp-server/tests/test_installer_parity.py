"""Static parity checks across the four installers.

The Brain has four independent install paths (`brain-setup.py`, `setup-mac.sh`,
`setup-linux.sh`, `setup-windows.ps1`) that must produce equivalent installs. Every
one of them is hand-maintained, so a fix reliably lands in whichever one the session
was looking at and the other three silently keep the bug. Two live instances found
2026-08-24: the `permissions.allow` rule reached `setup-mac.sh` (a596572) and
`setup-windows.ps1` but never `brain-setup.py` — the primary installer — or
`setup-linux.sh`; and PR #16's PowerShell stderr guard landed on one of the two
identical `claude mcp remove` call sites.

These are text assertions, not behavioural ones: running four installers for real
needs four operating systems. Text is enough to catch drift, which is the actual
failure mode.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

POSIX_INSTALLERS = ["setup-mac.sh", "setup-linux.sh"]
ALL_INSTALLERS = ["brain-setup.py", "setup-mac.sh", "setup-linux.sh", "setup-windows.ps1"]


def read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("installer", ALL_INSTALLERS)
def test_installer_exists(installer):
    assert (REPO_ROOT / installer).is_file(), f"{installer} is documented but missing"


@pytest.mark.parametrize("installer", ALL_INSTALLERS)
def test_every_installer_writes_the_permission_rule(installer):
    """Every installer must pre-approve the brain CLI in settings.json.

    Without `permissions.allow -> Bash(<brain_cmd>:*)` the model's proactive saves
    hit a permission prompt outside /brain skill turns, which defeats the whole
    automatic-memory design — and does it invisibly, since an unanswered prompt
    looks exactly like the model choosing not to save.
    """
    src = read(installer)
    assert "permissions" in src, (
        f"{installer} never touches settings['permissions'] — proactive brain saves "
        f"will hit permission prompts on installs made with it"
    )
    assert "Bash(" in src, f"{installer} does not construct a Bash(...) permission rule"


@pytest.mark.parametrize("installer", ALL_INSTALLERS)
def test_permission_rule_merge_is_idempotent(installer):
    """Re-running an installer must not append a duplicate allow rule.

    Each installer prunes Brain-owned rules before re-adding. The prune predicate has
    to match the rule that same installer writes, which is easy to get wrong because
    the rule format differs per platform: an env-prefixed `.../bin/brain` on POSIX,
    a `brain.cmd` wrapper path on Windows.
    """
    src = read(installer)
    assert re.search(r"for r in allow", src), (
        f"{installer} appends a permission rule without pruning prior Brain-owned "
        f"rules — re-running it would accumulate duplicates"
    )


@pytest.mark.parametrize("installer", POSIX_INSTALLERS)
def test_posix_installers_stay_in_sync(installer):
    """setup-linux.sh is a fork of setup-mac.sh and drifts when only one is edited."""
    src = read(installer)
    required = [
        ("--with-mcp opt-in", "--with-mcp"),
        ("MCP deregistration on CLI-first installs", "mcp remove"),
        ("hooks template merge", "settings.hooks.json"),
        ("global CLAUDE.md render", "__BRAIN_CMD__"),
        ("brain skill install", "skills/brain"),
        ("embedder warm-up", "EmbedIndex"),
        ("package install", "pip"),
        ("permission rule", "permissions"),
    ]
    missing = [label for label, needle in required if needle not in src]
    assert not missing, f"{installer} is missing: {', '.join(missing)}"


@pytest.mark.parametrize("installer", ALL_INSTALLERS)
def test_no_installer_installs_the_package_editable(installer):
    """`pip install -e` breaks `import brain_mcp` from a foreign cwd.

    setuptools' generated .pth file does not reliably activate at startup, so an
    editable install works from the project root and fails everywhere else — and
    Claude Code launches hooks from arbitrary directories, so "everywhere else" is
    the normal case. The failure is silent: hooks just stop preloading.
    """
    src = read(installer)
    assert not re.search(r"pip[\"']?\s+install\s+(?:[^\n]*\s)?-e\b", src), (
        f"{installer} installs brain-mcp editable; use a plain non-editable install"
    )
    assert "--editable" not in src, f"{installer} installs brain-mcp editable"


def test_windows_native_calls_are_guarded():
    """Every `claude mcp remove` in the PowerShell installer needs its own catch.

    `setup-windows.ps1` runs under Windows PowerShell 5.1 (the documented invocation
    is `powershell -ExecutionPolicy Bypass -File ...`), where
    `$ErrorActionPreference='Stop'` turns *any* native stderr into a terminating
    NativeCommandError. "No MCP server named brain in user scope" is the expected
    output when there is nothing to remove, so an unguarded call fails the whole
    install on its own idempotent path. Redirecting with `2>$null` does NOT suppress
    it — verified against 5.1 on 2026-08-24 — so try/catch is the only fix.
    """
    lines = read("setup-windows.ps1").splitlines()
    unguarded = []
    for i, line in enumerate(lines):
        if "mcp remove" not in line or "&" not in line:
            continue
        if "catch" not in "\n".join(lines[i:i + 4]):
            unguarded.append(i + 1)
    assert not unguarded, (
        f"setup-windows.ps1 line(s) {unguarded}: `claude mcp remove` is not wrapped in "
        f"a try/catch. Under PS 5.1 the expected 'nothing to remove' stderr becomes a "
        f"terminating error, so setup exits 1 after every step already succeeded."
    )


def test_windows_templates_are_read_and_written_as_utf8():
    """PS 5.1's Get-Content/Set-Content default to the ANSI codepage.

    On 2026-08-24 that silently produced 43 mojibake sequences in the generated global
    CLAUDE.md and 7 in the brain skill — the two load-bearing behavioural files,
    corrupted by the one step whose whole job is to write them. The templates
    themselves were clean, so nothing upstream showed the damage.
    """
    src = read("setup-windows.ps1")
    assert src.count("ReadAllText") >= 2, (
        "both behavioural templates must be read with [System.IO.File]::ReadAllText"
    )
    assert "Encoding]::UTF8" in src, "template reads must name UTF-8 explicitly"
    assert "UTF8Encoding" in src, "template writes must pass an explicit no-BOM UTF8Encoding"
    for hazard in ("Get-Content", "-Raw"):
        assert hazard not in src or "ReadAllText" in src, (
            f"setup-windows.ps1 still uses {hazard} to read a template; that decodes as "
            f"ANSI under PS 5.1"
        )


def test_hook_templates_cover_the_same_events():
    """The POSIX and Windows hook templates must wire identical event sets.

    They are separate files because the platforms need different command forms (an
    env prefix vs a generated launcher), which makes it easy to add an event to one
    and forget the other — the hook then simply never fires on that platform, with
    no error anywhere.
    """
    posix = json.loads(read("templates/settings.hooks.json"))["hooks"]
    win = json.loads(read("templates/settings.hooks.win.json"))["hooks"]
    assert set(posix) == set(win), (
        f"hook event drift: POSIX-only {set(posix) - set(win)}, "
        f"Windows-only {set(win) - set(posix)}"
    )
    for event in posix:
        p_timeout = posix[event][0]["hooks"][0].get("timeout")
        w_timeout = win[event][0]["hooks"][0].get("timeout")
        assert p_timeout == w_timeout, (
            f"{event} timeout differs: POSIX {p_timeout}s vs Windows {w_timeout}s"
        )


def test_every_hook_template_command_maps_to_a_real_hook():
    """A typo'd hook name fails silently — Claude Code logs it and moves on."""
    hooks_dir = REPO_ROOT / "hooks"
    for template in ("templates/settings.hooks.json", "templates/settings.hooks.win.json"):
        for event, entries in json.loads(read(template))["hooks"].items():
            command = entries[0]["hooks"][0]["command"]
            name = command.split()[-1].replace("__BRAIN_HOOKS__/", "")
            name = name[:-3] if name.endswith(".py") else name
            assert (hooks_dir / f"{name}.py").is_file(), (
                f"{template} wires {event} to '{name}', but hooks/{name}.py does not exist"
            )
