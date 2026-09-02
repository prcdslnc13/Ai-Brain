"""brain-compact: rollups are vault notes, buckets come from filenames, merges are safe.

Three generations of bug live in this file's history:

1. `compact._concat` wrote `sessions/daily/YYYY-MM-DD.md` with no frontmatter of its
   own. The first real run (2026-08-25) produced 61 rollups and doctor reported all
   61 as MALFORMED_FRONTMATTER with a hint ("re-save with `brain save`") that cannot
   apply to a machine-written file. A standing 61-file WARN trains you to ignore the
   one signal that catches genuine save corruption.

2. Buckets and ageing were computed from **mtime** (F19). mtime is when compaction
   last wrote the file, so every daily one run produced landed in one weekly named
   for the run's week, each stage's clock restarted on every run, and two machines
   filed the same daily under different weeks. The period now comes from the name.

3. Merging keyed on `## <anything>` headings, so every heading inside an absorbed
   body polluted the key set; archiving `shutil.move`d over an existing archived
   weekly; and a crash between writing a rollup and unlinking its sources left
   sources the rerun saw as "already merged" and never deleted (F20).

Dates in these tests are relative to today, because the clock under test is the
calendar date in the filename: a literal January stamp would age past *every*
threshold the moment the year rolled on.
"""

from __future__ import annotations

import ast
import os
import time
from datetime import date, timedelta
from pathlib import Path

import yaml

from brain_mcp import compact, doctor, vault

DAY = 86400.0
TODAY = date.today()


def _day(days_ago: int) -> str:
    return (TODAY - timedelta(days=days_ago)).isoformat()


def _stamp(days_ago: int, hhmm: str = "0900") -> str:
    return f"{_day(days_ago)}-{hhmm}"


def _week(days_ago: int) -> str:
    return compact._iso_week_key(TODAY - timedelta(days=days_ago))


def _aged_checkpoint(sessions: Path, stamp: str, project: str,
                     age_days: float | None = None) -> Path:
    """A raw checkpoint shaped like the real renderer's. `age_days` backdates the
    mtime; by default the mtime is left fresh, since the name is the clock."""
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
    if age_days is not None:
        when = time.time() - age_days * DAY
        os.utime(p, (when, when))
    return p


def _project_with_aging_checkpoints(brain: Path, project: str = "demo") -> Path:
    """Two same-day checkpoints, ten days old: past the daily threshold, short of weekly."""
    proj = brain / "projects" / project
    sessions = proj / "sessions"
    _aged_checkpoint(sessions, _stamp(10, "0900"), project)
    _aged_checkpoint(sessions, _stamp(10, "1700"), project)
    return proj


def _compact(brain: Path, proj: Path, dry_run: bool = False, today: date | None = None):
    return compact._compact_project(proj, brain / "archive", dry_run, today=today)


def _rollups(proj: Path) -> list[Path]:
    return sorted((proj / "sessions" / "daily").glob("*.md"))


def _weeklies(proj: Path) -> list[Path]:
    return sorted((proj / "sessions" / "weekly").glob("*.md"))


def _archived(brain: Path, project: str = "demo") -> list[Path]:
    d = brain / "archive" / "projects" / project / "sessions" / "weekly"
    return sorted(d.glob("*.md")) if d.exists() else []


def _frontmatter_of(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), f"{path.name} has no frontmatter block"
    end = text.find("\n---", 3)
    assert end != -1, f"{path.name} has an unterminated frontmatter block"
    return yaml.safe_load(text[3:end])


def _rollup_with(target: Path, project: str, kind: str, sections: dict[str, str]) -> Path:
    """A marker-era rollup holding the given {source name: body} sections."""
    target.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(compact._render_section(n, b) for n, b in sections.items())
    target.write_text(compact._rollup_frontmatter(target, project, kind) + body,
                      encoding="utf-8")
    return target


# --- frontmatter ---------------------------------------------------------------------

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
    assert f"## {_stamp(10, '0900')}.md" in body
    assert f"## {_stamp(10, '1700')}.md" in body
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
    legacy = proj / "sessions" / "daily" / f"{_day(10)}.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(f"## {_stamp(10)}.md\n\nold rollup body\n", encoding="utf-8")

    counts = _compact(vault_dir, proj)

    assert counts["frontmatter_backfilled"] == 1
    fm = _frontmatter_of(legacy)
    assert fm["type"] == "session" and fm["project"] == "demo"
    assert "old rollup body" in legacy.read_text(encoding="utf-8"), "backfill is additive"


