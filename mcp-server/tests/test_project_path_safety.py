r"""One helper decides what a project name may be, and nothing escapes Brain/projects/.

`project` values arrive from the CLI, the MCP server (i.e. from a model), the hooks'
payload `cwd`, brain-compact, doctor, and the pi extension. Every one of them used to
be joined straight into a path -- `root / "projects" / project` -- so `../../x`,
`/etc/x`, `C:\x` or `\\server\share` read and wrote outside the vault entirely
(reproduced 2026-08-25 against a throwaway vault).

The style here follows `test_memory_path_predicate.py`: the highest-value assertion is
not "this input is rejected" but "no module can build the path itself", because this
repo's bug pattern is a fix landing at one of N parallel sites.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest

from brain_mcp import cli, compact, doctor, render, vault

from conftest import memory

# Directory names actually in use in the reference vault on 2026-08-25. A rule that
# rejects one of these silently orphans a real project's memories, which is worse than
# the traversal it was meant to stop.
REAL_PROJECT_NAMES = [
    "Ai-Brain", "DbGatekeeper", "JLaser", "JoeTheSlicer", "JoeTheSlicer-v2",
    "LB-RAG", "LightBurn", "MM-CompMatrix2", "MM-CompatibilityMatrix",
    "MM-ToolDecoder", "SkippyTheAverage", "automatic-disco", "cherryd",
    "claw-code", "docs-site", "f42", "g-coder", "hooks", "joespanier",
    "lb-employee-admin", "lb-vendor-compatibility-app", "lb-vendor-public-sites",
    "lb-vendor-svg-services", "machiner-calcs", "mcp-server", "node", "paseo",
    "prcdslnc13", "slb-test-kit", "src", "super-duper-system",
    "test-matrix-manager", "web-ui", "xtool-capture",
]

# Names a directory basename may plausibly carry that must keep working: the rule is a
# blacklist, not a whitelist.
ALSO_VALID = [
    "My Project",            # spaces
    "v1.2.3",                # dots inside
    "under_score",
    "caf\u00e9-br\u00fbl\u00e9",   # non-ASCII letters
    "\u043f\u0440\u043e\u0435\u043a\u0442",  # non-Latin script
    "app(old)",              # parentheses
    "a+b&c!",                # assorted punctuation
    "x" * vault.PROJECT_NAME_MAX_LEN,
]

REJECTED = [
    "",                      # empty
    "   ",                   # whitespace only
    ".",                     # this directory
    "..",                    # the traversal
    "...",                   # dots only
    "trailing.",             # Windows strips the dot
    " lead",                 # the created dir would not carry the name we filtered on
    "trail ",
    "../x",
    "..\\x",
    "../../../../etc",
    "/etc/x",
    "\\etc\\x",
    "C:\\Windows",
    "C:x",                   # drive-qualified relative
    "\\\\server\\share",     # UNC
    "a/b\\c",                # mixed separators
    "nul", "CON", "aux.txt", "com1", "LPT9",   # Windows device names
    "x" * (vault.PROJECT_NAME_MAX_LEN + 1),
    "bad\x00name",
    "bad\nname",
]


# --------------------------------------------------------------- the predicate

@pytest.mark.parametrize("name", REAL_PROJECT_NAMES + ALSO_VALID)
def test_real_and_plausible_names_are_accepted(name: str) -> None:
    assert vault.validate_project_name(name) == name


@pytest.mark.parametrize("name", REJECTED)
def test_traversal_and_unportable_names_are_rejected(name: str) -> None:
    with pytest.raises(vault.ProjectNameError):
        vault.validate_project_name(name)


def test_project_name_error_is_a_value_error() -> None:
    """The CLI, the MCP server and brain-compact all catch ValueError families;
    a bespoke base class would slip past every one of them."""
    assert issubclass(vault.ProjectNameError, ValueError)


@pytest.mark.parametrize("name", REAL_PROJECT_NAMES)
def test_project_dir_builds_under_the_projects_root(vault_dir: Path, name: str) -> None:
    built = vault.project_dir(name, "sessions", root=vault_dir)
    assert built == vault_dir / "projects" / name / "sessions"


def test_project_dir_rejects_a_symlinked_escape(vault_dir: Path, tmp_path: Path) -> None:
    """The containment check is belt-and-braces for exactly this: a name that passes
    every character rule but whose directory points out of the vault."""
    outside = tmp_path / "outside"
    outside.mkdir()
    link = vault_dir / "projects" / "Escapee"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted for this user")
    with pytest.raises(vault.ProjectNameError):
        vault.project_dir("Escapee", root=vault_dir)


# ------------------------------------------------------- nothing escapes on disk

def _tree(root: Path) -> set[Path]:
    return set(root.rglob("*"))


@pytest.mark.parametrize("name", [n for n in REJECTED if n.strip()])
def test_rejected_names_create_nothing_anywhere(vault_dir: Path, name: str) -> None:
    """Assert on the filesystem, not just the exception. A traversal that raises
    *after* mkdir has already happened is still a traversal."""
    sandbox = vault_dir.parent.parent  # tmp_path: holds the vault and nothing else
    before = _tree(sandbox)

    for call in (
        lambda: vault.write_memory("project", "n", "body", project=name),
        lambda: vault.write_checkpoint(name, "body"),
        lambda: vault.ensure_project_overview_stub(name, None),
        lambda: vault.list_memories(mtype="project", project=name),
        lambda: vault.search_memories("body", project=name),
        lambda: vault.session_start_bundle(name),
        lambda: vault.project_dir(name, root=vault_dir),
    ):
        with pytest.raises(ValueError):
            call()

    assert _tree(sandbox) == before, "a rejected project name touched the filesystem"


def test_traversal_cannot_read_a_neighbouring_project(populated_vault: Path) -> None:
    """`Widget/../Secret` used to resolve, so a recall scoped to one project could
    reach another's memories."""
    memory(populated_vault / "projects" / "Secret" / "overview.md",
           "overview", "project", "Launch date is classified.")
    for probe in ("Widget/../Secret", "Widget\\..\\Secret", "../Secret"):
        with pytest.raises(vault.ProjectNameError):
            render.list_payload(mtype="project", project=probe)
        with pytest.raises(vault.ProjectNameError):
            render.recall_payload(query="classified", project=probe)
        with pytest.raises(vault.ProjectNameError):
            vault.session_start_bundle(probe)


