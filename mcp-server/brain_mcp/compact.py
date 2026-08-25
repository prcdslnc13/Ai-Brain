"""brain-compact: roll up old session checkpoints into daily/weekly/archive buckets.

Layout invariants (so the session-bundle preload keeps working):

- `Brain/projects/<p>/sessions/*.md` — raw checkpoints, top-level only. The session
  bundle picks the most recent one (`vault.session_start_bundle` uses `.glob("*.md")`,
  which is non-recursive). Anything we move *out of* this top-level directory becomes
  invisible to the preload — exactly what we want.
- `Brain/projects/<p>/sessions/daily/YYYY-MM-DD.md` — concat of all raw checkpoints
  written on that day. Created when raw files are 7-30 days old.
- `Brain/projects/<p>/sessions/weekly/YYYY-Www.md` — concat of dailies in that ISO
  week. Created when dailies are 30-365 days old.
- `Brain/archive/projects/<p>/sessions/weekly/YYYY-Www.md` — weeklies older than 365 days.

All transforms are idempotent: re-running merges with existing rollups by source
filename rather than overwriting, and re-running on an already-compacted vault is a
no-op.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from . import vault
from ._console import force_utf8_stdio

DAILY_AGE_MIN = timedelta(days=7)
WEEKLY_AGE_MIN = timedelta(days=30)
ARCHIVE_AGE_MIN = timedelta(days=365)

_SOURCE_HEADER_RE = re.compile(r"^## (.+)$", re.MULTILINE)


def _bucket_by_day(files: list[Path]) -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = defaultdict(list)
    for f in files:
        try:
            d = datetime.fromtimestamp(f.stat().st_mtime).date().isoformat()
        except OSError:
            continue
        out[d].append(f)
    return out


def _bucket_by_iso_week(files: list[Path]) -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = defaultdict(list)
    for f in files:
        try:
            dt = datetime.fromtimestamp(f.stat().st_mtime)
        except OSError:
            continue
        iso = dt.isocalendar()
        out[f"{iso.year}-W{iso.week:02d}"].append(f)
    return out


def _existing_sources(target: Path) -> set[str]:
    if not target.exists():
        return set()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return set()
    return set(_SOURCE_HEADER_RE.findall(text))


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


def _concat(target: Path, sources: list[Path], dry_run: bool, project: str, kind: str) -> int:
    """Append `sources` into `target`, deduped by source filename. Returns count added."""
    already = _existing_sources(target)
    parts: list[str] = []
    added = 0
    for src in sorted(sources, key=lambda p: p.stat().st_mtime):
        if src.name in already:
            continue
        try:
            body = src.read_text(encoding="utf-8")
        except OSError:
            continue
        parts.append(f"## {src.name}\n\n{body.rstrip()}\n")
        added += 1
    if not parts:
        return 0
    if dry_run:
        return added
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
    return added


def _delete_sources(sources: list[Path], dry_run: bool) -> None:
    if dry_run:
        return
    for src in sources:
        try:
            src.unlink()
        except OSError:
            pass


def _compact_project(project_dir: Path, archive_root: Path, dry_run: bool) -> Counter:
    """Compact one project's sessions/ tree. Returns counts for the summary line."""
    sessions = project_dir / "sessions"
    counts: Counter = Counter()
    if not sessions.exists():
        return counts

    now = datetime.now().timestamp()
    project = project_dir.name

    # Backfill first, so a vault compacted before rollups carried frontmatter
    # converges on the next run rather than only on the next rollup write.
    counts["frontmatter_backfilled"] += _backfill_frontmatter(
        sessions / "daily", project, "daily", dry_run)
    counts["frontmatter_backfilled"] += _backfill_frontmatter(
        sessions / "weekly", project, "weekly", dry_run)

    raw = [p for p in sessions.glob("*.md") if p.is_file()]
    aging_raw = [p for p in raw if (now - p.stat().st_mtime) >= DAILY_AGE_MIN.total_seconds()]
    by_day = _bucket_by_day(aging_raw)
    for day, files in by_day.items():
        if len(files) < 2:
            continue
        target = sessions / "daily" / f"{day}.md"
        added = _concat(target, files, dry_run, project, "daily")
        if added:
            counts["raw_to_daily"] += added
            counts["daily_files"] += 1
            _delete_sources(files, dry_run)

    daily_dir = sessions / "daily"
    if daily_dir.exists():
        dailies = [p for p in daily_dir.glob("*.md") if p.is_file()]
        aging_dailies = [p for p in dailies
                         if (now - p.stat().st_mtime) >= WEEKLY_AGE_MIN.total_seconds()]
        by_week = _bucket_by_iso_week(aging_dailies)
        for week, files in by_week.items():
            target = sessions / "weekly" / f"{week}.md"
            added = _concat(target, files, dry_run, project, "weekly")
            if added:
                counts["daily_to_weekly"] += added
                counts["weekly_files"] += 1
                _delete_sources(files, dry_run)

    weekly_dir = sessions / "weekly"
    if weekly_dir.exists():
        weeklies = [p for p in weekly_dir.glob("*.md") if p.is_file()]
        aging_weeklies = [p for p in weeklies
                          if (now - p.stat().st_mtime) >= ARCHIVE_AGE_MIN.total_seconds()]
        for w in aging_weeklies:
            dest = archive_root / "projects" / project_dir.name / "sessions" / "weekly" / w.name
            counts["archived"] += 1
            if dry_run:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(w), str(dest))
            except OSError:
                pass

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
    projects_root = root / "projects"
    if not projects_root.exists():
        print("no projects directory; nothing to compact.")
        return

    if args.project:
        targets = [projects_root / args.project]
        if not targets[0].exists():
            print(f"project not found: {args.project}", file=sys.stderr)
            sys.exit(1)
    else:
        targets = sorted(p for p in projects_root.iterdir() if p.is_dir())

    totals: Counter = Counter()
    for proj in targets:
        totals += _compact_project(proj, archive_root, args.dry_run)

    prefix = "[dry-run] " if args.dry_run else ""
    line = (
        f"{prefix}compacted {totals['raw_to_daily']} raw -> {totals['daily_files']} daily, "
        f"{totals['daily_to_weekly']} daily -> {totals['weekly_files']} weekly, "
        f"archived {totals['archived']}"
    )
    # Reported, not silent: a run whose only effect was repairing headers would
    # otherwise print all zeros and read as a no-op.
    if totals["frontmatter_backfilled"]:
        line += f", backfilled frontmatter on {totals['frontmatter_backfilled']}"
    print(line)


if __name__ == "__main__":
    main()
