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
import shlex
from pathlib import Path

import pytest

from conftest import load_repo_script

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
    env-prefixed `.../bin/brain` on POSIX, a `brain.cmd` wrapper path on Windows, the
    `BRAIN_AGENT_SURFACE=1`-prefixed per-subcommand rules, and the current
    `brain-agent.py` launcher rules.
    """
    src = read(SHARED_MERGE)
    assert 'f"Bash({brain_cmd} {sub}:*)"' in src, "the shared module no longer writes the rules"
    assert "AGENT_SUBCOMMANDS" in src, "the narrow per-subcommand rule list is gone"
    assert "def prune_permission_rules" in src, "nothing removes the rules on uninstall"
    assert re.search(r"is_brain_permission_rule", src)
    assert (
        "/bin/brain" in src and "brain.cmd" in src
        and "brain_agent_surface=" in src and "brain-agent.py" in src
    ), (
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


# File I/O calls the installer makes, and what each may say about its encoding.
# The three writer helpers default to UTF-8 when the kwarg is absent; read_text /
# write_text / open default to the locale, which is the bug.
IO_CALLS = {"read_text", "write_text", "open", "write_managed_text", "_write_managed",
            "atomic_write_text"}
DEFAULT_UTF8_CALLS = {"write_managed_text", "_write_managed", "atomic_write_text"}
# The one function allowed -- required -- to write something other than UTF-8: the
# Windows batch launcher, which cmd.exe reads in the OEM codepage.
BATCH_WRITERS = {"write_windows_launch_cmd"}


def _io_calls_by_function(src: str):
    """(enclosing FunctionDef, call node, call name) for every IO call."""
    tree = ast.parse(src)
    owner: dict[int, tuple[ast.FunctionDef, ast.Call, str]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                name = node.func.attr
            elif isinstance(node.func, ast.Name):
                name = node.func.id
            else:
                continue
            if name in IO_CALLS:
                # ast.walk is breadth-first, so an inner def overwrites its outer.
                owner[id(node)] = (fn, node, name)
    return list(owner.values())


def _forwards_own_utf8_parameter(fn: ast.FunctionDef, value: ast.expr) -> bool:
    """`encoding=encoding` inside `def f(..., encoding="utf-8")` is a pass-through
    with a UTF-8 default, not a locale-default write."""
    if not isinstance(value, ast.Name):
        return False
    args = fn.args
    params = args.args + args.kwonlyargs
    defaults = [None] * (len(args.args) - len(args.defaults)) + list(args.defaults) + list(args.kw_defaults)
    for param, default in zip(params, defaults):
        if param.arg == value.id:
            return isinstance(default, ast.Constant) and str(default.value).lower().startswith("utf-8")
    return False


def test_the_installer_names_the_right_encoding_for_every_file_it_touches():
    """Every file read/write in the installer names UTF-8 explicitly -- except the
    Windows batch launcher, which must NOT be UTF-8.

    On 2026-08-24 `setup-windows.ps1` produced 43 mojibake sequences in the generated
    global CLAUDE.md and 7 in the brain skill — the two load-bearing behavioural
    files, corrupted by the one step whose whole job is to write them. That script is
    gone (ROADMAP 3G) but the bug class is not: `read_text()` with no encoding uses
    the locale default, cp1252 on a stock Windows.

    The batch file is the mirror image (found 2026-09-01): cmd.exe reads `.cmd` files
    in the OEM codepage, so a `brain-launch.cmd` written as UTF-8 with a non-ASCII
    path set BRAIN_VAULT to a directory that does not exist and every hook failed
    silently. The previous version of this test accepted any `encoding=` kwarg at
    all, which the UTF-8 batch write satisfied.

    Parsed rather than grepped: a regex for the call stops at the first `)`, so
    `write_text(t.replace(a, b), encoding="utf-8")` reads as unqualified.
    """
    offenders = []
    oem_writes = 0
    for fn, node, call_name in _io_calls_by_function(read("brain-setup.py")):
        fn_name = fn.name
        where = f"line {node.lineno}: {call_name}() in {fn_name}"
        kw = next((k for k in node.keywords if k.arg == "encoding"), None)
        if kw is None:
            if call_name not in DEFAULT_UTF8_CALLS:
                offenders.append(f"{where} has no explicit encoding")
            elif fn_name in BATCH_WRITERS:
                offenders.append(f"{where} would write the batch file as UTF-8 (default)")
            continue
        literal = kw.value.value if isinstance(kw.value, ast.Constant) else None
        if fn_name in BATCH_WRITERS:
            if isinstance(literal, str):
                offenders.append(f"{where} hard-codes {literal!r}; cmd.exe reads the OEM codepage")
            else:
                oem_writes += 1
        elif isinstance(literal, str):
            if not literal.lower().startswith("utf-8"):
                offenders.append(f"{where} uses {literal!r}; only the batch launcher may not be UTF-8")
        elif not _forwards_own_utf8_parameter(fn, kw.value):
            offenders.append(f"{where} must pass the literal encoding='utf-8', not {ast.unparse(kw.value)}")
    assert not offenders, "brain-setup.py encoding problems: " + "; ".join(offenders)
    assert oem_writes >= 1, "the batch launcher is no longer written in the OEM codepage"


def test_the_batch_launcher_has_no_non_ascii_of_its_own():
    """Whatever the user's paths are, the fixed text of brain-launch.cmd must encode
    in every OEM codepage -- the old header carried an em dash, which cp437 cannot
    represent, so the write would have failed on every install once the encoding
    was fixed. Rendered with ASCII paths so only the template's own text is tested."""
    setup = load_repo_script("brain-setup.py")
    fn_src = read("brain-setup.py")
    body = fn_src[fn_src.index("def write_windows_launch_cmd("):fn_src.index("def merge_settings_json(")]
    assert body.isascii(), "write_windows_launch_cmd contains non-ASCII text of its own"
    assert setup.MANAGED_MARKER.isascii()


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
    """A typo'd hook name fails silently — Claude Code logs it and moves on.

    Split with shlex, the way a shell would: the placeholders are quoted in the
    templates, and `str.split()` would hand back the closing quote as part of the name.
    """
    hooks_dir = REPO_ROOT / "hooks"
    for template in ("templates/settings.hooks.json", "templates/settings.hooks.win.json"):
        for event, entries in json.loads(read(template))["hooks"].items():
            for group in entries:
                for hook in group["hooks"]:
                    name = hook_script_name(hook["command"])
                    assert (hooks_dir / f"{name}.py").is_file(), (
                        f"{template} wires {event} to '{name}', but hooks/{name}.py "
                        f"does not exist"
                    )