def test_preload_of_an_outside_directory_is_refused(vault_dir: Path, tmp_path: Path) -> None:
    """The bundle globs `<project>/feedback/*.md`; an escaping project pointed that
    glob at arbitrary markdown on the user's disk."""
    stash = tmp_path / "stash" / "feedback"
    stash.mkdir(parents=True)
    (stash / "private.md").write_text("---\ntype: feedback\n---\n\nsecret\n", encoding="utf-8")
    with pytest.raises(vault.ProjectNameError):
        vault.session_start_bundle("../../stash")


def test_doctor_degrades_instead_of_raising(populated_vault: Path) -> None:
    """doctor.check runs inside the SessionStart hook. Raising there drops the whole
    preload -- every behavioural rule for the session -- so a bad project must warn."""
    findings = doctor.check("../../../etc")
    codes = [f["code"] for f in findings]
    assert "PROJECT_NAME_INVALID" in codes
    assert doctor.worst_severity(findings) != "error"
    # ...and the project-scoped checks must simply not have run.
    assert "OVERVIEW_MISSING" not in codes


def test_compact_refuses_an_escaping_project(populated_vault: Path,
                                             monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(sys, "argv", ["brain-compact", "--project", "../../etc", "--dry-run"])
    with pytest.raises(SystemExit) as excinfo:
        compact.main()
    assert excinfo.value.code == 1
    assert "brain-compact error" in capsys.readouterr().err


def test_project_basename_sanitizes_rather_than_raising(tmp_path: Path) -> None:
    """It feeds the SessionStart hook. None is a session without project scope;
    an exception is a session without any Brain at all."""
    assert vault.project_basename(None) is None
    assert vault.project_basename("") is None
    good = tmp_path / "My Project"
    good.mkdir()
    assert vault.project_basename(str(good)) == "My Project"
    # A drive/UNC root resolves to an empty basename.
    assert vault.project_basename(Path(tmp_path.anchor).as_posix()) is None


def test_hook_project_basename_delegates(tmp_path: Path) -> None:
    """The hooks kept their own `Path(cwd).name`; it is the value that reaches
    write_checkpoint, so it must obey the same rule."""
    import _common

    good = tmp_path / "Widget"
    good.mkdir()
    assert _common.project_basename({"cwd": str(good)}) == "Widget"
    assert _common.project_basename({}) is None


# ------------------------------------------------------------ frontend surfaces

def test_cli_reports_a_validation_failure_without_a_traceback(
    vault_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["checkpoint", "../../evil", "--summary", "x"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "Traceback" not in err
    assert "ProjectNameError" not in err, "the class name is noise; the rule is the message"


def test_cli_save_reports_cleanly(vault_dir: Path, capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["save", "project", "n", "--content", "b", "--project", "C:\\Windows"])
    assert excinfo.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


def test_mcp_returns_an_error_result_and_keeps_serving(vault_dir: Path) -> None:
    """A raise out of `call_tool` terminates the stdio loop; the whole session's
    memory goes with it. It must come back as a result instead."""
    from brain_mcp import server

    out = asyncio.run(server.call_tool("brain_checkpoint",
                                       {"project": "../evil", "summary": "x"}))
    assert "error" in out[0].text
    # Still serving: a valid call right after must succeed.
    ok = asyncio.run(server.call_tool("brain_checkpoint",
                                      {"project": "Widget", "summary": "x"}))
    assert "checkpoint" in ok[0].text


# ----------------------------------------------------------------- the invariant

# `"projects" / <anything that is not a literal>` is the shape of the bug: it means a
# module joined a project value into a path itself instead of going through
# vault.project_dir(). Literal-only joins (`"projects" / "feedback"`) are fine.
_UNGUARDED_JOIN = re.compile(r'"projects"\s*/\s*(?!")')

_OWNER = "vault.py"  # projects_root() / project_dir() live here and may join freely


def _sources() -> list[Path]:
    package = Path(vault.__file__).resolve().parent
    hooks = package.parents[2] / "hooks"
    return sorted(package.glob("*.py")) + sorted(hooks.glob("*.py"))


def test_no_module_builds_a_project_path_itself() -> None:
    offenders = []
    for source in _sources():
        if source.name == _OWNER:
            continue
        for lineno, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if _UNGUARDED_JOIN.search(line):
                offenders.append(f"{source.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "these join a project value into a path directly instead of calling "
        "vault.project_dir() / vault.projects_root():\n  " + "\n  ".join(offenders)
    )


def test_the_owner_module_still_has_exactly_one_builder() -> None:
    """A second builder inside vault.py is how the three "is this a memory"
    predicates drifted apart; the same failure mode is available here."""
    text = Path(vault.__file__).resolve().read_text(encoding="utf-8")
    joins = [
        line.strip()
        for line in text.splitlines()
        if _UNGUARDED_JOIN.search(line) and not line.strip().startswith("#")
    ]
    assert joins == [], f"vault.py should join through projects_root(): {joins}"


# ------------------------------------------- the containment check must not misfire

# A guard that rejects legitimate input is its own outage. `Path.resolve()` is not
# prefix-stable on Windows: CPython strips the OS's `\?\` extended-length prefix only
# if it can re-resolve the stripped form and get the same answer, so a path being
# created concurrently -- or one over MAX_PATH -- keeps the prefix while the base it is
# compared against does not. Found 2026-08-25 when 40 concurrent write_checkpoint calls
# for ONE ordinary project raised ProjectNameError and dropped the checkpoints, in
# exactly the concurrent-checkpoint case the uniqueness work exists to make safe.

_BS = "\\"


@pytest.mark.parametrize(
    "left,right",
    [
        (_BS * 2 + "?" + _BS + "C:" + _BS + "V" + _BS + "Brain", "C:" + _BS + "V" + _BS + "Brain"),
        (_BS * 2 + "?" + _BS + "UNC" + _BS + "srv" + _BS + "sh", _BS * 2 + "srv" + _BS + "sh"),
        ("C:" + _BS + "Vaults" + _BS + "Ai", "c:" + _BS + "vaults" + _BS + "ai"),
    ],
    ids=["extended-length", "extended-unc", "case-fold"],
)
def test_comparable_normalizes_windows_path_spellings(left: str, right: str) -> None:
    assert vault._comparable(Path(left)) == vault._comparable(Path(right))


def test_extended_prefix_constants_are_spelled_correctly() -> None:
    """One backslash out and the strip below silently no-ops, restoring the bug."""
    assert vault._EXTENDED_PREFIX == _BS + _BS + "?" + _BS
    assert vault._EXTENDED_UNC_PREFIX == _BS + _BS + "?" + _BS + "UNC" + _BS


def test_project_dir_survives_an_extended_length_resolve(
    vault_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r"""Force the asymmetry the race produces: the target resolves to the `\\?\` form
    while the base resolves plainly. Before the fix this raised ProjectNameError."""
    real_resolve = Path.resolve
    target = vault_dir / "projects" / "Ai-Brain" / "sessions"

    def flaky(self: Path, strict: bool = False) -> Path:
        resolved = real_resolve(self)
        if self == target:
            return Path(_BS * 2 + "?" + _BS + str(resolved))
        return resolved

    monkeypatch.setattr(Path, "resolve", flaky)
    assert vault.project_dir("Ai-Brain", "sessions", root=vault_dir) == target


def test_concurrent_checkpoints_never_hit_the_traversal_guard(vault_dir: Path) -> None:
    """The regression in full: many writers, one project, no ProjectNameError, and
    every body still readable under its own name."""
    import concurrent.futures as cf

    with cf.ThreadPoolExecutor(16) as pool:
        paths = list(pool.map(lambda i: vault.write_checkpoint("Ai-Brain", f"BODY-{i}"), range(32)))

    assert len(set(paths)) == 32
    bodies = {p.read_text(encoding="utf-8").split("BODY-")[1].split()[0] for p in paths}
    assert bodies == {str(i) for i in range(32)}
    names = [p.name for p in paths]
    assert all(n.endswith(".md") for n in names)

    # Group by the undisambiguated stem. A 32-writer burst can straddle a SECOND
    # boundary, so more than one group is normal and must not read as a failure --
    # asserting a single group here made this test intermittently red.
    groups: dict[str, list[str]] = {}
    for name in names:
        stem = name[: -len(".md")]
        base, sep, tail = stem.rpartition("_")
        if sep and tail.isdigit():
            groups.setdefault(base, []).append(tail)
        else:
            groups.setdefault(stem, [])

    for base, tails in groups.items():
        # Chronological order must survive the disambiguator: the plain name sorts
        # ahead of its own _NN siblings. `_` (0x5F) is deliberate -- `-` (0x2D) sorts
        # before `.` and would put the second checkpoint of a second ahead of the first.
        assert all(base + ".md" < base + "_" + t + ".md" for t in tails)
        assert sorted(tails) == sorted(tails, key=int), "suffixes must be zero-padded"
        assert all(len(t) == 2 and t.isdigit() for t in tails), tails
    assert sum(len(t) + 1 for t in groups.values()) == 32