def test_backfill_is_a_dry_run_no_op(vault_dir: Path) -> None:
    proj = vault_dir / "projects" / "demo"
    legacy = proj / "sessions" / "daily" / f"{_day(10)}.md"
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
    _aged_checkpoint(proj / "sessions", _stamp(10, "2100"), "demo")
    _aged_checkpoint(proj / "sessions", _stamp(10, "2300"), "demo")

    _compact(vault_dir, proj)

    rollups = _rollups(proj)
    assert len(rollups) == 1
    text = rollups[0].read_text(encoding="utf-8")
    assert text.count("type: session") == 5, (
        "one rollup header plus four absorbed checkpoint headers"
    )
    assert _frontmatter_of(rollups[0])["type"] == "session"
    assert f"## {_stamp(10, '2300')}.md" in text
    assert f"## {_stamp(10, '0900')}.md" in text, "the original sources survive the append"


def test_a_lone_checkpoint_for_a_day_still_rolls_up(vault_dir: Path) -> None:
    """A day with one checkpoint compacts like any other.

    `_compact_project` used to skip any day bucket with fewer than two files, so a
    singleton day stayed in `sessions/*.md` -- and therefore preload-visible --
    forever, however old it got. Rolling up is not deduplication; it is getting the
    file out of the non-recursively-globbed top level.
    """
    proj = vault_dir / "projects" / "demo"
    lone = _aged_checkpoint(proj / "sessions", _stamp(10), "demo")

    counts = _compact(vault_dir, proj)

    assert counts["raw_to_daily"] == 1
    assert not lone.exists(), "the source must be consumed, not duplicated"
    rollups = _rollups(proj)
    assert len(rollups) == 1
    assert _frontmatter_of(rollups[0])["type"] == "session"
    assert f"## {_stamp(10)}.md" in rollups[0].read_text(encoding="utf-8")


def test_nothing_aged_is_left_at_the_top_level(vault_dir: Path) -> None:
    """The property the guard removal buys: no old raw checkpoint survives a run.

    Mixed singleton and multi-checkpoint days, all past DAILY_AGE_MIN -- afterwards
    `sessions/*.md` must be empty, because everything it held was eligible.
    """
    proj = vault_dir / "projects" / "demo"
    sessions = proj / "sessions"
    _aged_checkpoint(sessions, _stamp(10, "0900"), "demo")
    _aged_checkpoint(sessions, _stamp(10, "1700"), "demo")
    _aged_checkpoint(sessions, _stamp(20, "1000"), "demo")
    _aged_checkpoint(sessions, _stamp(25, "1000"), "demo")

    _compact(vault_dir, proj)

    assert list(sessions.glob("*.md")) == []
    assert len(_rollups(proj)) == 3, "one rollup per distinct day"


def test_recent_checkpoints_are_never_touched(vault_dir: Path) -> None:
    """The other half: DAILY_AGE_MIN still protects the preload.

    Removing the bucket-size guard must not widen what counts as aged -- the newest
    checkpoint is what SessionStart loads.
    """
    proj = vault_dir / "projects" / "demo"
    sessions = proj / "sessions"
    fresh = _aged_checkpoint(sessions, _stamp(1), "demo")

    counts = _compact(vault_dir, proj)

    assert counts["raw_to_daily"] == 0
    assert fresh.exists()
    assert not (proj / "sessions" / "daily").exists()


def test_a_late_checkpoint_merges_into_an_existing_rollup(vault_dir: Path) -> None:
    """A lone straggler for an already-rolled-up day must not be stranded.

    Under the old guard this single file could never join its own day's rollup,
    because a bucket of one was skipped outright.
    """
    proj = _project_with_aging_checkpoints(vault_dir)
    _compact(vault_dir, proj)
    straggler = _aged_checkpoint(proj / "sessions", _stamp(10, "2300"), "demo")

    counts = _compact(vault_dir, proj)

    assert counts["raw_to_daily"] == 1
    assert not straggler.exists()
    assert len(_rollups(proj)) == 1
    text = _rollups(proj)[0].read_text(encoding="utf-8")
    assert f"## {_stamp(10, '2300')}.md" in text
    assert text.count("type: session") == 4, "still exactly one rollup header"