def hook_script_name(command: str) -> str:
    """The hook module a template command invokes, ignoring trailing arguments.

    POSIX: the token ending in `.py`. Windows: the token after `__BRAIN_LAUNCH__`
    (the launcher takes the hook name as its first argument). The preload entries
    carry `--part I --parts N` after the name, so "last token" is no longer it.
    """
    tokens = shlex.split(command)  # placeholders are quoted in the templates
    for i, tok in enumerate(tokens):
        if tok.endswith(".py"):
            return tok.replace("__BRAIN_HOOKS__/", "")[:-3]
        if tok == "__BRAIN_LAUNCH__" and i + 1 < len(tokens):
            return tokens[i + 1]
    raise AssertionError(f"no hook name in template command: {command!r}")


# Paths with a space in every component the templates interpolate. A space in a
# username is ordinary on Windows ("C:\Users\Jo Bloggs") and a bare placeholder split
# the command there: every hook failed, and failed silently.
SPACED = {
    "brain_python": "/Users/jo bloggs/src/Ai Brain/mcp-server/.venv/bin/python",
    "brain_hooks": "/Users/jo bloggs/src/Ai Brain/hooks",
    "brain_vault": "/Users/jo bloggs/Vaults/Ai Brain",
    "brain_launch": r"C:\Users\Jo Bloggs\.claude\brain-launch.cmd",
}


@pytest.mark.parametrize(
    "template, kwargs, expected_words",
    [
        ("templates/settings.hooks.json",
         {k: SPACED[k] for k in ("brain_python", "brain_hooks", "brain_vault")}, 3),
        ("templates/settings.hooks.win.json", {"brain_launch": SPACED["brain_launch"]}, 2),
    ],
    ids=["posix", "windows"],
)
def test_hook_templates_quote_every_placeholder(template, kwargs, expected_words):
    """Rendered with spaced paths, each command must shell-split into exactly the
    words the platform expects -- `VAR=value python hook.py` on POSIX, `launcher
    hook-name` on Windows -- with every path intact as ONE word. And the quoted
    rendering must still be recognised as ours, or a re-install could not prune it."""
    sm = load_repo_script("brain_settings_merge.py")
    block = sm.render_hooks_template(read(template), **kwargs)
    for event, groups in block.items():
        for group in groups:
            for hook in group["hooks"]:
                command = hook["command"]
                words = shlex.split(command)
                # The preload entries carry `--part I --parts N` after the fixed
                # words; those are bare flags and digits, never a path.
                assert len(words) >= expected_words, f"{template} {event}: {command!r} -> {words}"
                trailing = words[expected_words:]
                assert all(w.startswith("--") or w.isdigit() for w in trailing), (
                    f"{template} {event}: unexpected trailing words {trailing} in {command!r}"
                )
                if "brain_launch" in kwargs:
                    assert words[0] == kwargs["brain_launch"].replace("\\", "/")
                else:
                    assert words[0] == f"BRAIN_VAULT={kwargs['brain_vault']}"
                    assert words[1] == kwargs["brain_python"]
                    assert words[2].startswith(kwargs["brain_hooks"] + "/")
                assert sm.is_brain_command(
                    command, kwargs.get("brain_hooks", ""), kwargs.get("brain_launch", "")
                ), f"quoted rendering not recognised as Brain-owned: {command!r}"


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
