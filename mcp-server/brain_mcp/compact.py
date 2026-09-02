"""brain-compact: roll up old session checkpoints into daily/weekly/archive buckets.

Layout invariants (so the session-bundle preload keeps working):

- `Brain/projects/<p>/sessions/*.md` — raw checkpoints, top-level only. The session
  bundle picks the most recent one (`vault.session_start_bundle` uses `.glob("*.md")`,
  which is non-recursive). Anything we move *out of* this top-level directory becomes
  invisible to the preload — exactly what we want.
- `Brain/projects/<p>/sessions/daily/YYYY-MM-DD.md` — concat of all raw checkpoints
  stamped with that day. Created when the day is 7+ days in the past.
- `Brain/projects/<p>/sessions/weekly/YYYY-Www.md` — concat of dailies in that ISO
  week. Created when the day is 30+ days in the past.
- `Brain/archive/projects/<p>/sessions/weekly/YYYY-Www.md` — weeklies whose week ended
  365+ days ago.

Which bucket a file belongs to, and how old it is, come from its **filename**, never
its mtime (F19, 2026-09-01). mtime is when the file was last *written*: every daily a
single compaction run produced shared that run's mtime, so July's dailies were filed
into one weekly named for the September week the run happened in, every stage's
ageing clock restarted at each compaction, and two machines compacting the same daily
on different days filed it under different weeks. The stamp in the name is the only
thing all machines agree on.

Merging is by **source section**, keyed on the leaf checkpoint's filename and marked
with `<!-- brain-compact source: <name> -->` (F20). Legacy rollups marked sections
with a bare `## <name>.md` heading, which is still recognised on read; new writes
carry both, the marker for the key and the heading for the reader. Every transform is
idempotent: re-running adds nothing already present, and a source whose every section
is already in its target — the leftover of a run that died between writing the rollup
and unlinking the sources — is reclaimed (deleted) rather than left as a preload
candidate forever.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from . import vault
from ._console import force_utf8_stdio

DAILY_AGE_MIN = timedelta(days=7)
WEEKLY_AGE_MIN = timedelta(days=30)
ARCHIVE_AGE_MIN = timedelta(days=365)

SOURCE_MARKER_PREFIX = "<!-- brain-compact source: "
_MARKER_RE = re.compile(r"^<!-- brain-compact source: (\S+) -->[ \t]*$")
# The pre-marker form. Anchored on a `.md` name so an ordinary heading inside an
# absorbed checkpoint body (`## Decisions`) is content, not a section boundary — the
# old `^## (.+)$` treated every heading in every absorbed body as a merge key.
_LEGACY_HEADING_RE = re.compile(r"^## (\S+\.md)[ \t]*$")

# Filename shapes. Raw checkpoints are `YYYY-MM-DD-HHMM[SS]-<machine>[_NN].md` (or the
# legacy `YYYY-MM-DD-HHMM.md`); only the date prefix matters here.
_DATE_PREFIX_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?![\d])")
_WEEKLY_NAME_RE = re.compile(r"^(\d{4})-W(\d{2})$")


# --------------------------------------------------------------------------- periods --

def _date_from_name(path: Path) -> date | None:
    m = _DATE_PREFIX_RE.match(path.name)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _week_from_name(path: Path) -> tuple[str, date] | None:
    """(`YYYY-Www`, Monday of that week) for a weekly rollup name."""
    m = _WEEKLY_NAME_RE.match(path.stem)
    if not m:
        return None
    try:
        monday = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    except ValueError:
        return None
    return f"{m.group(1)}-W{int(m.group(2)):02d}", monday


def _mtime_date(path: Path) -> date | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return None


def _day_of(path: Path) -> date | None:
    """Calendar day a raw checkpoint or daily rollup belongs to.

    From the name. A file whose name carries no date at all (hand-named, foreign)
    falls back to its mtime — the alternative is leaving it at the top level as a
    preload candidate forever, which is worse than filing it on the one clock it has.
    """
    return _date_from_name(path) or _mtime_date(path)


def _iso_week_key(day: date) -> str:
    iso = day.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _aged(period_end: date, today: date, min_age: timedelta) -> bool:
    return (today - period_end) >= min_age


# -------------------------------------------------------------------------- sections --

def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:]
    return text


def _sections(text: str) -> list[tuple[str, str]]:
    """Split a rollup into (source name, body) pairs.

    A section starts at a marker line, or — until the first marker is seen — at a
    legacy `## <name>.md` heading. After a marker has been seen, headings are body:
    a rollup written by this version never relies on them, and an absorbed checkpoint
    may legitimately contain one. The heading that immediately follows its own marker
    is dropped, since rendering puts it back.
    """
    out: list[tuple[str, str]] = []
    name: str | None = None
    lines: list[str] = []
    seen_marker = False

    def flush() -> None:
        if name is not None:
            out.append((name, "\n".join(lines).strip("\n")))

    for line in _strip_frontmatter(text).split("\n"):
        m = _MARKER_RE.match(line)
        if m:
            flush()
            name, lines, seen_marker = m.group(1), [], True
            continue
        h = _LEGACY_HEADING_RE.match(line)
        if h:
            if h.group(1) == name and not any(l.strip() for l in lines):
                continue  # the heading rendered under its own marker
            if not seen_marker:
                flush()
                name, lines = h.group(1), []
                continue
        if name is not None:
            lines.append(line)
    flush()
    return out


def _render_section(name: str, body: str) -> str:
    return f"{SOURCE_MARKER_PREFIX}{name} -->\n## {name}\n\n{body.strip()}\n"


def _existing_sources(target: Path) -> set[str]:
    if not target.exists():
        return set()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {name for name, _ in _sections(text)}


def _units(src: Path, split: bool) -> list[tuple[str, str]] | None:
    """What a source contributes: its sections when it is a rollup, else itself whole.

    Returns None when the file cannot be read (it is then neither merged nor
    deleted). A rollup with no recognisable sections but real content is absorbed
    whole under its own name, so nothing is ever deleted that was not first written.
    """
    try:
        text = src.read_text(encoding="utf-8")
    except OSError:
        return None
    if split:
        found = _sections(text)
        if found:
            return found
        if not _strip_frontmatter(text).strip():
            return []
    return [(src.name, text.rstrip())]


def _rollup_frontmatter(target: Path, project: str, kind: str) -> str:
    """Frontmatter for a rollup file.

    A rollup lives inside `projects/`, which is a memory dir, so it needs a
    parseable header like every other note there. Without one it reads as type
    `unknown`, drops out of every type-filtered recall (including
    `--include-sessions`), and `doctor._check_frontmatter` flags it. The first
    real brain-compact run (2026-08-25) wrote 61 headerless rollups and doctor
    reported all 61 as MALFORMED_FRONTMATTER with a remediation hint -- "re-save
    with `brain save`" -- that cannot apply to a machine-written rollup.

    Built with `vault._frontmatter()` rather than an f-string: `project` and the
    period land in YAML scalars, and an interpolated colon is what silently
    destroyed four notes' types in the 2026-07-28 Windows incident.
    """
    period = target.stem
    return vault._frontmatter({
        "name": f"session rollup {period} ({project})",
        "description": f"{kind} rollup of session checkpoints for {project} covering {period}",
        "type": "session",
        "project": project,
        "rollup": kind,
        "period": period,
    })


def _backfill_frontmatter(rollup_dir: Path, project: str, kind: str, dry_run: bool) -> int:
    """Give pre-existing headerless rollups a frontmatter block. Returns count fixed.

    Self-healing so a vault compacted before the header existed converges on the
    next run, instead of carrying a permanent doctor WARN clearable only by
    hand-editing every file.
    """
    if not rollup_dir.exists():
        return 0
    fixed = 0
    for f in sorted(rollup_dir.glob("*.md")):
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if text.startswith("---"):
            continue
        fixed += 1
        if not dry_run:
            vault._atomic_write(f, _rollup_frontmatter(f, project, kind) + text.lstrip("\n"))
    return fixed


@dataclass
class _Merge:
    added: int = 0                                  # sections newly written
    absorbed: list[Path] = field(default_factory=list)  # sources now fully in target
    reclaimed: int = 0                              # absorbed sources that added nothing


def _merge(target: Path, sources: list[Path], dry_run: bool, project: str, kind: str,
           *, split: bool) -> _Merge:
    """Merge `sources` into `target` by section name; report what is safe to delete.

    A source is *absorbed* — and therefore deletable — when every section it holds
    is in the target after this call, whether written now or found already there.
    The second case is the crash leftover: the rollup was written, the process died
    before unlinking, and the rerun used to see `added == 0` and leave the sources
    in place as raw preload candidates forever.
    """
    result = _Merge()
    already = _existing_sources(target)
    parts: list[str] = []
    for src in sorted(sources, key=lambda p: p.name):
        units = _units(src, split)
        if units is None:
            continue
        contributed = 0
        for name, body in units:
            if name in already:
                continue
            parts.append(_render_section(name, body))
            already.add(name)
            contributed += 1
        result.absorbed.append(src)
        result.added += contributed
        if not contributed:
            result.reclaimed += 1
    if not parts or dry_run:
        return result
    target.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(parts)
    if target.exists():
        prior = target.read_text(encoding="utf-8").rstrip("\n")
        text = f"{prior}\n\n{body}"
    else:
        text = _rollup_frontmatter(target, project, kind) + body
    # Atomic, not append-in-place: a rollup is a vault note like any other, and a
    # mid-write failure on the append path truncates a file that is by then the
    # only copy of the checkpoints it absorbed -- the sources are unlinked
    # immediately after.
    vault._atomic_write(target, text)
    return result


def _delete_sources(sources: list[Path], dry_run: bool) -> None:
    if dry_run:
        return
    for src in sources:
        try:
            src.unlink()
        except OSError:
            pass


def _compact_project(project_dir: Path, archive_root: Path, dry_run: bool,
                     today: date | None = None) -> Counter:
    """Compact one project's sessions/ tree. Returns counts for the summary line."""
    sessions = project_dir / "sessions"
    counts: Counter = Counter()
    if not sessions.exists():
        return counts

    today = today or date.today()
    # Validated even though the name came off disk: it is about to be joined into
    # the archive path, and a directory literally named `..` (creatable on some
    # filesystems, or arriving via Obsidian Sync from another OS) would put the
    # rollup outside `archive/projects/`. One helper decides this everywhere.
    project = vault.validate_project_name(project_dir.name)

    # Backfill first, so a vault compacted before rollups carried frontmatter
    # converges on the next run rather than only on the next rollup write.
    counts["frontmatter_backfilled"] += _backfill_frontmatter(
        sessions / "daily", project, "daily", dry_run)
    counts["frontmatter_backfilled"] += _backfill_frontmatter(
        sessions / "weekly", project, "weekly", dry_run)

    # Raw -> daily. A day's period ends at the start of the next day.
    by_day: dict[date, list[Path]] = defaultdict(list)
    for p in sessions.glob("*.md"):
        if not p.is_file():
            continue
        day = _day_of(p)
        if day is not None and _aged(day + timedelta(days=1), today, DAILY_AGE_MIN):
            by_day[day].append(p)
    for day, files in by_day.items():
        # No minimum bucket size. A day with one checkpoint is exactly as aged as a
        # day with five, and the point of a rollup is not deduplication -- it is
        # moving the file out of the non-recursively-globbed top level so it stops
        # being a preload candidate and stops growing that directory.
        target = sessions / "daily" / f"{day.isoformat()}.md"
        m = _merge(target, files, dry_run, project, "daily", split=False)
        counts["raw_to_daily"] += m.added
        counts["reclaimed"] += m.reclaimed
        if m.added:
            counts["daily_files"] += 1
        _delete_sources(m.absorbed, dry_run)

    # Daily -> weekly, keyed on the ISO week of the day in the daily's name.
    daily_dir = sessions / "daily"
    if daily_dir.exists():
        by_week: dict[str, list[Path]] = defaultdict(list)
        for p in daily_dir.glob("*.md"):
            if not p.is_file():
                continue
            day = _day_of(p)
            if day is not None and _aged(day + timedelta(days=1), today, WEEKLY_AGE_MIN):
                by_week[_iso_week_key(day)].append(p)
        for week, files in by_week.items():
            target = sessions / "weekly" / f"{week}.md"
            m = _merge(target, files, dry_run, project, "weekly", split=True)
            counts["daily_to_weekly"] += m.added
            counts["reclaimed"] += m.reclaimed
            if m.added:
                counts["weekly_files"] += 1
            _delete_sources(m.absorbed, dry_run)

    # Weekly -> archive. Merged, never moved-over: a late-syncing checkpoint from an
    # offline machine regenerates a weekly for a week that is already archived, and
    # `shutil.move` onto the archived file replaced a year of that week's history
    # with the one straggler.
    weekly_dir = sessions / "weekly"
    if weekly_dir.exists():
        for w in sorted(weekly_dir.glob("*.md")):
            if not w.is_file():
                continue
            parsed = _week_from_name(w)
            if parsed is None:
                monday = _mtime_date(w)
                if monday is None:
                    continue
                week_name = w.stem
            else:
                week_name, monday = parsed
            if not _aged(monday + timedelta(days=7), today, ARCHIVE_AGE_MIN):
                continue
            dest = vault.project_dir(project, "sessions", "weekly", f"{week_name}.md",
                                     root=archive_root)
            m = _merge(dest, [w], dry_run, project, "weekly", split=True)
            if m.absorbed:
                counts["archived"] += 1
                counts["reclaimed"] += m.reclaimed
                _delete_sources(m.absorbed, dry_run)

    return counts


