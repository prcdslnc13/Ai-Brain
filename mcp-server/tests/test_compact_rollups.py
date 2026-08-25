"""A rollup written by brain-compact is a vault note, and every enumeration must agree.

`compact._concat` used to write `sessions/daily/YYYY-MM-DD.md` as a bare concatenation
of its source checkpoints, with no frontmatter of its own. Nothing noticed until the
first real run: on 2026-08-25 it produced 61 rollups and `doctor._check_frontmatter`
reported all 61 as MALFORMED_FRONTMATTER, with a remediation hint ("re-save each with
`brain save`") that cannot apply to a machine-written rollup. A standing 61-file WARN
is worse than no check at all -- it trains you to ignore the one signal that catches
genuine save corruption.

Both halves were wrong, so both are asserted here:
  - compact must stamp a parseable, correctly-typed header on every rollup, and repair
    the ones it wrote before it did;
  - doctor must decide "is this a memory" through the shared predicates rather than a
    private copy of the exclusion list -- the same drift that
    `test_memory_path_predicate` exists to prevent.
"""

from __future__ import annotations

import ast
import os
import time
from pathlib import Path

import yaml

from brain_mcp import compact, doctor, vault

DAY = 86400.0


def _aged_checkpoint(sessions: Path, stamp: str, project: str, age_days: float) -> Path:
    """A raw checkpoint whose mtime is `age_days` old, shaped like the real renderer's."""
    sessions.mkdir(parents=True, exist_ok=True)
    p = sessions / f"{stamp}.md"
    p.write_text(
        vault._frontmatter({
            "name": f"session checkpoint {stamp}",
            "description": f"automated session checkpoint for {project}",
            "type": "session",
            "project": project,
            "timestamp": stamp,
        }) + f"## What the user asked for\n\nwork on {project}\n",
        encoding="utf-8",
    )
    when = time.time() - age_days * DAY
    os.utime(p, (when, when))
    return p


def _project_with_aging_checkpoints(brain: Path, project: str = "demo") -> Path:
    """Two same-day checkpoints old enough to roll up (compact needs >= 2 per day)."""
    proj = brain / "projects" / project
    sessions = proj / "sessions"
    _aged_checkpoint(sessions, "2026-01-05-0900", project, age_days=40)
    _aged_checkpoint(sessions, "2026-01-05-1700", project, age_days=40)
    return proj


def _compact(brain: Path, proj: Path, dry_run: bool = False):
    return compact._compact_project(proj, brain / "archive", dry_run)


def _rollups(proj: Path) -> list[Path]:
    return sorted((proj / "sessions" / "daily").glob("*.md"))


def _frontmatter_of(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), f"{path.name} has no frontmatter block"
    end = text.find("\n---", 3)
    assert end != -1, f"{path.name} has an unterminated frontmatter block"
    return yaml.safe_load(text[3:end])


def test_rollup_carries_parseable_frontmatter(vault_dir: Path) -> None:
    proj = _project_with_aging_checkpoints(vault_dir)
    _compact(vault_dir, proj)

    rollups = _rollups(proj)
    assert len(rollups) == 1, "two same-day checkpoints must collapse into one rollup"
    fm = _frontmatter_of(rollups[0])
    assert fm["type"] in doctor.KNOWN_TYPES
    assert fm["type"] == "session", "a rollup is still session history"
    assert fm["project"] == "demo", "type/project-filtered recall needs both"


def test_rollup_body_still_contains_every_source(vault_dir: Path) -> None:
    """The header must be additive.

    Absorbing a checkpoint and losing it is the one unrecoverable failure here,
    since the sources are unlinked immediately after the rollup is written.
    """
    proj = _project_with_aging_checkpoints(vault_dir)
    _compact(vault_dir, proj)

    body = _rollups(proj)[0].read_text(encoding="utf-8")
    assert "## 2026-01-05-0900.md" in body
    assert "## 2026-01-05-1700.md" in body
    assert body.count("work on demo") == 2