# --- F19: the period is in the name, never the mtime ---------------------------------

def test_raw_checkpoint_day_comes_from_its_name(vault_dir: Path) -> None:
    """A checkpoint stamped ten days ago rolls up under that day even when its
    mtime is *now* -- which it is on every machine the file synced to later."""
    proj = vault_dir / "projects" / "demo"
    raw = _aged_checkpoint(proj / "sessions", _stamp(10), "demo")  # fresh mtime
    assert time.time() - raw.stat().st_mtime < DAY

    counts = _compact(vault_dir, proj)

    assert counts["raw_to_daily"] == 1
    assert [p.name for p in _rollups(proj)] == [f"{_day(10)}.md"]


def test_daily_is_filed_under_the_week_in_its_name(vault_dir: Path) -> None:
    """The F19 headline: July's dailies must not land in a September weekly.

    A daily named for a day 60 days ago, with a fresh mtime, must be filed under
    that day's ISO week -- not under this week's, which is what an mtime bucket
    produced for every daily a single run wrote.
    """
    proj = vault_dir / "projects" / "demo"
    daily = _rollup_with(proj / "sessions" / "daily" / f"{_day(60)}.md", "demo", "daily",
                         {f"{_stamp(60)}.md": "sixty days ago"})
    assert time.time() - daily.stat().st_mtime < DAY

    counts = _compact(vault_dir, proj)

    assert counts["daily_to_weekly"] == 1
    assert not daily.exists()
    assert [p.name for p in _weeklies(proj)] == [f"{_week(60)}.md"]
    assert _week(60) != _week(0), "the fixture must not straddle the current week"


def test_ageing_clock_is_the_name_not_the_mtime(vault_dir: Path) -> None:
    """The inverse: a file *named* for yesterday is recent, however old its mtime.

    Under the mtime clock each stage's timer restarted whenever compaction wrote
    the file; a name-based clock cannot be reset by writing.
    """
    proj = vault_dir / "projects" / "demo"
    raw = _aged_checkpoint(proj / "sessions", _stamp(1), "demo", age_days=40)
    daily = _rollup_with(proj / "sessions" / "daily" / f"{_day(2)}.md", "demo", "daily",
                         {f"{_stamp(2)}.md": "two days ago"})
    old = time.time() - 400 * DAY
    os.utime(daily, (old, old))

    counts = _compact(vault_dir, proj)

    assert counts["raw_to_daily"] == 0 and counts["daily_to_weekly"] == 0
    assert raw.exists() and daily.exists()


def test_weekly_is_archived_by_the_week_in_its_name(vault_dir: Path) -> None:
    proj = vault_dir / "projects" / "demo"
    week = _week(400)
    weekly = _rollup_with(proj / "sessions" / "weekly" / f"{week}.md", "demo", "weekly",
                          {f"{_stamp(400)}.md": "over a year ago"})
    assert time.time() - weekly.stat().st_mtime < DAY

    counts = _compact(vault_dir, proj)

    assert counts["archived"] == 1
    assert not weekly.exists()
    assert [p.name for p in _archived(vault_dir)] == [f"{week}.md"]
    assert "over a year ago" in _archived(vault_dir)[0].read_text(encoding="utf-8")


def test_two_machines_file_the_same_daily_under_one_week(vault_dir: Path) -> None:
    """Compacting on different days (different machines, different mtimes) must
    agree on the weekly, or the same day's history is split across two files
    that Obsidian Sync then merges into a conflict."""
    sections = {f"{_stamp(45)}.md": "shared history"}
    a = vault_dir / "projects" / "machine-a"
    b = vault_dir / "projects" / "machine-b"
    _rollup_with(a / "sessions" / "daily" / f"{_day(45)}.md", "machine-a", "daily", sections)
    _rollup_with(b / "sessions" / "daily" / f"{_day(45)}.md", "machine-b", "daily", sections)

    _compact(vault_dir, a, today=TODAY)
    _compact(vault_dir, b, today=TODAY + timedelta(days=9))

    assert [p.name for p in _weeklies(a)] == [p.name for p in _weeklies(b)] == [f"{_week(45)}.md"]