def main() -> None:
    force_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="Roll up old session checkpoints into daily/weekly/archive buckets."
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing.")
    parser.add_argument("--project", help="compact only this project basename (default: all).")
    args = parser.parse_args()

    try:
        root = vault.vault_root()
    except RuntimeError as e:
        print(f"brain-compact error: {e}", file=sys.stderr)
        sys.exit(1)

    archive_root = root / "archive"
    projects_root = vault.projects_root(root)
    if not projects_root.exists():
        print("no projects directory; nothing to compact.")
        return

    if args.project:
        try:
            targets = [vault.project_dir(args.project, root=root)]
        except ValueError as e:
            print(f"brain-compact error: {e}", file=sys.stderr)
            sys.exit(1)
        if not targets[0].exists():
            print(f"project not found: {args.project}", file=sys.stderr)
            sys.exit(1)
    else:
        targets = sorted(p for p in projects_root.iterdir() if p.is_dir())

    totals: Counter = Counter()
    for proj in targets:
        try:
            totals += _compact_project(proj, archive_root, args.dry_run)
        except ValueError as e:
            # One unusable directory name must not abort the whole run: compaction
            # is the only thing bounding checkpoint growth, and it is already
            # something nobody remembers to run.
            print(f"skipping {proj.name}: {e}", file=sys.stderr)

    prefix = "[dry-run] " if args.dry_run else ""
    line = (
        f"{prefix}compacted {totals['raw_to_daily']} raw -> {totals['daily_files']} daily, "
        f"{totals['daily_to_weekly']} daily -> {totals['weekly_files']} weekly, "
        f"archived {totals['archived']}"
    )
    # Reported, not silent: a run whose only effect was repairing headers or
    # reclaiming already-merged leftovers would otherwise print all zeros and read
    # as a no-op.
    if totals["frontmatter_backfilled"]:
        line += f", backfilled frontmatter on {totals['frontmatter_backfilled']}"
    if totals["reclaimed"]:
        line += f", reclaimed {totals['reclaimed']} already-merged source(s)"
    print(line)


if __name__ == "__main__":
    main()