def test_doctor_does_not_flag_a_compacted_vault(vault_dir: Path) -> None:
    """The end-to-end assertion: compact, then doctor, and stay quiet."""
    proj = _project_with_aging_checkpoints(vault_dir)
    _compact(vault_dir, proj)

    codes = [f.code for f in doctor._check_frontmatter(vault_dir)]
    assert "MALFORMED_FRONTMATTER" not in codes, (
        "compaction must not manufacture a warning; this is the 61-file regression"
    )


def test_doctor_ignores_a_headerless_session_file(vault_dir: Path) -> None:
    """Belt and braces: even an unrepaired rollup must not be reported.

    A vault compacted by an older brain-compact, or any future writer under
    sessions/, must not light up a check whose hint tells you to `brain save` it.
    """
    stray = vault_dir / "projects" / "demo" / "sessions" / "daily" / "2026-01-05.md"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("## a.md\n\nno frontmatter at all\n", encoding="utf-8")

    codes = [f.code for f in doctor._check_frontmatter(vault_dir)]
    assert "MALFORMED_FRONTMATTER" not in codes


def test_doctor_still_catches_a_genuinely_broken_memory(vault_dir: Path) -> None:
    """Skipping sessions must not blunt the check it exists for.

    The 2026-07-28 incident: a colon in a save title produced invalid YAML and four
    notes silently lost their type.
    """
    bad = vault_dir / "feedback" / "colon-in-title.md"
    bad.write_text(
        "---\nname: F1 Ultra job path: .xf is a tar\ntype: feedback\n---\n\nBody.\n",
        encoding="utf-8",
    )
    codes = [f.code for f in doctor._check_frontmatter(vault_dir)]
    assert "MALFORMED_FRONTMATTER" in codes


def test_legacy_headerless_rollup_is_backfilled(vault_dir: Path) -> None:
    """A vault compacted before headers existed must converge on the next run."""
    proj = vault_dir / "projects" / "demo"
    legacy = proj / "sessions" / "daily" / "2026-01-05.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("## 2026-01-05-0900.md\n\nold rollup body\n", encoding="utf-8")

    counts = _compact(vault_dir, proj)

    assert counts["frontmatter_backfilled"] == 1
    fm = _frontmatter_of(legacy)
    assert fm["type"] == "session" and fm["project"] == "demo"
    assert "old rollup body" in legacy.read_text(encoding="utf-8"), "backfill is additive"


def test_backfill_is_a_dry_run_no_op(vault_dir: Path) -> None:
    proj = vault_dir / "projects" / "demo"
    legacy = proj / "sessions" / "daily" / "2026-01-05.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("## a.md\n\nbody\n", encoding="utf-8")

    counts = _compact(vault_dir, proj, dry_run=True)

    assert counts["frontmatter_backfilled"] == 1, "dry-run still reports what it would fix"
    assert not legacy.read_text(encoding="utf-8").startswith("---")


def test_compaction_stays_idempotent(vault_dir: Path) -> None:
    """Re-running must add no sources and no second frontmatter block."""
    proj = _project_with_aging_checkpoints(vault_dir)
    _compact(vault_dir, proj)
    first = _rollups(proj)[0].read_text(encoding="utf-8")

    counts = _compact(vault_dir, proj)

    assert counts["raw_to_daily"] == 0
    assert counts["frontmatter_backfilled"] == 0
    assert _rollups(proj)[0].read_text(encoding="utf-8") == first
    assert first.index("---") == 0


