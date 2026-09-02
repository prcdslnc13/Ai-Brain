"""Ai-Brain health checks.

Surfaces silent-failure modes (unset BRAIN_VAULT, missing subdirs, Obsidian Sync
conflicts, corrupt vector index, editable install, stale checkpoints) into a
format consumable by:

  - the `brain_doctor` MCP tool (JSON findings list),
  - the SessionStart hook banner (warn/error findings prepended to the bundle),
  - the `brain-doctor` CLI (human-readable stdout).

No external network or model calls. Safe to run on every session start.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from . import vault
from ._console import force_utf8_stdio

SEVERITY_ORDER = ("ok", "info", "warn", "error")


@dataclass
class Finding:
    severity: str
    code: str
    message: str
    hint: str = ""

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
        }


def _check_brain_vault() -> list[Finding]:
    raw = os.environ.get("BRAIN_VAULT")
    if not raw:
        return [Finding(
            "error", "BRAIN_VAULT_UNSET",
            "BRAIN_VAULT environment variable is not set.",
            "Re-run setup-mac.sh or setup-windows.ps1 with the vault path, "
            "or export BRAIN_VAULT before launching Claude Code.",
        )]
    path = Path(raw).expanduser()
    if not path.exists():
        return [Finding(
            "error", "BRAIN_VAULT_MISSING",
            f"BRAIN_VAULT points to {path} which does not exist.",
            "Check that Obsidian Sync has mounted the vault on this machine.",
        )]
    brain = path / "Brain"
    if not brain.exists():
        return [Finding(
            "error", "BRAIN_DIR_MISSING",
            f"{brain} does not exist.",
            "Create the Brain/ directory inside the vault, or wait for "
            "Obsidian Sync to finish its initial sync.",
        )]
    return [Finding("ok", "BRAIN_VAULT_OK", f"vault at {path}")]


REQUIRED_SUBDIRS = ("user", "feedback", "projects", "references")


def _check_subdirs(brain: Path) -> list[Finding]:
    missing = [d for d in REQUIRED_SUBDIRS if not (brain / d).exists()]
    if missing:
        return [Finding(
            "warn", "SUBDIR_MISSING",
            f"Brain subdirs not present: {', '.join(missing)}.",
            "These are auto-created on first brain_save of that type. If you "
            "expect existing data, Obsidian Sync may not have finished.",
        )]
    return [Finding("ok", "SUBDIRS_OK", "all required Brain subdirs present")]


SYNC_CONFLICT_GLOBS = (
    "*(conflict*).md",
    "*.sync-conflict-*.md",
    "*conflicted copy*.md",
)


def _check_sync_conflicts(brain: Path) -> list[Finding]:
    hits: set[Path] = set()
    for pat in SYNC_CONFLICT_GLOBS:
        for p in brain.rglob(pat):
            if ".index" in p.parts or "archive" in p.parts:
                continue
            hits.add(p)
    if not hits:
        return [Finding("ok", "SYNC_CONFLICTS_OK", "no sync conflict files detected")]
    ordered = sorted(hits)
    sample = ", ".join(str(p.relative_to(brain)) for p in ordered[:3])
    more = f" (+{len(ordered) - 3} more)" if len(ordered) > 3 else ""
    return [Finding(
        "error", "SYNC_CONFLICTS",
        f"{len(ordered)} Obsidian Sync conflict file(s) in vault: {sample}{more}",
        "Open the vault in Obsidian, reconcile each conflict by hand, then "
        "delete the losing copy. Until resolved, recall may return stale data.",
    )]


MEMORY_DIRS = ("user", "feedback", "references", "projects")
KNOWN_TYPES = {"user", "feedback", "project", "reference", "session"}


def _check_frontmatter(brain: Path) -> list[Finding]:
    """Flag memory files whose frontmatter fails to parse or lacks a valid type.

    Such files stay full-text searchable but silently drop out of every
    type-filtered recall — the worst kind of failure, because saves appear to
    work. Found 2026-07-28 (Windows): a colon in a save title produced
    `name: F1 Ultra job path: .xf is a tar`, invalid YAML, and four notes
    lost their type without any error surfacing anywhere.
    """
    bad: list[tuple[Path, str]] = []
    for d in MEMORY_DIRS:
        droot = brain / d
        if not droot.exists():
            continue
        for p in droot.rglob("*.md"):
            # Route through the shared predicates rather than re-listing the
            # exclusions here — a third copy of EXCLUDE_DIRS is exactly how the
            # three enumerations drifted apart before is_memory_path() unified
            # them.
            if not vault.is_memory_path(p, brain):
                continue
            # Session checkpoints and their daily/weekly rollups are written by
            # the transcript renderer and brain-compact, not by a user-supplied
            # save title, so they cannot exhibit the failure this check is for --
            # and the hint below ("re-save with `brain save`") is meaningless for
            # a machine-generated rollup. On 2026-08-25 the first real
            # brain-compact run wrote 61 rollups and this check flagged every one
            # of them, which is how a standing 61-file WARN teaches you to ignore
            # the check that exists to catch genuine save corruption.
            # `_check_near_duplicates` already skips sessions the same way.
            if vault.is_session_path(p):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue
            except ValueError:
                # A cp1252 note raises UnicodeDecodeError — a ValueError, which the
                # OSError catch let straight through the whole doctor and cost the
                # session its preload (F4). It is a finding, not a crash.
                bad.append((p, "not valid UTF-8"))
                continue
            if not text.startswith("---"):
                bad.append((p, "no frontmatter"))
                continue
            end = text.find("\n---", 3)
            if end == -1:
                bad.append((p, "unterminated frontmatter"))
                continue
            try:
                fm = yaml.safe_load(text[3:end])
            except yaml.YAMLError:
                bad.append((p, "frontmatter is not valid YAML"))
                continue
            if not isinstance(fm, dict):
                bad.append((p, "frontmatter is not a mapping"))
                continue
            t = fm.get("type")
            # `type: [x]` parses to a list, and `list in set` raises TypeError.
            if not isinstance(t, str) or t not in KNOWN_TYPES:
                bad.append((p, f"type is {t!r}"))
    if not bad:
        return [Finding("ok", "FRONTMATTER_OK", "all memory frontmatter parses with a valid type")]
    sample = "; ".join(
        f"{p.relative_to(brain)} ({reason})" for p, reason in bad[:3]
    )
    more = f" (+{len(bad) - 3} more)" if len(bad) > 3 else ""
    return [Finding(
        "warn", "MALFORMED_FRONTMATTER",
        f"{len(bad)} memory file(s) with broken/missing frontmatter: {sample}{more}",
        "These notes are invisible to type/project-filtered recall. Re-save "
        "each with `brain save` (same title minus any colon lands on the same "
        "slug and overwrites in place), or fix the YAML by hand in Obsidian.",
    )]


INDEX_BUSY_TIMEOUT_DEFAULT_S = 2.0


def index_busy_timeout() -> float:
    """How long a health check waits for the vector index's lock.

    Deliberately *much* shorter than `embed.SQLITE_BUSY_TIMEOUT_S`. Those 30s belong
    to writers, where waiting always beats failing. Doctor is read-only and runs
    inside the SessionStart hook, which Claude Code kills at 15s — and a killed hook
    drops the entire preload, which is strictly worse than the false INDEX_CORRUPT
    this replaced. Waiting longer cannot improve the diagnosis either: "someone holds
    the write lock" is the same answer at 2s as at 30s.

    Measured 2026-08-24: a single `PRAGMA integrity_check` against a locked index
    took 41.8s at the writer's 30s setting — nearly 3x the whole hook's budget.
    """
    raw = os.environ.get("BRAIN_DOCTOR_INDEX_TIMEOUT_S", "")
    if not raw:
        return INDEX_BUSY_TIMEOUT_DEFAULT_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return INDEX_BUSY_TIMEOUT_DEFAULT_S


def _reindex_running() -> bool:
    """True when a `brain reindex` holds the cross-process lock.

    Checked before touching sqlite at all: it is a single file stat, where every
    index check below would otherwise pay the busy timeout in turn. Three of them
    connect, so without this the hook's cost under a reindex is additive.
    """
    try:
        from . import embed
        return embed.reindex_lock_held()
    except Exception:
        return False


def _is_lock_error(exc: BaseException) -> bool:
    """True when sqlite refused because someone else holds the write lock.

    A locked database is a *healthy* database with a writer in it — during a reindex
    that is the expected state, not a fault. One predicate, owned by embed, so the
    module that raises IndexBusy and the module that classifies findings agree.
    """
    from . import embed
    return embed._is_lock_error(exc)


def _check_vector_index(brain: Path) -> list[Finding]:
    idx = brain / ".index" / "embeddings.sqlite"
    if not idx.exists():
        return [Finding(
            "info", "INDEX_MISSING",
            "Vector index not yet built.",
            "The MCP server warms it up on startup; first brain_recall builds it otherwise.",
        )]
    if _reindex_running():
        return [Finding(
            "info", "INDEX_BUSY",
            "A reindex is running; skipped the integrity check rather than wait for it.",
            "Nothing to do — re-run `brain doctor` once it finishes.",
        )]
    try:
        from . import embed
        conn = sqlite3.connect(
            embed.index_uri(idx), uri=True, timeout=index_busy_timeout()
        )
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        # A lock is not corruption. This check used to report both as INDEX_CORRUPT
        # and tell the user to delete the index — advice that costs a ~300s rebuild
        # of perfectly valid vectors. A reindex holds the write lock for its chunk
        # commits, and SessionStart both kicks a reindex and renders this banner, so
        # a healthy index hitting the old 5s default timeout was an ordinary session
        # start, not a corner case (reproduced 2026-08-24).
        if _is_lock_error(e):
            return [Finding(
                "info", "INDEX_BUSY",
                f"Vector index is locked by another process: {e}",
                "A reindex is most likely running. Nothing to do — recall falls back "
                "to ripgrep meanwhile, and this clears on its own.",
            )]
        return [Finding(
            "warn", "INDEX_CORRUPT",
            f"Vector index at {idx} is unreadable: {e}",
            "Delete .index/embeddings.sqlite; it will rebuild on next query. "
            "Recall falls back to ripgrep until then.",
        )]
    if row and row[0] == "ok":
        size_mb = round(idx.stat().st_size / 1e6, 2)
        return [Finding("ok", "INDEX_OK", f"vector index {size_mb} MB, integrity_check=ok")]
    return [Finding(
        "warn", "INDEX_CORRUPT",
        f"Vector index integrity_check returned: {row!r}",
        "Delete .index/embeddings.sqlite to rebuild.",
    )]


INDEX_BACKLOG_WARN = 50


def _check_index_stale(brain: Path) -> list[Finding]:
    """Backlog of un-embedded files.

    Recall syncs only a time-boxed slice, so a backlog is not a correctness bug —
    it is a latency bug that hides. On 2026-08-24 an index six weeks behind turned
    a single `brain recall` into a 2m45s stall, and nothing anywhere said so.
    """
    if os.environ.get("BRAIN_EMBED", "1") == "0":
        return []
    if _reindex_running():
        # The backlog is being drained right now; reporting it as stale would be both
        # wrong and expensive — backlog() connects, so it would sit on the lock.
        return [Finding("info", "INDEX_REINDEXING", "a reindex is draining the backlog")]
    try:
        from . import embed
        # Doctor's timeout, not the reader default: three checks connect from
        # inside a hook Claude Code kills at 15s, so the waits are additive.
        pending = embed.EmbedIndex.backlog(timeout=index_busy_timeout())
    except Exception:
        # IndexBusy included: the backlog is unknown, and _check_vector_index
        # already reports INDEX_BUSY for the same lock. Deferred, not wrong.
        return []
    if pending == 0:
        return [Finding("ok", "INDEX_FRESH", "vector index up to date")]
    cmd = "brain reindex"
    if pending >= INDEX_BACKLOG_WARN:
        return [Finding(
            "warn", "INDEX_STALE",
            f"{pending} file(s) missing from the vector index.",
            f"Recall embeds only a {embed.EmbedIndex.SYNC_BUDGET_DEFAULT:.0f}s slice per call, "
            f"so this backlog slows every recall until it clears. Run `{cmd}`.",
        )]
    return [Finding(
        "info", "INDEX_STALE",
        f"{pending} file(s) pending embedding; recall will absorb them incrementally.",
    )]


def _check_index_recipe(brain: Path) -> list[Finding]:
    """Index built by a superseded embed_text() recipe.

    Invisible to INDEX_STALE, which compares mtimes: the files did not change, the
    recipe did. Without this the index would sit on old-recipe vectors indefinitely
    if the background reindex never ran.
    """
    if os.environ.get("BRAIN_EMBED", "1") == "0":
        return []
    if _reindex_running():
        # A reindex is what transitions the recipe. Reporting it stale mid-rebuild is
        # both wrong and, because text_recipe_changed() connects, slow.
        return []
    try:
        from . import embed
        if not embed.text_recipe_changed(timeout=index_busy_timeout()):
            return []
    except Exception:
        return []
    return [Finding(
        "warn", "INDEX_RECIPE_STALE",
        "Vector index was built with a superseded embedding recipe.",
        "Run `brain reindex` to rebuild. Recall keeps working on the old vectors "
        "until then; foreground syncs deliberately skip the rebuild.",
    )]


def _check_editable_install() -> list[Finding]:
    try:
        import brain_mcp
    except ImportError as e:
        return [Finding(
            "error", "BRAIN_MCP_IMPORT_FAILED",
            f"brain_mcp import failed: {e}",
            "Re-run setup-mac.sh or setup-windows.ps1 to reinstall into the venv.",
        )]
    mod_file = Path(brain_mcp.__file__).resolve()
    if "site-packages" not in mod_file.parts:
        return [Finding(
            "warn", "EDITABLE_INSTALL",
            f"brain_mcp appears installed editable ({mod_file}).",
            "CLAUDE.md forbids pip install -e . — hooks break from foreign cwds. "
            "Re-run setup-mac.sh with a plain reinstall.",
        )]
    return [Finding("ok", "INSTALL_OK", f"brain_mcp at {mod_file.parent}")]


def _check_fastembed() -> list[Finding]:
    if os.environ.get("BRAIN_EMBED", "1") == "0":
        return [Finding(
            "info", "EMBED_DISABLED",
            "BRAIN_EMBED=0; vector search disabled, using ripgrep fallback.",
        )]
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return [Finding(
            "warn", "FASTEMBED_MISSING",
            "fastembed not importable; recall will use ripgrep only.",
            "Reinstall the MCP server venv (setup-mac.sh / setup-windows.ps1).",
        )]
    return [Finding("ok", "FASTEMBED_OK", "fastembed importable")]


_ACTIVITY_COLUMNS_RE = re.compile(
    r"\[sig=([YN]) sav=([YN]) nud=([YN])(?: pro=([YN]))?(?: too=([YN]))?(?: sys=([YN]))?"
    r"(?: re=([YN]))?\]"
)
# `<stamp> <account> <project> [sig=…` — account never contains a space
# (BRAIN_ACCOUNT, default "claude"); project may, so it is everything up to the
# column block.
_ACTIVITY_KEY_RE = re.compile(r"^\S+ \S+ (\S+) (.*?) \[sig=")
SAVE_GAP_WINDOW = 30  # tail of activity.md to examine
SAVE_GAP_THRESHOLD = 3  # signal-without-save count that triggers a WARN
PROMISE_GAP_THRESHOLD = 1  # any unfulfilled promise is a bug worth flagging


def _tail_activity(brain: Path, n: int) -> list[str]:
    activity = brain / "activity.md"
    if not activity.exists():
        return []
    try:
        with activity.open("r", encoding="utf-8") as f:
            return list(deque(f, maxlen=n))
    except Exception:
        return []


def _audited_rows(lines: list[str]) -> list[dict]:
    """Parse activity rows into dicts and fold gate re-entries into the turn
    they belong to.

    `hooks/stop.py` writes a row *before* it blocks on an unfulfilled promise,
    and a second row (tagged `re=Y`) when Claude Code re-enters it after the
    model has saved or recanted. The second row's assistant text still spans
    the whole turn, original promise included, so it reads `pro=Y` either way
    — and the first row is `pro=Y sav=N` by construction. Counted naively,
    every gate block therefore produced a PROMISE_GAP warning at the next
    SessionStart for a promise the gate had already made the model resolve
    (2026-09-01). So a re-entry row is never a row of its own: it supersedes
    the nearest preceding row from the same account+project, contributing its
    `sav` (the save may have happened *because* the gate fired) and marking
    the turn `gated`, which the promise check skips — the gate ran and the
    model answered it, whichever way. Legacy rows without a `re=` column are
    ordinary rows.
    """
    rows: list[dict] = []
    for line in lines:
        m = _ACTIVITY_COLUMNS_RE.search(line)
        if not m:
            continue
        key_m = _ACTIVITY_KEY_RE.match(line)
        row = {
            "key": (key_m.group(1), key_m.group(2)) if key_m else None,
            "sig": m.group(1), "sav": m.group(2), "nud": m.group(3),
            "pro": m.group(4), "too": m.group(5), "sys": m.group(6),
            "reentry": m.group(7) == "Y",
            "gated": False,
        }
        if row["reentry"]:
            for prior in reversed(rows):
                if prior["key"] == row["key"] and not prior["reentry"]:
                    prior["gated"] = True
                    if row["sav"] == "Y":
                        prior["sav"] = "Y"
                    break
            continue
        rows.append(row)
    return rows


def _check_save_gap(brain: Path) -> list[Finding]:
    """Warn when recent activity shows save-signals without brain_save calls.

    Only counts lines written after the audit-column format landed. Older lines
    have no `[sig=... sav=... nud=...]` suffix and are silently skipped.
    """
    lines = _tail_activity(brain, SAVE_GAP_WINDOW)
    if not lines:
        return []

    audited = 0
    signal_no_save_nudged = 0
    signal_no_save_unnudged = 0
    for row in _audited_rows(lines):
        if row["too"] == "N":
            continue  # brain tools weren't callable — a missed save was unsatisfiable
        if row["sys"] == "Y":
            continue  # system-generated turn (notification/skill expansion) —
            # its "user text" wasn't typed by the user, so sig is meaningless
        audited += 1
        if row["sig"] == "Y" and row["sav"] == "N":
            if row["nud"] == "Y":
                signal_no_save_nudged += 1
            else:
                signal_no_save_unnudged += 1

    total_gap = signal_no_save_nudged + signal_no_save_unnudged
    if audited == 0:
        return [Finding(
            "info", "SAVE_GAP_NO_DATA",
            "No audited activity lines yet — new stop.py format hasn't rolled out.",
        )]
    if total_gap < SAVE_GAP_THRESHOLD:
        return [Finding(
            "ok", "SAVE_GAP_OK",
            f"{audited} audited turns in window; {total_gap} signal-without-save.",
        )]
    detail = f"nudged={signal_no_save_nudged}, unnudged={signal_no_save_unnudged}"
    return [Finding(
        "warn", "SAVE_GAP",
        f"{total_gap} of last {audited} turns had a save-signal with no brain save ({detail}).",
        "If 'unnudged' dominates, enable the nudge (unset BRAIN_NUDGE or set =1). "
        "If 'nudged' dominates, the model is ignoring the nudge — tighten "
        "templates/global-CLAUDE.md proactive-save triggers.",
    )]


def _check_promise_gap(brain: Path) -> list[Finding]:
    """Warn when recent activity shows save-*promises* without brain_save calls.

    This is the observability backstop behind the Stop-hook gate in
    `hooks/stop.py`. With the gate enabled (default), these should be near-zero
    — if they're not, either the gate is disabled (BRAIN_STOP_GATE=0), the
    promise regex missed a phrasing, or the session died between the gate
    blocking and the model answering it.

    Only examines lines written after the `pro=` column landed. Older lines
    have no `pro=` suffix and are silently skipped. Rows tagged `too=N` (the
    brain MCP server wasn't registered that session, so the promised save was
    physically uncallable) are also skipped — that's an infra failure, not a
    model bug. See the 2026-06-03 false positive this guards against.

    Rows tagged `sys=Y` (system-generated turn) are deliberately NOT skipped
    here, unlike in _check_save_gap: `pro` measures the *assistant's* text,
    which is genuinely model-authored whatever triggered the turn, so an
    unfulfilled promise on a notification turn is a real miss.

    Turns the gate blocked and the model then answered (a `re=Y` row follows —
    see `_audited_rows`) are skipped: the gate did its job, and whether the
    model saved or recanted, there is no unfulfilled promise left.
    """
    lines = _tail_activity(brain, SAVE_GAP_WINDOW)
    if not lines:
        return []

    audited = 0
    unfulfilled = 0
    for row in _audited_rows(lines):
        if row["pro"] is None:
            continue  # old-format line, no promise column
        if row["too"] == "N":
            continue  # brain tools weren't callable this session — the promise
            # was physically unsatisfiable (infra failure, not a model bug)
        audited += 1
        if row["gated"]:
            continue  # the gate blocked and the model answered it: not a gap
        if row["pro"] == "Y" and row["sav"] == "N":
            unfulfilled += 1

    if audited == 0:
        return []
    if unfulfilled < PROMISE_GAP_THRESHOLD:
        return [Finding(
            "ok", "PROMISE_GAP_OK",
            f"{audited} audited turns in window; no unfulfilled save-promises.",
        )]
    return [Finding(
        "warn", "PROMISE_GAP",
        f"{unfulfilled} of last {audited} audited turns promised a save but no brain save/checkpoint ran.",
        "The Stop-hook gate should catch these. If it didn't: check that "
        "BRAIN_STOP_GATE is not set to 0, and consider tightening "
        "hooks/_savesig.py PROMISE_PATTERNS if a phrasing slipped through.",
    )]


def _check_stale_uncommitted(
    brain: Path,
    project: str | None,
    project_cwd: str | Path | None,
) -> list[Finding]:
    """Flag when the project has on-disk changes that postdate the latest
    session checkpoint. Catches the specific failure mode behind the 2026-04-22
    MM-ToolDecoder incident: a window died mid-work after significant edits,
    nothing checkpointed, and the next session started with no trail.

    Disabled by BRAIN_STALE_CHECK=0. Skipped when we have no project cwd, no
    git repo, or no prior checkpoints (first-session case — nothing to compare).
    """
    if not project or not project_cwd:
        return []
    if os.environ.get("BRAIN_STALE_CHECK", "1").strip() in ("0", "false", "no", "off"):
        return []
    cwd_path = Path(project_cwd).expanduser()
    if not (cwd_path / ".git").exists():
        return []

    sessions = vault.project_dir(project, "sessions", root=brain)
    if not sessions.exists():
        return []
    checkpoints = list(sessions.glob("*.md"))
    if not checkpoints:
        return []
    # safe_mtime, not p.stat(): a checkpoint deleted by sync or brain-compact
    # between the glob and the stat raised out of the comprehension (F4).
    latest_mtime = max(vault.safe_mtime(p) for p in checkpoints)
    if latest_mtime == 0.0:
        return []

    commit_age_hours: float | None = None
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd_path), "log", "-1", "--format=%ct"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                ts = int(result.stdout.strip().splitlines()[0])
                if ts > latest_mtime:
                    commit_age_hours = (datetime.now().timestamp() - ts) / 3600
            except ValueError:
                pass
    except Exception:
        pass

    uncommitted_age_hours: float | None = None
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd_path), "status", "--porcelain"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            newest = 0.0
            for line in result.stdout.splitlines():
                # porcelain format: "XY path" (path may be quoted for special chars)
                rest = line[3:] if len(line) > 3 else ""
                rest = rest.strip().strip('"')
                if "->" in rest:  # renames: "old -> new"
                    rest = rest.split("->")[-1].strip()
                if not rest:
                    continue
                fpath = cwd_path / rest
                try:
                    m = fpath.stat().st_mtime
                    if m > newest:
                        newest = m
                except OSError:
                    continue
            if newest > latest_mtime:
                uncommitted_age_hours = (datetime.now().timestamp() - newest) / 3600
    except Exception:
        pass

    if commit_age_hours is None and uncommitted_age_hours is None:
        return [Finding(
            "ok", "STALE_UNCOMMITTED_OK",
            f"project '{project}' git state matches latest checkpoint.",
        )]

    parts: list[str] = []
    if commit_age_hours is not None:
        parts.append(f"commits as recent as {int(commit_age_hours)}h ago")
    if uncommitted_age_hours is not None:
        parts.append(f"uncommitted edits as recent as {int(uncommitted_age_hours)}h ago")
    latest_iso = datetime.fromtimestamp(latest_mtime).strftime("%Y-%m-%d %H:%M")
    return [Finding(
        "warn", "STALE_UNCOMMITTED",
        f"project '{project}' has {' and '.join(parts)}, postdating the last Brain checkpoint ({latest_iso}).",
        "Work happened since the last checkpoint. If you did it in this project, "
        "reconstruct what changed and call brain_checkpoint to capture it. If it "
        "was done outside Claude Code (manual edits, another tool), you can ignore "
        "this — or set BRAIN_STALE_CHECK=0 per-install to silence it.",
    )]


def _check_project_overview(brain: Path, project: str | None) -> list[Finding]:
    if not project:
        return []
    overview = vault.project_dir(project, "overview.md", root=brain)
    if not overview.exists():
        return [Finding(
            "warn", "OVERVIEW_MISSING",
            f"No overview.md for project '{project}' — session bundle is missing project context.",
            "The SessionStart hook normally writes a stub on first run; if you see this, the hook "
            "either didn't run or couldn't write to the vault. Check hook logs on this machine.",
        )]
    try:
        if vault.is_overview_stub(overview):
            return [Finding(
                "info", "OVERVIEW_STUB",
                f"project '{project}' has a stub overview.md — model should upgrade it this session.",
                "The model reads the stub's Source material pointers and calls brain_save to "
                "replace it with a real summary. Automatic on first turn per global-CLAUDE.md.",
            )]
    except Exception:
        pass
    return [Finding("ok", "OVERVIEW_OK", f"project '{project}' has overview.md")]


def _check_stale_checkpoint(brain: Path, project: str | None) -> list[Finding]:
    if not project:
        return []
    sessions = vault.project_dir(project, "sessions", root=brain)
    if not sessions.exists():
        return [Finding(
            "info", "NO_CHECKPOINTS",
            f"No checkpoints for project '{project}' yet.",
            "SessionEnd / PreCompact hooks or brain_checkpoint will create the first one.",
        )]
    checkpoints = list(sessions.glob("*.md"))
    if not checkpoints:
        return []
    newest_mtime = max(vault.safe_mtime(p) for p in checkpoints)
    if newest_mtime == 0.0:
        return []
    age_days = (datetime.now().timestamp() - newest_mtime) / 86400
    if age_days > 30:
        return [Finding(
            "info", "STALE_CHECKPOINT",
            f"Newest checkpoint for '{project}' is {int(age_days)} days old.",
            "Checkpoint hooks may not be firing; check hook logs on this machine.",
        )]
    return [Finding("ok", "CHECKPOINT_FRESH", f"newest checkpoint for '{project}' is {int(age_days)}d old")]


# Bytes, whole file including frontmatter. Set from the 2026-07-30 compaction pass: a tight
# rule + Why + How-to-apply lands at 1.0-1.5 KB, while the bodies that had actually bloated the
# preload were 1.7-3.5 KB. Flagging at 1500 catches essays without nagging about near-misses.
MEMORY_BODY_SOFT_LIMIT = 1500


# Part 1 is packed against `vault.banner_reserve_chars()` in every process — the
# hook's included — so doctor sizing with a placeholder of that length renders the
# exact parts the hooks will emit (the real banner is clipped into the reserve).


def _check_bundle_budget(project: str | None,
                         bundle_cache: dict | None = None) -> list[Finding]:
    """Both preload paths drop entries once they hit their byte budget — silently.

    A skipped memory is indistinguishable from one the model chose to ignore, so this
    failure mode reads as "the model stopped following my corrections". The session and
    subagent budgets are sized independently: on 2026-08-06 the corpus had grown past
    the 44 KB subagent budget (3 feedback rules dropped from every subagent) while this
    check — then sizing only the 72 KB session budget — reported OK.

    Two budgets, one read: the corpus is collected once and assembled twice (F12).
    The assembled bundles are stashed in `bundle_cache` (`session`, `subagent`) so
    the SessionStart hook can render what doctor already built.

    Sizing also renders exactly what the hooks would emit — `render_parts` at the
    hook output cap — because the vault-side byte budget cannot see the harness's
    per-hook cap (2026-09-01): a 44 KB bundle passed BUNDLE_BUDGET_OK for weeks
    while Claude Code spilled it to a file the model never read.
    `PRELOAD_PART_OVERSIZED` fires if any part would exceed the cap and
    `PRELOAD_OVERFLOW` names what fell off the last part into the catalogue.
    """
    from . import brain_prep

    findings: list[Finding] = []
    try:
        candidates = vault.collect_preload_candidates(project)
    except Exception as exc:  # never let a health check break the session banner
        return [Finding(
            "info", "BUNDLE_CHECK_FAILED",
            f"Could not read the preload corpus to size it: {exc}",
        )]

    def size_one(label: str, warn_code: str, ok_code: str, knob: str,
                 cache_key: str, budget_kb: float | None, slim: bool,
                 banner_reserve: int) -> list[Finding]:
        try:
            bundle = vault.assemble_bundle(candidates, budget_kb=budget_kb, slim=slim)
        except Exception as exc:
            return [Finding(
                "info", "BUNDLE_CHECK_FAILED",
                f"Could not build the {label} preload bundle to size it: {exc}",
            )]
        if bundle_cache is not None:
            bundle_cache[cache_key] = bundle
        out: list[Finding] = []
        limit = bundle.get("budget_limit_kb")
        used = bundle.get("budget_consumed_kb")
        skipped = bundle.get("skipped_sections") or {}
        pinned_note = ""
        if bundle.get("pinned_clipped"):
            pinned_note = (
                f" ({len(bundle['pinned_clipped'])} pinned item(s) clipped to the per-item "
                f"cap, {bundle.get('pinned_kb')} KB pinned)"
            )
        if skipped:
            detail = ", ".join(f"{n} {sec}" for sec, n in sorted(skipped.items()))
            out.append(Finding(
                "warn", warn_code,
                f"{label} preload hit its {limit} KB budget and skipped {detail}{pinned_note}.",
                "Those memories are saved but never loaded, so their rules stop applying with "
                f"no visible failure. Raise {knob}, or compact oversized memories. The skipped "
                "names are catalogued at the end of the preload for `brain recall`.",
            ))
        else:
            deferred = bundle.get("deferred_why_kb") or 0
            note = f", {deferred} KB of **Why:** deferred to recall" if deferred else ""
            out.append(Finding(
                "ok", ok_code,
                f"{label} preload {used}/{limit} KB, nothing skipped{note}{pinned_note}",
            ))

        # Now the harness side: what the hook entries would actually deliver.
        cap = vault.hook_output_cap()
        try:
            parts = brain_prep.render_parts(bundle, banner="#" * banner_reserve)
        except Exception as exc:
            out.append(Finding(
                "info", "BUNDLE_CHECK_FAILED",
                f"Could not render the {label} preload parts to size them: {exc}",
            ))
            return out
        oversized = [i + 1 for i, p in enumerate(parts) if len(p) > cap]
        if oversized:
            out.append(Finding(
                "warn", "PRELOAD_PART_OVERSIZED",
                f"{label} preload part(s) {oversized} exceed the {cap}-char hook output cap "
                f"(sizes: {[len(p) for p in parts]}).",
                "Claude Code spills a hook output over 10,000 chars to a file the model "
                "never reads. Lower BRAIN_HOOK_OUTPUT_CAP is not the fix — a single item "
                "larger than a part is; find it with `brain doctor` OVERSIZED_MEMORIES.",
            ))
        delivered = {
            path for p in parts
            for path in re.findall(r"^### (.+)$", p, flags=re.MULTILINE)
        }
        overflow = [
            vault.memory_display_name(item["path"])
            for section in bundle.get("sections", [])
            for item in section["items"]
            if item["path"] not in delivered
        ]
        if overflow:
            sample = ", ".join(overflow[:5])
            more = f" (+{len(overflow) - 5} more)" if len(overflow) > 5 else ""
            out.append(Finding(
                "warn", "PRELOAD_OVERFLOW",
                f"{label} preload: {len(overflow)} memories fit the {limit} KB budget but not "
                f"in {vault.PRELOAD_PARTS} hook parts of {cap} chars; catalogued by name on "
                f"the last part: {sample}{more}.",
                "They are one `brain recall <name>` away, not lost. To load them by default, "
                "compact the largest memories, or register more hook parts (vault.PRELOAD_PARTS).",
            ))
        return out

    findings.extend(size_one(
        "SessionStart", "BUNDLE_SATURATED", "BUNDLE_BUDGET_OK",
        "BRAIN_BUNDLE_BUDGET_KB", "session", None, False, vault.banner_reserve_chars()))
    if os.environ.get("BRAIN_SUBAGENT_PRELOAD", "1") != "0":
        # Mirror the SubagentStart hook exactly: slim bundle (project feedback but no
        # overview/checkpoint), subagent budget, no banner.
        findings.extend(size_one(
            "SubagentStart", "SUBAGENT_BUNDLE_SATURATED", "SUBAGENT_BUNDLE_OK",
            "BRAIN_SUBAGENT_BUDGET_KB", "subagent", vault.subagent_budget_kb(), True, 0))
    return findings


def _check_pinned_sizes(brain: Path) -> list[Finding]:
    """Overviews and newest checkpoints over their per-item preload cap.

    `_check_memory_sizes` scans user/feedback only, so on 2026-09-01 an 80 KB
    checkpoint consumed the whole session bundle (81.98/72 KB, every user and
    feedback entry skipped) while MEMORY_SIZES_OK reported clean (F5). Pinned
    items are clipped now, so this is informational: what is clipped is what the
    session does not see.
    """
    oversized: list[tuple[int, int, str]] = []
    projects = vault.projects_root(brain)
    if not projects.exists():
        return [Finding("ok", "PINNED_SIZES_OK", "no projects to size")]
    for proj in sorted(p for p in projects.iterdir() if p.is_dir()):
        overview = proj / "overview.md"
        if overview.exists():
            try:
                n = len(overview.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                n = 0
            cap = vault.pinned_max_chars("overview")
            if n > cap:
                oversized.append((n, cap, f"projects/{proj.name}/overview.md"))
        latest = vault.latest_checkpoint(proj / "sessions")
        if latest is not None:
            try:
                n = len(latest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                n = 0
            cap = vault.pinned_max_chars("checkpoint")
            if n > cap:
                oversized.append((n, cap, f"projects/{proj.name}/sessions/{latest.name}"))
    if not oversized:
        return [Finding(
            "ok", "PINNED_SIZES_OK",
            "every overview and newest checkpoint preloads whole (under its per-item cap)",
        )]
    oversized.sort(reverse=True)
    worst = ", ".join(f"{name} ({n} > {cap} chars)" for n, cap, name in oversized[:3])
    more = f" (+{len(oversized) - 3} more)" if len(oversized) > 3 else ""
    return [Finding(
        "info", "OVERSIZED_PINNED",
        f"{len(oversized)} pinned preload item(s) exceed their per-item cap and load "
        f"clipped: {worst}{more}.",
        "The preload carries the head plus a marker saying how to read the rest. Keep "
        "overviews under BRAIN_PRELOAD_PINNED_MAX_CHARS; long checkpoints are fine on "
        "disk, the next session just sees the top of them.",
    )]


def _preload_cost(text: str) -> int:
    """Bytes this memory actually contributes to a preload bundle.

    Not `st_size`. Since the two-tier preload (2026-08-25) the bundle carries
    `vault.preload_text()` -- the rule and the how-to-apply, with the `**Why:**`
    replaced by a one-line marker -- so a memory's size on disk and its cost in
    context have diverged. Measured over this vault's flagged memories the day
    after: 20480 B on disk, 12653 B actually preloaded.

    Honours `BRAIN_PRELOAD_DEFER_WHY`: with deferral off, whole bodies load again
    and the raw text is the true cost.
    """
    from . import vault

    rendered = vault.preload_text(text) if vault.defer_why_enabled() else text
    return len(rendered.encode("utf-8"))


def _check_memory_sizes(brain: Path) -> list[Finding]:
    """Memories whose *preloaded* bytes crowd the budget and push others out.

    Scans project-scoped feedback (projects/*/feedback/) too — it fills the same
    bundles as global feedback, just for fewer sessions.

    The soft limit is measured against preload cost rather than file size, because
    protecting the bundle budget is the only reason a memory's length ever mattered.
    Measuring `st_size` instead flagged files that cost nothing -- five of the eight
    it reported on 2026-08-25 preloaded under the limit already -- and acting on that
    means lossily rewriting the user's own corrections to buy budget already
    recovered, which is the exact failure deferral was chosen to avoid. It would
    equally miss the opposite case: a memory that is small on disk and entirely
    rule text, which is 100% preload cost.
    """
    from . import vault

    oversized: list[tuple[int, int, str]] = []
    dirs = [brain / "user", brain / "feedback"]
    projects = vault.projects_root(brain)
    if projects.exists():
        dirs += sorted(p for p in projects.glob("*/feedback") if p.is_dir())
    for d in dirs:
        if not d.exists():
            continue
        sub = str(d.relative_to(brain))
        for f in sorted(d.rglob("*.md")):
            # Safe today -- these dirs hold no sessions -- but an unguarded rglob
            # is the shape that let doctor drift from the shared predicate before.
            if not vault.is_memory_path(f, brain):
                continue
            try:
                raw = f.read_text(encoding="utf-8")
            except OSError:
                continue
            cost = _preload_cost(raw)
            if cost > MEMORY_BODY_SOFT_LIMIT:
                oversized.append((cost, len(raw.encode("utf-8")), f"{sub}/{f.name}"))

    if not oversized:
        return [Finding(
            "ok", "MEMORY_SIZES_OK",
            f"all user/feedback memories preload within the {MEMORY_BODY_SOFT_LIMIT} B "
            f"soft limit",
        )]

    oversized.sort(reverse=True)
    total_kb = sum(c for c, _, _ in oversized) / 1024.0
    deferred_kb = sum(r - c for c, r, _ in oversized) / 1024.0
    worst = ", ".join(name for _, _, name in oversized[:3])
    # Both numbers, always: "3.3 KB preloaded" is the actionable one, and the
    # deferred figure is what stops a reader concluding the file must be cut down.
    deferred_note = (
        f", {deferred_kb:.1f} KB more already deferred to recall" if deferred_kb >= 0.05 else ""
    )
    return [Finding(
        "info", "OVERSIZED_MEMORIES",
        f"{len(oversized)} memories exceed the {MEMORY_BODY_SOFT_LIMIT} B preload soft "
        f"limit ({total_kb:.1f} KB preloaded{deferred_note}); largest: {worst}.",
        "This is preload cost, not file size. If a memory has no `**Why:**` section, "
        "restructuring it into rule / Why / How-to-apply is lossless and usually enough. "
        "Only then compact: reference the file, commit or doc instead of copying its "
        "detail into the memory.",
    )]


# --- Corpus-hygiene checks -------------------------------------------------
# Added 2026-08-06 after the first manual dedup audit found ~30 stale or
# duplicate entries polluting recall (the "bad information pulled into
# context" failure). These catch the *mechanical* classes automatically;
# semantic supersession still needs a periodic model-driven review pass.

STUB_ONLY_STALE_DAYS = 30
NEAR_DUP_THRESHOLD_DEFAULT = 0.92
NEAR_DUP_MAX_REPORTED = 5
def _non_memory_names() -> frozenset:
    """Vault bookkeeping filenames, single-sourced from vault.EXCLUDE_FILES.

    This used to be a third independent list. It was the only one that knew about
    README.md, so `brain list` returned the vault's README as a memory while this
    check correctly ignored it.
    """
    from . import vault
    return vault.EXCLUDE_FILES


def _check_shadowed_overviews(brain: Path) -> list[Finding]:
    """A stub overview.md coexisting with a sibling memory named like an
    overview means a save landed under the wrong name: the stub keeps
    preloading while the real context never does. Found 2026-08-06 —
    machiner-calcs' real overview was saved as `machiner-calcs-overview`,
    so every session there got the stub for months."""
    from brain_mcp import vault

    projects = vault.projects_root(brain)
    if not projects.exists():
        return []
    hits: list[str] = []
    for proj in sorted(p for p in projects.iterdir() if p.is_dir()):
        ov = proj / "overview.md"
        if not ov.exists() or not vault.is_overview_stub(ov):
            continue
        for sib in sorted(proj.glob("*.md")):
            if sib.name != "overview.md" and "overview" in sib.stem.lower():
                hits.append(f"projects/{proj.name}/{sib.name}")
                break
    if not hits:
        return [Finding("ok", "OVERVIEW_SHADOW_OK", "no stub overview is shadowing a misnamed real one")]
    return [Finding(
        "warn", "STUB_SHADOWED_OVERVIEW",
        f"{len(hits)} project(s) have a stub overview.md beside what looks like "
        f"the real overview under another name: {', '.join(hits[:3])}"
        + (f" (+{len(hits) - 3} more)" if len(hits) > 3 else "") + ".",
        "The stub is what preloads. Re-save the real body with "
        "`brain save project overview --project <X>` (overwrites the stub), "
        "then `brain forget` the misnamed file.",
    )]


def _check_stub_only_projects(brain: Path) -> list[Finding]:
    """Projects containing nothing but an old stub overview are almost always
    wrong-cwd launches (a session started in ~, ~/src, a scratch dir), not
    real projects — 7 such dirs were found in the 2026-08-06 audit. Only
    flagged once both the stub and the newest session checkpoint are older
    than STUB_ONLY_STALE_DAYS, so genuinely new projects never appear."""
    from brain_mcp import vault

    projects = vault.projects_root(brain)
    if not projects.exists():
        return []
    now = datetime.now().timestamp()
    cutoff = now - STUB_ONLY_STALE_DAYS * 86400
    hits: list[str] = []
    for proj in sorted(p for p in projects.iterdir() if p.is_dir()):
        ov = proj / "overview.md"
        if not ov.exists() or not vault.is_overview_stub(ov):
            continue
        memories = [
            p for p in proj.rglob("*.md")
            if not vault.is_session_path(p) and vault.is_memory_path(p, brain) and p != ov
        ]
        if memories:
            continue
        try:
            newest = ov.stat().st_mtime
        except OSError:
            continue
        for s in proj.glob("sessions/*.md"):
            try:
                newest = max(newest, s.stat().st_mtime)
            except OSError:
                continue
        if newest < cutoff:
            hits.append(proj.name)
    if not hits:
        return []
    return [Finding(
        "info", "STUB_ONLY_PROJECTS",
        f"{len(hits)} project dir(s) contain only a stale stub overview "
        f"(no memories, nothing touched in {STUB_ONLY_STALE_DAYS}+ days): "
        f"{', '.join(hits[:5])}" + (f" (+{len(hits) - 5} more)" if len(hits) > 5 else "") + ".",
        "Usually a session launched in the wrong directory — "
        "`brain forget Brain/projects/<name>/overview.md` cleans each up. "
        "A real-but-dormant project can be left alone; its stub upgrades on the next session there.",
    )]


def _check_near_duplicates(brain: Path) -> list[Finding]:
    """Flag memory pairs whose stored embedding vectors are nearly identical —
    the fingerprint of duplicate saves, superseded-entry buildup, or the same
    fact recorded in two scopes. Reads vectors already in the index (no model
    load, no embedding); silently skips when the index or numpy is absent."""
    if os.environ.get("BRAIN_EMBED", "1") == "0":
        return []
    idx = brain / ".index" / "embeddings.sqlite"
    if not idx.exists():
        return []
    if _reindex_running():
        return []  # corpus hygiene can wait; the lock costs more than the finding
    try:
        import numpy as np

        from brain_mcp import vault

        threshold = float(os.environ.get("BRAIN_DUP_THRESHOLD", NEAR_DUP_THRESHOLD_DEFAULT))

        paths: list[Path] = []
        vectors: list = []
        from brain_mcp import embed

        conn = sqlite3.connect(
            embed.index_uri(idx), uri=True, timeout=index_busy_timeout()
        )
        try:
            for raw_path, blob in conn.execute("SELECT path, vector FROM embeddings"):
                p = Path(raw_path)
                if not p.is_absolute():
                    p = brain / p  # rows are keyed vault-relative since 2026-08-24
                try:
                    rel = p.relative_to(brain)
                except ValueError:
                    continue  # legacy key from another vault location; sync() clears it
                if not p.exists():
                    continue  # deleted since last index sync
                if "sessions" in rel.parts or not vault.is_memory_path(p, brain):
                    continue
                if p.name == "overview.md" and vault.is_overview_stub(p):
                    continue  # stubs are boilerplate — they all match each other
                paths.append(rel)
                vectors.append(np.frombuffer(blob, dtype="<f4"))
        finally:
            conn.close()

        if len(paths) < 2:
            return []
        mat = np.vstack(vectors)
        norms = np.linalg.norm(mat, axis=1)
        norms[norms == 0] = 1.0
        mat = mat / norms[:, None]
        sims = mat @ mat.T
        iu = np.triu_indices(len(paths), k=1)
        flat = sims[iu]
        over = np.nonzero(flat >= threshold)[0]
        if over.size == 0:
            return [Finding(
                "ok", "NEAR_DUP_OK",
                f"no memory pairs above cosine {threshold} among {len(paths)} indexed memories",
            )]
        ranked = over[np.argsort(-flat[over])]
        pairs = [
            f"{paths[iu[0][i]]} ~ {paths[iu[1][i]]} ({flat[i]:.2f})"
            for i in ranked[:NEAR_DUP_MAX_REPORTED]
        ]
        more = f" (+{over.size - NEAR_DUP_MAX_REPORTED} more)" if over.size > NEAR_DUP_MAX_REPORTED else ""
        return [Finding(
            "info", "NEAR_DUPLICATE_MEMORIES",
            f"{over.size} memory pair(s) with cosine similarity >= {threshold}: "
            + "; ".join(pairs) + more + ".",
            "Likely duplicates or superseded chains — review each pair and merge or "
            "`brain forget` the stale one. Deliberate per-project twins can be ignored; "
            "tune with BRAIN_DUP_THRESHOLD.",
        )]
    except Exception:
        return []  # hygiene check must never break the session banner


def _run_check(findings: list[Finding], name: str, fn, *args) -> None:
    """Run one check, converting a raise into a CHECK_FAILED finding.

    `check()` is called from the SessionStart hook, where an exception used to be
    treated as a fatal vault error: the hook emitted a banner blaming a package
    import and exited before building the bundle, so any one raising check cost
    the session every memory (F4). Eighteen checks with no isolation is eighteen
    ways to lose the preload; a failed check is a finding like any other.
    """
    try:
        findings.extend(fn(*args))
    except Exception as exc:
        findings.append(Finding(
            "info", "CHECK_FAILED",
            f"health check {name} raised {type(exc).__name__}: {exc}",
            "The preload is unaffected. Run `brain doctor` for the full report; if it "
            "reproduces, the check has a bug worth a fix.",
        ))


def check(
    project: str | None = None,
    project_cwd: str | Path | None = None,
    bundle_cache: dict | None = None,
) -> list[dict]:
    """Run every health check; never raises for a check's own bug.

    `bundle_cache`, when given, receives the preload bundles the budget check
    assembled (`session`, `subagent`) so the SessionStart hook renders what doctor
    already built instead of reading the corpus again (F12).
    """
    findings: list[Finding] = []

    try:
        vault_findings = _check_brain_vault()
    except Exception as exc:
        vault_findings = [Finding(
            "error", "BRAIN_VAULT_UNSET",
            f"could not resolve the vault: {type(exc).__name__}: {exc}",
        )]
    findings.extend(vault_findings)
    if any(f.severity == "error" for f in vault_findings):
        return [f.to_dict() for f in findings]

    brain = Path(os.environ["BRAIN_VAULT"]).expanduser() / "Brain"

    # Degrade to an unscoped run rather than raising: `check()` is called from the
    # SessionStart hook, and an exception there loses the entire preload. Reported
    # so the cause is visible instead of the project checks just going quiet.
    if project is not None:
        try:
            vault.validate_project_name(project)
        except ValueError as e:
            findings.append(Finding(
                "warn", "PROJECT_NAME_INVALID",
                f"project value cannot be used as a vault directory: {e}",
                "Project names are directory basenames — no separators, no '..', no "
                "drive letters. Project-scoped checks and preload are skipped this run.",
            ))
            project = None

    checks = (
        ("subdirs", _check_subdirs, (brain,)),
        ("sync_conflicts", _check_sync_conflicts, (brain,)),
        ("frontmatter", _check_frontmatter, (brain,)),
        ("bundle_budget", _check_bundle_budget, (project, bundle_cache)),
        ("memory_sizes", _check_memory_sizes, (brain,)),
        ("pinned_sizes", _check_pinned_sizes, (brain,)),
        ("shadowed_overviews", _check_shadowed_overviews, (brain,)),
        ("stub_only_projects", _check_stub_only_projects, (brain,)),
        ("near_duplicates", _check_near_duplicates, (brain,)),
        ("vector_index", _check_vector_index, (brain,)),
        ("index_stale", _check_index_stale, (brain,)),
        ("index_recipe", _check_index_recipe, (brain,)),
        ("editable_install", _check_editable_install, ()),
        ("fastembed", _check_fastembed, ()),
        ("project_overview", _check_project_overview, (brain, project)),
        ("stale_checkpoint", _check_stale_checkpoint, (brain, project)),
        ("stale_uncommitted", _check_stale_uncommitted, (brain, project, project_cwd)),
        ("save_gap", _check_save_gap, (brain,)),
        ("promise_gap", _check_promise_gap, (brain,)),
    )
    for name, fn, args in checks:
        _run_check(findings, name, fn, *args)

    return [f.to_dict() for f in findings]


def worst_severity(findings: list[dict]) -> str:
    worst = "ok"
    for f in findings:
        sev = f.get("severity", "ok")
        if SEVERITY_ORDER.index(sev) > SEVERITY_ORDER.index(worst):
            worst = sev
    return worst


def render_banner(findings: list[dict], min_severity: str = "warn") -> str:
    """Render warn+error findings as a markdown banner. Returns '' if nothing to show."""
    min_idx = SEVERITY_ORDER.index(min_severity)

    def rank(f: dict) -> int:
        sev = f.get("severity", "ok")
        return SEVERITY_ORDER.index(sev) if sev in SEVERITY_ORDER else 0

    visible = [f for f in findings if rank(f) >= min_idx]
    if not visible:
        return ""
    # The banner sits *outside* the memory fence, i.e. in the position the model reads
    # as ours — and several findings interpolate vault filenames into their message.
    # A file named after a fence marker would otherwise close the fence from trusted
    # ground, so findings are defanged too (ROADMAP 3F).
    from . import vault

    lines = ["## Brain Health", ""]
    for f in visible:
        label = str(f.get("severity", "ok")).upper()
        code = f.get("code", "UNKNOWN")
        line = f"- **[{label}]** `{code}` — {vault.neutralize_fence(str(f.get('message', '')))}"
        if f.get("hint"):
            line += f"  \n  *{vault.neutralize_fence(str(f['hint']))}*"
        lines.append(line)
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    force_utf8_stdio()
    parser = argparse.ArgumentParser(description="Run Ai-Brain health checks.")
    parser.add_argument("--project", help="project basename for stale-checkpoint check")
    parser.add_argument(
        "--cwd",
        help="project working directory for stale-uncommitted check "
             "(defaults to current cwd when --project is given)",
    )
    parser.add_argument("--json", action="store_true", help="emit findings as JSON")
    parser.add_argument(
        "--quiet", action="store_true",
        help="only print warn/error findings",
    )
    args = parser.parse_args()

    cwd = args.cwd if args.cwd else (os.getcwd() if args.project else None)
    findings = check(args.project, cwd)

    if args.json:
        print(json.dumps(findings, indent=2))
        sys.exit(0 if worst_severity(findings) != "error" else 1)

    for f in findings:
        sev = f["severity"]
        if args.quiet and sev in ("ok", "info"):
            continue
        line = f"[{sev.upper():5s}] {f['code']}: {f['message']}"
        print(line)
        if f.get("hint"):
            print(f"        -> {f['hint']}")

    sys.exit(0 if worst_severity(findings) != "error" else 1)


if __name__ == "__main__":
    main()