def test_a_file_with_no_date_in_its_name_falls_back_to_mtime(vault_dir: Path) -> None:
    """The one place mtime is still consulted: a name that carries no date at all.
    Leaving it at the top level forever would be worse."""
    proj = vault_dir / "projects" / "demo"
    sessions = proj / "sessions"
    sessions.mkdir(parents=True)
    odd = sessions / "hand-named-notes.md"
    odd.write_text("some notes\n", encoding="utf-8")
    old = time.time() - 10 * DAY
    os.utime(odd, (old, old))

    counts = _compact(vault_dir, proj)

    assert counts["raw_to_daily"] == 1 and not odd.exists()
    assert [p.name for p in _rollups(proj)] == [f"{_day(10)}.md"]


# --- F20: merge keys, crash leftovers, and archive collisions ------------------------

def test_new_rollups_mark_their_sources(vault_dir: Path) -> None:
    proj = _project_with_aging_checkpoints(vault_dir)
    _compact(vault_dir, proj)

    text = _rollups(proj)[0].read_text(encoding="utf-8")
    for hhmm in ("0900", "1700"):
        assert f"{compact.SOURCE_MARKER_PREFIX}{_stamp(10, hhmm)}.md -->" in text


def test_headings_inside_absorbed_bodies_are_not_merge_keys(vault_dir: Path) -> None:
    """`## What the user asked for` is a checkpoint heading, not a source. The old
    `^## (.+)$` key put every such heading into the merge set."""
    proj = _project_with_aging_checkpoints(vault_dir)
    _compact(vault_dir, proj)

    keys = compact._existing_sources(_rollups(proj)[0])
    assert keys == {f"{_stamp(10, '0900')}.md", f"{_stamp(10, '1700')}.md"}


def test_legacy_heading_rollups_are_still_recognised(vault_dir: Path) -> None:
    """A rollup written before markers existed keys on its `## <name>.md` headings,
    and a marker-era append to the same file parses alongside them."""
    proj = vault_dir / "projects" / "demo"
    legacy = proj / "sessions" / "daily" / f"{_day(10)}.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        "## a.md\n\nbody a\n\n## Decisions\n\nnot a source\n\n"
        f"{compact._render_section('b.md', 'body b\n\n## Files touched\n\n- x.py')}",
        encoding="utf-8",
    )
    assert compact._existing_sources(legacy) == {"a.md", "b.md"}
    assert dict(compact._sections(legacy.read_text(encoding="utf-8")))["b.md"].endswith("- x.py")


def test_crash_leftover_sources_are_reclaimed(vault_dir: Path) -> None:
    """A run that died between writing the rollup and unlinking its sources.

    The rerun found every section already present, so `added == 0`, and the
    delete step -- gated on `added` -- never ran: the sources stayed as raw
    preload candidates forever, and re-ran the same no-op on every compaction.
    """
    proj = vault_dir / "projects" / "demo"
    raw = _aged_checkpoint(proj / "sessions", _stamp(10), "demo")
    daily = _rollup_with(proj / "sessions" / "daily" / f"{_day(10)}.md", "demo", "daily",
                         {raw.name: raw.read_text(encoding="utf-8")})
    before = daily.read_text(encoding="utf-8")

    counts = _compact(vault_dir, proj)

    assert counts["raw_to_daily"] == 0
    assert counts["reclaimed"] == 1
    assert not raw.exists(), "an already-merged source is reclaimed, not stranded"
    assert daily.read_text(encoding="utf-8") == before, "and nothing is duplicated"


def test_reclaim_never_deletes_what_was_not_written(vault_dir: Path) -> None:
    """The guard on the guard: a source is reclaimed only when every section it
    holds is in the target. A raw checkpoint whose name is absent is added first."""
    proj = vault_dir / "projects" / "demo"
    raw = _aged_checkpoint(proj / "sessions", _stamp(10), "demo")
    daily = _rollup_with(proj / "sessions" / "daily" / f"{_day(10)}.md", "demo", "daily",
                         {"unrelated.md": "something else"})

    counts = _compact(vault_dir, proj, dry_run=True)
    assert counts["raw_to_daily"] == 1 and counts["reclaimed"] == 0
    assert raw.exists(), "dry-run deletes nothing"

    counts = _compact(vault_dir, proj)
    assert counts["raw_to_daily"] == 1 and not raw.exists()
    assert "work on demo" in daily.read_text(encoding="utf-8")