def test_appending_to_an_existing_rollup_keeps_one_header(vault_dir: Path) -> None:
    """Later checkpoints from the same day merge into the existing rollup.

    The append path is the one that must NOT re-stamp a header: two frontmatter
    blocks at the top of a file is invalid YAML, which would land the rollup right
    back in the check this whole change is about.
    """
    proj = _project_with_aging_checkpoints(vault_dir)
    _compact(vault_dir, proj)
    _aged_checkpoint(proj / "sessions", "2026-01-05-2100", "demo", age_days=40)
    _aged_checkpoint(proj / "sessions", "2026-01-05-2300", "demo", age_days=40)

    _compact(vault_dir, proj)

    rollups = _rollups(proj)
    assert len(rollups) == 1
    text = rollups[0].read_text(encoding="utf-8")
    assert text.count("type: session") == 5, (
        "one rollup header plus four absorbed checkpoint headers"
    )
    assert _frontmatter_of(rollups[0])["type"] == "session"
    assert "## 2026-01-05-2300.md" in text
    assert "## 2026-01-05-0900.md" in text, "the original sources survive the append"


def test_a_lone_checkpoint_for_a_day_is_left_raw(vault_dir: Path) -> None:
    """Pinning existing behaviour, not endorsing it.

    `_compact_project` skips any day bucket with fewer than two files, so a day
    that only ever had one checkpoint keeps that file in `sessions/*.md` -- and
    therefore preload-visible -- indefinitely, however old it gets. Anything that
    changes the `len(files) < 2` guard should have to change this test on purpose.
    """
    proj = vault_dir / "projects" / "demo"
    lone = _aged_checkpoint(proj / "sessions", "2026-01-05-0900", "demo", age_days=400)

    counts = _compact(vault_dir, proj)

    assert counts["raw_to_daily"] == 0
    assert lone.exists()
    assert not (proj / "sessions" / "daily").exists()


# --- the invariant, not the instance -------------------------------------------------

def _functions_with_markdown_enumeration(source: Path) -> list[ast.FunctionDef]:
    """Every function in `source` that walks a directory for *.md files."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    out = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) not in ("glob", "rglob"):
                continue
            globs_markdown = any(
                isinstance(a, ast.Constant)
                and isinstance(a.value, str)
                and a.value.endswith(".md")
                for a in node.args
            )
            if globs_markdown:
                out.append(fn)
                break
    return out


def test_recursive_memory_enumerations_route_through_the_predicate() -> None:
    """A *recursive* walk of a memory dir must ask vault what counts as a memory.

    Non-recursive `glob` is exempt: it cannot descend into sessions/, archive/ or
    .index/, so it has nothing to filter. `rglob` can, and every check that reaches
    for one has to answer the same question -- which is precisely the question that
    had three different answers before `is_memory_path` unified them.
    """
    offenders = []
    for fn in _functions_with_markdown_enumeration(Path(doctor.__file__)):
        body = ast.dump(fn)
        recursive = "'rglob'" in body
        routed = "is_memory_path" in body or "is_session_path" in body
        if recursive and not routed:
            offenders.append(fn.name)
    assert not offenders, (
        f"{offenders} rglob '*.md' without vault.is_memory_path/is_session_path; "
        f"that is how doctor came to flag 61 machine-written rollups"
    )


def test_no_module_redeclares_the_excluded_directories() -> None:
    """The sibling of test_no_module_redeclares_the_exclusion_list, for EXCLUDE_DIRS.

    doctor carried the literal tuple ("archive", "_setup", ".index") -- a fourth
    private copy of the same policy, and the reason its frontmatter check drifted.
    """
    package = Path(vault.__file__).resolve().parent
    offenders = []
    for source in sorted(package.glob("*.py")):
        if source.name == "vault.py":
            continue
        text = source.read_text(encoding="utf-8")
        names_dirs_literally = '"_setup"' in text or "'_setup'" in text
        routed = "EXCLUDE_DIRS" in text or "is_memory_path" in text
        if names_dirs_literally and not routed:
            offenders.append(source.name)
    assert not offenders, (
        f"{offenders} name excluded directories literally instead of using "
        f"vault.EXCLUDE_DIRS / vault.is_memory_path"
    )
