"""Parity checks across the install path.

There were four independent installers (`brain-setup.py`, `setup-mac.sh`,
`setup-linux.sh`, `setup-windows.ps1`) plus four uninstallers, all hand-maintained,
so a fix reliably landed in whichever one the session was looking at while the others
silently kept the bug. Two live instances found 2026-08-24: the `permissions.allow`
rule reached `setup-mac.sh` (a596572) and `setup-windows.ps1` but never
`brain-setup.py` — the primary installer — or `setup-linux.sh`; and PR #16's
PowerShell stderr guard landed on one of two identical `claude mcp remove` call sites.

ROADMAP 3G retired the six shell/PowerShell scripts (deprecated then deleted,
2026-08-25), so "parity" now spans two Python entry points that already share
`brain_settings_merge.py`. Most of what this file used to assert was keeping the
duplicates honest and went away with them. What remains is the part that was never
about duplication: properties a single installer can still get wrong, each of which
has been gotten wrong before.

These are text assertions, not behavioural ones — running an installer for real needs
a machine per platform. Text is enough to catch drift, which is the actual failure
mode.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

ALL_INSTALLERS = ["brain-setup.py"]
ALL_UNINSTALLERS = ["brain-uninstall.py"]
SHARED_MERGE = "brain_settings_merge.py"

# ROADMAP 3G deleted these on 2026-08-25. Named here so the test below can prove they
# are gone *and* unreferenced: a deleted script that a doc still recommends is a worse
# failure than the duplication, because the user follows the doc and gets nothing.
DELETED_SCRIPTS = [
    "setup-mac.sh", "setup-linux.sh", "setup-windows.ps1",
    "uninstall-mac.sh", "uninstall-linux.sh", "uninstall-windows.ps1",
]


def read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("installer", ALL_INSTALLERS)
def test_installer_exists(installer):
    assert (REPO_ROOT / installer).is_file(), f"{installer} is documented but missing"


@pytest.mark.parametrize("script", ALL_INSTALLERS + ALL_UNINSTALLERS)
def test_every_installer_routes_through_the_shared_merge_module(script):
    """All eight settings.json writers must share one implementation.

    Before 2026-08-25 the merge existed five times: once in `brain-setup.py` and once
    as an embedded Python heredoc in each shell/PowerShell script. Both live bugs it
    hid were "the fix landed at one of N sites" — hooks assigned over an event
    (deleting the user's own hooks) and malformed JSON replaced with `{}`. Routing
    every site through `brain_settings_merge.py` is what makes those unfixable in
    only one place. Ownership detection has to be shared across the install/uninstall
    boundary too: a narrower predicate in the uninstaller strands orphan hooks, a
    wider one deletes a third-party hook.
    """
    src = read(script)
    assert SHARED_MERGE in src or "brain_settings_merge" in src, (
        f"{script} does not use {SHARED_MERGE} — its settings.json handling has "
        f"forked from the other seven"
    )


@pytest.mark.parametrize("script", ALL_INSTALLERS + ALL_UNINSTALLERS)
def test_no_installer_reimplements_the_merge(script):
    """The fragments of the old inline implementations must not come back."""
    src = read(script)
    forbidden = {
        'settings["hooks"][event] = definition':
            "assigning over an event deletes third-party hooks for it",
        "settings = {}":
            "swallowing a parse error into an empty dict is how a malformed "
            "settings.json got replaced wholesale; the shared module refuses instead",
        "hooks_block":
            "template rendering belongs to brain_settings_merge.render_hooks_template",
    }
    hits = [f"{needle!r} ({why})" for needle, why in forbidden.items() if needle in src]
    assert not hits, f"{script} reimplements the shared merge: " + "; ".join(hits)


def test_the_shared_module_writes_and_prunes_the_permission_rule():
    """Every installer must pre-approve the brain CLI in settings.json.

    Without a `permissions.allow` entry the model's proactive saves hit a permission
    prompt outside /brain skill turns, which defeats the whole automatic-memory
    design — and does it invisibly, since an unanswered prompt looks exactly like the
    model choosing not to save. The prune half is what keeps a re-run from
    accumulating duplicates, and it must recognise every rule shape ever written: an
    env-prefixed `.../bin/brain` on POSIX, a `brain.cmd` wrapper path on Windows, and
    the current per-subcommand rules carrying the agent-surface gate.
    """
    src = read(SHARED_MERGE)
    assert 'f"Bash({brain_cmd} {sub}:*)"' in src, "the shared module no longer writes the rules"
    assert "AGENT_SUBCOMMANDS" in src, "the narrow per-subcommand rule list is gone"
    assert "def prune_permission_rules" in src, "nothing removes the rules on uninstall"
    assert re.search(r"is_brain_permission_rule", src)
    assert "/bin/brain" in src and "brain.cmd" in src and "brain_agent_surface=" in src, (
        "the prune predicate must match every rule shape we have ever written, or a "
        "superseded rule becomes a standing approval that survives each re-install"
    )


@pytest.mark.parametrize("installer", ALL_INSTALLERS)
def test_every_installer_passes_the_brain_command(installer):
    """Routing through the module is only useful if the rule's value is supplied."""
    src = read(installer)
    assert "--brain-cmd" in src or "brain_cmd=" in src, (
        f"{installer} calls the shared merge without a brain command, so no "
        f"permission rule is written and proactive saves will prompt"
    )


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


def test_the_installer_reads_and_writes_templates_as_utf8():
    """Every file read/write in the installer names an encoding.

    On 2026-08-24 `setup-windows.ps1` produced 43 mojibake sequences in the generated
    global CLAUDE.md and 7 in the brain skill — the two load-bearing behavioural
    files, corrupted by the one step whose whole job is to write them. The templates
    themselves were clean, so nothing upstream showed the damage.

    That script is gone (ROADMAP 3G) but the bug class is not: it belongs to whoever
    renders the templates, which is now `brain-setup.py` alone. Python is not immune —
    `read_text()` with no encoding uses the locale default, which is cp1252 on a
    stock Windows, reintroducing exactly this.

    Parsed rather than grepped: a regex for the call stops at the first `)`, so
    `write_text(t.replace(a, b), encoding="utf-8")` reads as unqualified.
    """
    tree = ast.parse(read("brain-setup.py"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("read_text", "write_text", "open"):
            continue
        if not any(kw.arg == "encoding" for kw in node.keywords):
            offenders.append(f"line {node.lineno}: .{node.func.attr}()")
    assert not offenders, (
        "brain-setup.py has file I/O with no explicit encoding: " + "; ".join(offenders)
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


# ----------------------------------------------------- the installer runs its own tests

# `pytest` is declared as the `dev` optional-dependency, and until 2026-08-25 no
# installer installed it. So a fresh venv -- which is what every install produces --
# could not run the suite at all without a hand-typed `pip install pytest`. The cost
# was not hypothetical: test_comparable_normalizes_windows_path_spellings[case-fold]
# asserted Windows-only case folding unconditionally and had NEVER passed on macOS or
# Linux, and it survived a whole code-review-remediation cycle because the suite was
# nobody's default. `brain-setup.py` now installs the extra and runs the suite.
#
# These assert on `brain-setup.py` alone, deliberately: it is the installer that
# actually gets run. The shell/PowerShell three are a known, accepted gap here rather
# than an oversight -- so if you port this, extend the parametrize rather than adding
# a second copy of the check.

def test_the_primary_installer_installs_the_dev_extra():
    """Without the extra there is no pytest, and the self-test below is decorative."""
    src = read("brain-setup.py")
    assert re.search(r"MCP_SERVER_DIR\}\[dev\]|\[dev\]", src), (
        "brain-setup.py must install the 'dev' extra, or the venv it builds cannot "
        "run the test suite"
    )


def test_the_primary_installer_runs_the_suite():
    src = read("brain-setup.py")
    assert '"-m", "pytest"' in src, "brain-setup.py must run the test suite during setup"
    assert "--skip-tests" in src, "the self-test needs a documented escape hatch"


def test_a_failing_suite_does_not_abort_the_install_but_does_exit_nonzero():
    """Both halves matter, and they pull in opposite directions.

    Aborting would mean a red test costs the user their memory system, so the wiring
    must still land. But exiting 0 would recreate the exact silence this step exists
    to end -- a scripted install could report success over a broken checkout.
    """
    src = read("brain-setup.py")
    assert "sys.exit(4)" in src, (
        "a failing self-test must make brain-setup.py exit nonzero"
    )
    # The run_tests() call site must not be wrapped in anything that stops the install.
    call = re.search(r"tests_ok, tests_reason = run_tests\(.*\)", src)
    assert call, "run_tests must be called and its result kept"
    assert "die(" not in call.group(0), "a failing self-test must not abort the install"


def test_the_self_test_does_not_inherit_the_users_real_vault():
    """conftest builds a throwaway vault; an inherited BRAIN_VAULT could point the
    suite at the user's real memories, which setup has no business writing to."""
    src = read("brain-setup.py")
    run_tests = src[src.index("def run_tests("):src.index("def ensure_brain_layout(")]
    assert 'env.pop("BRAIN_VAULT", None)' in run_tests, (
        "run_tests must drop an inherited BRAIN_VAULT before running the suite"
    )


# ------------------------------------------------------------------- retirement (3G)


@pytest.mark.parametrize("script", DELETED_SCRIPTS)
def test_the_retired_scripts_are_gone(script):
    """Deleted on 2026-08-25, and they must not come back by copy-paste.

    Re-adding one is how the N-way duplication returns: the next Windows or Linux
    bring-up is exactly the moment someone reaches for a platform script again.
    """
    assert not (REPO_ROOT / script).exists(), (
        f"{script} is back. Install behaviour belongs in brain-setup.py — see ROADMAP 3G "
        f"for why four installers cost more than they bought."
    )


def _runnable_mentions(doc: str, script: str) -> list[str]:
    """Lines that tell a reader to *run* `script`, ignoring lines that discuss it.

    The distinction is the point. "ROADMAP 3G retired setup-mac.sh; do not add a
    platform script back" is exactly what CLAUDE.md should say to the next session —
    banning the substring would delete the institutional memory along with the file.
    What must not survive is an invocation: a path, a `-File` argument, or a line
    inside a fenced command block.
    """
    hits, in_fence = [], False
    for line in read(doc).splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if script not in line:
            continue
        if in_fence or f"/{script}" in line or f"{_BSLASH}{script}" in line or "-File " in line:
            hits.append(line.strip())
    return hits


_BSLASH = chr(92)

USER_DOCS = ["README.md", "WINDOWS-SETUP.md", "LMSTUDIO-SETUP.md", "PI-SETUP.md", "CLAUDE.md"]


@pytest.mark.parametrize("script", DELETED_SCRIPTS)
def test_no_doc_still_tells_a_user_to_run_a_retired_script(script):
    """A doc recommending a deleted script is worse than the duplication was.

    The duplication produced installs that were subtly wrong; a stale doc produces no
    install at all, from a user following the instructions correctly.
    """
    offenders = {d: _runnable_mentions(d, script) for d in USER_DOCS}
    offenders = {d: lines for d, lines in offenders.items() if lines}
    assert not offenders, (
        f"these docs still show how to run the retired {script}: "
        + "; ".join(f"{d}: {lines[0]!r}" for d, lines in offenders.items())
    )


def test_the_docs_name_the_installer_that_does_exist():
    """Every install guide points at the one supported entry point."""
    for doc in ("README.md", "WINDOWS-SETUP.md", "LMSTUDIO-SETUP.md", "PI-SETUP.md"):
        assert "brain-setup.py" in read(doc), f"{doc} never names the supported installer"


def test_the_roadmap_records_the_retirement():
    """The commit message and code comments point here; a dangling pointer is worse
    than none, and 3G is where the reasoning (and the 3.9.6 bootstrap check) lives."""
    roadmap = read("ROADMAP.md")
    assert "Retire the platform-specific installers" in roadmap