def test_archiving_merges_into_an_existing_archived_weekly(vault_dir: Path) -> None:
    """A late-syncing checkpoint from an offline machine regenerates a weekly for a
    week that is already archived. `shutil.move` replaced the archived year of
    history with the one straggler."""
    proj = vault_dir / "projects" / "demo"
    week = _week(400)
    archived = _rollup_with(
        vault_dir / "archive" / "projects" / "demo" / "sessions" / "weekly" / f"{week}.md",
        "demo", "weekly", {"old-a.md": "history a", "old-b.md": "history b"})
    late = _rollup_with(proj / "sessions" / "weekly" / f"{week}.md", "demo", "weekly",
                        {"old-b.md": "history b", "late-c.md": "straggler c"})

    counts = _compact(vault_dir, proj)

    assert counts["archived"] == 1
    assert not late.exists()
    text = archived.read_text(encoding="utf-8")
    assert "history a" in text, "the archived history survives"
    assert text.count("history b") == 1, "shared sections are not duplicated"
    assert "straggler c" in text
    assert compact._existing_sources(archived) == {"old-a.md", "old-b.md", "late-c.md"}


def test_regenerated_daily_adds_only_its_new_sections_to_the_weekly(vault_dir: Path) -> None:
    """Section-level keys are what make reclaiming safe.

    A daily rolled into its weekly and deleted; a late raw checkpoint recreates a
    daily of the *same name* with the old section plus a new one. Keyed on the
    daily's filename it would read as "already merged" -- and reclaiming it would
    delete the only copy of the new section.
    """
    proj = vault_dir / "projects" / "demo"
    week = _week(45)
    weekly = _rollup_with(proj / "sessions" / "weekly" / f"{week}.md", "demo", "weekly",
                          {f"{_stamp(45, '0900')}.md": "morning"})
    daily = _rollup_with(proj / "sessions" / "daily" / f"{_day(45)}.md", "demo", "daily",
                         {f"{_stamp(45, '0900')}.md": "morning",
                          f"{_stamp(45, '2300')}.md": "late night"})

    counts = _compact(vault_dir, proj)

    assert counts["daily_to_weekly"] == 1
    assert not daily.exists()
    text = weekly.read_text(encoding="utf-8")
    assert text.count("morning") == 1 and "late night" in text


def test_dry_run_reports_without_writing_or_deleting(vault_dir: Path) -> None:
    proj = _project_with_aging_checkpoints(vault_dir)
    raws = sorted((proj / "sessions").glob("*.md"))

    counts = _compact(vault_dir, proj, dry_run=True)

    assert counts["raw_to_daily"] == 2
    assert all(p.exists() for p in raws)
    assert not (proj / "sessions" / "daily").exists()


# --- the invariant, not the instance -------------------------------------------------

def test_compact_never_buckets_or_ages_by_mtime() -> None:
    """The class of bug behind F19: every period and age decision must come from a
    filename. `st_mtime` may appear only in the single documented fallback."""
    tree = ast.parse(Path(compact.__file__).read_text(encoding="utf-8"))
    users = set()
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute) and node.attr == "st_mtime":
                users.add(fn.name)
    assert users == {"_mtime_date"}, (
        f"{sorted(users)} read st_mtime; only the no-date-in-name fallback may"
    )


def test_compact_never_moves_over_an_existing_file() -> None:
    """The class of bug behind F20's archive half: nothing in compact may replace a
    destination wholesale. Merges go through `_merge`, which reads first."""
    tree = ast.parse(Path(compact.__file__).read_text(encoding="utf-8"))
    movers = {"move", "replace", "rename", "copy", "copyfile", "copy2"}
    offenders = [
        f"{fn.name}:{node.func.attr}"
        for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in movers
    ]
    assert not offenders, f"{offenders} can replace a destination wholesale"


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
