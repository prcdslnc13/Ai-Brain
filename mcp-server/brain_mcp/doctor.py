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


def _check_vector_index(brain: Path) -> list[Finding]:
    idx = brain / ".index" / "embeddings.sqlite"
    if not idx.exists():
        return [Finding(
            "info", "INDEX_MISSING",
            "Vector index not yet built.",
            "The MCP server warms it up on startup; first brain_recall builds it otherwise.",
        )]
    try:
        conn = sqlite3.connect(f"file:{idx}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
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
    r"\[sig=([YN]) sav=([YN]) nud=([YN])(?: pro=([YN]))?\]"
)
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
    for line in lines:
        m = _ACTIVITY_COLUMNS_RE.search(line)
        if not m:
            continue
        audited += 1
        sig, sav, nud = m.group(1), m.group(2), m.group(3)
        if sig == "Y" and sav == "N":
            if nud == "Y":
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
        f"{total_gap} of last {audited} turns had a save-signal with no brain_save call ({detail}).",
        "If 'unnudged' dominates, enable the nudge (unset BRAIN_NUDGE or set =1). "
        "If 'nudged' dominates, the model is ignoring the nudge — tighten "
        "templates/global-CLAUDE.md proactive-save triggers.",
    )]


def _check_promise_gap(brain: Path) -> list[Finding]:
    """Warn when recent activity shows save-*promises* without brain_save calls.

    This is the observability backstop behind the Stop-hook gate in
    `hooks/stop.py`. With the gate enabled (default), these should be near-zero
    — if they're not, either the gate is disabled (BRAIN_STOP_GATE=0), the
    promise regex missed a phrasing, or the model was already re-entering a
    blocked stop (stop_hook_active=true) and we deliberately bypassed the gate.

    Only examines lines written after the `pro=` column landed. Older lines
    have no `pro=` suffix and are silently skipped.
    """
    lines = _tail_activity(brain, SAVE_GAP_WINDOW)
    if not lines:
        return []

    audited = 0
    unfulfilled = 0
    for line in lines:
        m = _ACTIVITY_COLUMNS_RE.search(line)
        if not m:
            continue
        pro = m.group(4)
        if pro is None:
            continue  # old-format line, no promise column
        audited += 1
        sav = m.group(2)
        if pro == "Y" and sav == "N":
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
        f"{unfulfilled} of last {audited} audited turns promised a save but did not call brain_save/brain_checkpoint.",
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

    sessions = brain / "projects" / project / "sessions"
    if not sessions.exists():
        return []
    checkpoints = list(sessions.glob("*.md"))
    if not checkpoints:
        return []
    latest_mtime = max(p.stat().st_mtime for p in checkpoints)

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
    overview = brain / "projects" / project / "overview.md"
    if not overview.exists():
        return [Finding(
            "warn", "OVERVIEW_MISSING",
            f"No overview.md for project '{project}' — session bundle is missing project context.",
            "The SessionStart hook normally writes a stub on first run; if you see this, the hook "
            "either didn't run or couldn't write to the vault. Check hook logs on this machine.",
        )]
    try:
        from brain_mcp import vault
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
    sessions = brain / "projects" / project / "sessions"
    if not sessions.exists():
        return [Finding(
            "info", "NO_CHECKPOINTS",
            f"No checkpoints for project '{project}' yet.",
            "SessionEnd / PreCompact hooks or brain_checkpoint will create the first one.",
        )]
    checkpoints = list(sessions.glob("*.md"))
    if not checkpoints:
        return []
    newest = max(checkpoints, key=lambda p: p.stat().st_mtime)
    age_days = (datetime.now().timestamp() - newest.stat().st_mtime) / 86400
    if age_days > 30:
        return [Finding(
            "info", "STALE_CHECKPOINT",
            f"Newest checkpoint for '{project}' is {int(age_days)} days old.",
            "Checkpoint hooks may not be firing; check hook logs on this machine.",
        )]
    return [Finding("ok", "CHECKPOINT_FRESH", f"newest checkpoint for '{project}' is {int(age_days)}d old")]


def check(
    project: str | None = None,
    project_cwd: str | Path | None = None,
) -> list[dict]:
    findings: list[Finding] = []

    vault_findings = _check_brain_vault()
    findings.extend(vault_findings)
    if any(f.severity == "error" for f in vault_findings):
        return [f.to_dict() for f in findings]

    brain = Path(os.environ["BRAIN_VAULT"]).expanduser() / "Brain"
    findings.extend(_check_subdirs(brain))
    findings.extend(_check_sync_conflicts(brain))
    findings.extend(_check_vector_index(brain))
    findings.extend(_check_editable_install())
    findings.extend(_check_fastembed())
    findings.extend(_check_project_overview(brain, project))
    findings.extend(_check_stale_checkpoint(brain, project))
    findings.extend(_check_stale_uncommitted(brain, project, project_cwd))
    findings.extend(_check_save_gap(brain))
    findings.extend(_check_promise_gap(brain))

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
    visible = [f for f in findings if SEVERITY_ORDER.index(f["severity"]) >= min_idx]
    if not visible:
        return ""
    lines = ["## Brain Health", ""]
    for f in visible:
        label = f["severity"].upper()
        line = f"- **[{label}]** `{f['code']}` — {f['message']}"
        if f.get("hint"):
            line += f"  \n  *{f['hint']}*"
        lines.append(line)
    lines.append("")
    return "\n".join(lines)


def main() -> None:
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
