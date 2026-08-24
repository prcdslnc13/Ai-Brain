"""Vault path resolution, frontmatter parsing, and search."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

VALID_TYPES = {"user", "feedback", "project", "reference"}


def vault_root() -> Path:
    """Return the Brain/ directory inside BRAIN_VAULT."""
    raw = os.environ.get("BRAIN_VAULT")
    if not raw:
        raise RuntimeError(
            "BRAIN_VAULT environment variable is not set. "
            "Point it at the Obsidian vault root (the folder containing the Brain/ directory)."
        )
    root = Path(raw).expanduser().resolve() / "Brain"
    if not root.exists():
        raise RuntimeError(f"Brain directory does not exist: {root}")
    return root


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "untitled"


_machine_name_cache: str | None = None


def machine_name() -> str:
    """Short identifier for the machine this process runs on.

    Stamped into every memory's frontmatter and every checkpoint filename so
    the user — who works across several machines — can tell where a piece of
    work happened when it never got committed or finished. `BRAIN_MACHINE`
    overrides for hosts whose name is unhelpful (corp asset tags). On macOS the
    Bonjour LocalHostName (`Joes-MacBook-Pro-3`) is preferred because the plain
    hostname is often a generic `Mac.localdomain`; elsewhere it's the hostname
    with any domain suffix stripped."""
    global _machine_name_cache
    if _machine_name_cache is not None:
        return _machine_name_cache
    raw = (os.environ.get("BRAIN_MACHINE") or "").strip()
    if not raw and sys.platform == "darwin":
        try:
            raw = subprocess.run(
                ["scutil", "--get", "LocalHostName"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            raw = ""
    if not raw:
        raw = (platform.node() or "").strip()
    host = raw.split(".", 1)[0]
    _machine_name_cache = slugify(host) if host else "unknown-host"
    return _machine_name_cache


def project_basename(project_dir: str | None) -> str | None:
    if not project_dir:
        return None
    return Path(project_dir).resolve().name


def path_in_project(path: Path, project: str) -> bool:
    """True when `path` lives under a `projects/<project>/` directory.

    Compares path *components*, never a substring like `/projects/X/` — that
    form silently never matches on Windows, where str(Path) uses backslashes
    (2026-07-28: `recall --project` returned nothing on Windows for notes that
    were right there)."""
    parts = path.parts
    for i in range(len(parts) - 1):
        if parts[i] == "projects" and parts[i + 1] == project:
            return True
    return False


def _frontmatter(fields: dict) -> str:
    """Render a frontmatter block via the YAML dumper, never f-strings.

    An interpolated title containing a colon (`name: F1 job path: .xf is a
    tar`) is invalid YAML — the whole frontmatter fails to parse and the note
    silently loses its type, dropping out of every filtered recall
    (2026-07-28 Windows incident: four notes affected)."""
    dumped = yaml.safe_dump(
        fields,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=10_000,
    )
    return f"---\n{dumped}---\n\n"


def _atomic_write(path: Path, text: str) -> None:
    """Write via a same-directory temp file + os.replace so a mid-write
    failure can never truncate an existing memory (2026-07-28: a
    UnicodeEncodeError on Windows emptied three notes during overwrite).
    The temp name doesn't end in .md, so vault globs, the embed index, and
    Obsidian Sync never see it."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


@dataclass
class Memory:
    path: Path
    name: str
    description: str
    type: str
    body: str
    machine: str = ""

    @classmethod
    def from_file(cls, path: Path) -> "Memory":
        text = path.read_text(encoding="utf-8")
        name = path.stem
        description = ""
        mtype = "unknown"
        machine = ""
        body = text
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                try:
                    fm = yaml.safe_load(text[3:end]) or {}
                    name = fm.get("name", name)
                    description = fm.get("description", "")
                    mtype = fm.get("type", mtype)
                    machine = str(fm.get("machine") or "")
                    body = text[end + 4 :].lstrip()
                except yaml.YAMLError:
                    pass
        return cls(path=path, name=name, description=description, type=mtype,
                   body=body, machine=machine)

    def to_dict(self, body_chars: int | None = None) -> dict:
        """Serialize. When body_chars is set, truncate the body to that many chars
        with a "…" suffix when truncated. None = full body (the default for save
        paths where the caller wants the whole thing)."""
        if body_chars is None or len(self.body) <= body_chars:
            body = self.body
        else:
            body = self.body[:body_chars].rstrip() + "…"
        return {
            "path": str(self.path.relative_to(vault_root().parent)),
            "name": self.name,
            "description": self.description,
            "type": self.type,
            "machine": self.machine,
            "body": body,
        }


def write_memory(mtype: str, name: str, content: str, project: str | None = None) -> Path:
    if mtype not in VALID_TYPES:
        raise ValueError(f"type must be one of {sorted(VALID_TYPES)}, got {mtype!r}")
    root = vault_root()
    if mtype == "project":
        if not project:
            raise ValueError("project memories require a project name")
        target_dir = root / "projects" / project
    elif mtype == "feedback":
        # Global by default. `--project X` scopes the rule to one project: it lands in
        # projects/X/feedback/ and preloads only in that project's sessions. Added
        # 2026-08-06, when ~40% of the global feedback corpus turned out to be
        # project-specific advice loading into every session of every project.
        target_dir = (root / "projects" / project / "feedback") if project else (root / "feedback")
    else:
        target_dir = root / ("user" if mtype == "user" else "references")
    target_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(name)
    path = target_dir / f"{slug}.md"

    has_frontmatter = content.lstrip().startswith("---")
    if has_frontmatter:
        body = content
    else:
        description = content.strip().split("\n", 1)[0][:150]
        fields = {"name": name, "description": description, "type": mtype,
                  "machine": machine_name()}
        if project and mtype in ("project", "feedback"):
            fields["project"] = project
        body = _frontmatter(fields) + content.strip() + "\n"
    _atomic_write(path, body)
    _try_embed_upsert(path)
    return path


def _try_embed_upsert(path: Path) -> None:
    if os.environ.get("BRAIN_EMBED", "1") == "0":
        return
    try:
        from . import embed as _embed
        _embed.EmbedIndex.upsert(path)
    except Exception as e:
        print(f"brain embed upsert skipped: {e}", file=sys.stderr)


def _try_embed_delete(path: Path) -> None:
    if os.environ.get("BRAIN_EMBED", "1") == "0":
        return
    try:
        from . import embed as _embed
        _embed.EmbedIndex.delete(path)
    except Exception as e:
        print(f"brain embed delete skipped: {e}", file=sys.stderr)


def list_memories(mtype: str | None = None, project: str | None = None) -> list[Memory]:
    root = vault_root()
    candidates: list[Path] = []
    if mtype is None:
        candidates += list(root.rglob("*.md"))
    elif mtype == "user":
        candidates += list((root / "user").rglob("*.md"))
    elif mtype == "feedback":
        candidates += list((root / "feedback").rglob("*.md"))
        # Project-scoped feedback lives under projects/<p>/feedback/ (added 2026-08-06).
        proj_root = root / "projects"
        if proj_root.exists():
            candidates += list(proj_root.glob("*/feedback/**/*.md"))
    elif mtype == "reference":
        candidates += list((root / "references").rglob("*.md"))
    elif mtype == "project":
        proj_root = root / "projects"
        if project:
            proj_root = proj_root / project
        if proj_root.exists():
            candidates += list(proj_root.rglob("*.md"))
    candidates = [
        p for p in candidates
        if "_setup" not in p.parts and not p.name.startswith("_")
    ]
    if project:
        candidates = [p for p in candidates if path_in_project(p, project)]
    return [Memory.from_file(p) for p in sorted(set(candidates))]


def _ripgrep_search(query: str, root: Path) -> dict[Path, int]:
    """Literal (case-insensitive) matches -> occurrence count.

    Counts, not just paths: they are the only relevance signal a lexical-only hit
    has, and ordering those hits by recency alone put "most recently touched file
    that mentions the word once" ahead of "file that is largely about the word".
    """
    rg = shutil.which("rg")
    matches: dict[Path, int] = {}
    if rg:
        try:
            out = subprocess.run(
                [rg, "-c", "-i", "--type", "md", query, str(root)],
                capture_output=True, text=True, check=False,
            )
            for line in out.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                # "path:count" — rsplit, because Windows paths contain colons.
                path, _, count = line.rpartition(":")
                if not path:
                    continue
                try:
                    matches[Path(path)] = int(count)
                except ValueError:
                    matches[Path(path)] = 1
        except Exception:
            pass
    else:
        q = query.lower()
        for p in root.rglob("*.md"):
            try:
                n = p.read_text(encoding="utf-8").lower().count(q)
            except Exception:
                continue
            if n:
                matches[p] = n
    return {p: n for p, n in matches.items()
            if not any(part in EXCLUDE_DIRS for part in p.parts)
            and p.name not in EXCLUDE_FILES}


# How often a lexical-only hit gets a slot in the merged ranking: every Nth
# position, 1-based. 3 is deliberate — DEFAULT_TOP_K is 3, so the best file that
# matches the query literally but carries no vector is always visible in a default
# recall, while positions 1 and 2 stay pure vector ranking.
LEXICAL_SLOT_EVERY = 3

# Only this many lexical-only hits are woven into the head; the rest keep the old
# append-at-the-end behavior. Without the cap, a broad query (a project name appears
# in every checkpoint for that project) would hand a third of the ranking to
# whatever merely mentions the word — the 2026-07-11 blowup, re-entered through the
# front door.
LEXICAL_MERGE_CAP = 20


def _merge_lexical(vector_hits: list[Path], lexical_hits: list[Path]) -> list[Path]:
    """Weave lexical-only hits into the vector ranking, one every LEXICAL_SLOT_EVERY.

    A file with no vector used to sort below *every* vector hit, which made it
    unreachable rather than merely lower-ranked: measured 2026-08-24, a query whose
    literal text appeared in exactly one un-vectorized file ranked it 21st of 21.
    Reserving a slot bounds that at "never worse than position 3".
    """
    out: list[Path] = []
    vi = li = 0
    while vi < len(vector_hits) or li < len(lexical_hits):
        want_lexical = (len(out) + 1) % LEXICAL_SLOT_EVERY == 0
        if li < len(lexical_hits) and (want_lexical or vi >= len(vector_hits)):
            out.append(lexical_hits[li])
            li += 1
        elif vi < len(vector_hits):
            out.append(vector_hits[vi])
            vi += 1
        else:
            out.append(lexical_hits[li])
            li += 1
    return out


def search_memories(query: str, mtype: str | None = None, project: str | None = None) -> list[Memory]:
    """Hybrid search: vector top-K and literal ripgrep hits, merged.

    Vector hits carry the ranking; lexical-only hits (files ripgrep matched that have
    no vector — aged-out checkpoints, or anything the index hasn't reached yet) are
    woven in every LEXICAL_SLOT_EVERY slots so an exact match can't be buried.

    Disabled by setting BRAIN_EMBED=0. On any embed failure (missing dep, model load,
    sqlite error) falls back transparently to ripgrep substring search.
    """
    root = vault_root()
    use_embed = os.environ.get("BRAIN_EMBED", "1") != "0"

    ordered_paths: list[Path] = []
    seen: set[Path] = set()

    if use_embed:
        try:
            from . import embed as _embed
            _embed.EmbedIndex.sync()
            hits = _embed.EmbedIndex.query(
                query, top_k=20, type_filter=mtype, project_filter=project,
            )
            for path_str, _score in hits:
                p = Path(path_str)
                if p in seen:
                    continue
                if not p.exists():
                    continue
                if any(part in {"_setup", ".index", "archive"}
                       for part in p.parts):
                    continue
                ordered_paths.append(p)
                seen.add(p)
        except Exception as e:
            print(f"brain embed unavailable, falling back to ripgrep: {e}", file=sys.stderr)

    rg_hits = _ripgrep_search(query, root)
    extras = sorted(
        (p for p in rg_hits if p not in seen),
        key=lambda p: (-rg_hits[p], -p.stat().st_mtime),
    )
    # Only the head participates in the merge; the tail still just appends, so the
    # total match count a caller sees is unchanged.
    ordered_paths = _merge_lexical(ordered_paths, extras[:LEXICAL_MERGE_CAP])
    ordered_paths.extend(extras[LEXICAL_MERGE_CAP:])

    candidates = [Memory.from_file(p) for p in ordered_paths]
    if mtype:
        candidates = [m for m in candidates if m.type == mtype]
    if project:
        candidates = [m for m in candidates if path_in_project(m.path, project)]
    return candidates


# Default byte budget for the slim SubagentStart bundle. Lives here (not in the hook)
# so doctor can size the bundle with the same number the hook actually uses — on
# 2026-08-06 the subagent path had silently re-saturated while doctor reported OK,
# because doctor only checked the session budget. 44 KB was sized to "fit everything"
# on 2026-07-30 and the corpus outgrew it within a week; 56 KB is the same stopgap
# with headroom, and SUBAGENT_BUNDLE_SATURATED now fires when it runs out. The real
# fix is relevance-scoping the preload, not raising this number forever.
SUBAGENT_BUDGET_DEFAULT_KB = 56.0


def subagent_budget_kb() -> float:
    try:
        return float(os.environ.get("BRAIN_SUBAGENT_BUDGET_KB", str(SUBAGENT_BUDGET_DEFAULT_KB)))
    except ValueError:
        return SUBAGENT_BUDGET_DEFAULT_KB


def session_start_bundle(project: str | None = None, budget_kb: float | None = None,
                         slim: bool = False) -> dict:
    """Return the standard preload bundle: index + user + feedback + project context.

    Honours BRAIN_BUNDLE_BUDGET_KB (default 72) unless the caller passes an explicit
    `budget_kb` (the SubagentStart hook and doctor's subagent-sized check do, so the
    subagent budget never has to be smuggled through the session env var).

    `slim=True` is the subagent shape: it skips the project overview and latest
    checkpoint (the delegating agent passes task context in the subagent prompt) but
    still loads the project's scoped feedback — behavioral rules apply to delegated
    work just as much as to the main session.

    Elastic sections fill in priority order — project-scoped feedback, then user,
    then global feedback — so under a tight budget, global feedback is what gets
    dropped first. The index,
    project overview, and latest
    session checkpoint are always included — they're small and load-bearing. User profile
    entries and feedback files are added in priority order until the budget is exhausted.

    The default was 32 KB until 2026-07-30, by which point the corpus had outgrown it and
    18 of 22 feedback memories were being dropped from every preload — saved correctly but
    never loaded, so their rules silently stopped applying. `brain doctor` now reports
    BUNDLE_SATURATED when that happens again; raising this default only buys headroom.
    """
    root = vault_root()
    if budget_kb is None:
        try:
            budget_kb = float(os.environ.get("BRAIN_BUNDLE_BUDGET_KB", "72"))
        except ValueError:
            budget_kb = 72.0
    budget_bytes = int(budget_kb * 1024)

    bundle: dict = {
        "loaded_at": datetime.now().isoformat(timespec="seconds"),
        "sections": [],
        "budget_limit_kb": round(budget_kb, 2),
    }

    sections_by_label: dict[str, dict] = {}
    consumed_bytes = 0
    skipped_counts: dict[str, int] = {}

    def add_pinned(label: str, file: Path) -> None:
        nonlocal consumed_bytes
        try:
            content = file.read_text(encoding="utf-8")
        except Exception:
            return
        rel = str(file.relative_to(root.parent))
        item = {"path": rel, "content": content}
        section = sections_by_label.get(label)
        if section is None:
            section = {"label": label, "items": []}
            sections_by_label[label] = section
            bundle["sections"].append(section)
        section["items"].append(item)
        consumed_bytes += len(content.encode("utf-8"))

    def add_elastic(label: str, files: list[Path]) -> None:
        nonlocal consumed_bytes
        for f in files:
            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                continue
            size = len(content.encode("utf-8"))
            if consumed_bytes + size > budget_bytes and consumed_bytes > 0:
                skipped_counts[label] = skipped_counts.get(label, 0) + 1
                continue
            rel = str(f.relative_to(root.parent))
            item = {"path": rel, "content": content}
            section = sections_by_label.get(label)
            if section is None:
                section = {"label": label, "items": []}
                sections_by_label[label] = section
                bundle["sections"].append(section)
            section["items"].append(item)
            consumed_bytes += size

    index_file = root / "_index.md"
    if index_file.exists():
        add_pinned("index", index_file)

    if project:
        proj_dir = root / "projects" / project
        if proj_dir.exists():
            if not slim:
                overview = proj_dir / "overview.md"
                if overview.exists():
                    add_pinned(f"project:{project}:overview", overview)
                sessions_dir = proj_dir / "sessions"
                if sessions_dir.exists():
                    latest = sorted(sessions_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
                    if latest:
                        add_pinned(f"project:{project}:latest-session", latest[0])
            proj_feedback = proj_dir / "feedback"
            if proj_feedback.exists():
                add_elastic(
                    f"project:{project}:feedback",
                    sorted(proj_feedback.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True),
                )

    user_dir = root / "user"
    if user_dir.exists():
        add_elastic("user", sorted(user_dir.glob("*.md")))

    feedback_dir = root / "feedback"
    if feedback_dir.exists():
        feedback_files = sorted(
            feedback_dir.rglob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        add_elastic("feedback", feedback_files)

    bundle["budget_consumed_kb"] = round(consumed_bytes / 1024.0, 2)
    bundle["skipped_sections"] = skipped_counts
    return bundle


OVERVIEW_SOURCE_CANDIDATES = ("CLAUDE.md", "plan.md", "ROADMAP.md", "README.md")


def ensure_project_overview_stub(project: str, project_dir: str | Path | None) -> Path | None:
    """Write a minimal stub `projects/<project>/overview.md` if none exists.

    The stub has `stub: true` in frontmatter so the model (via the SessionStart
    bundle) and `brain_doctor` can tell it apart from a real, synthesized
    overview. On first model session in this project, the template directive in
    global-CLAUDE.md tells the model to read the listed source files and call
    `brain_save` to replace the stub with a real summary.

    Idempotent: returns None if overview.md already exists, or if `project` is
    falsy. Returns the path that was written otherwise.
    """
    if not project:
        return None
    root = vault_root()
    overview = root / "projects" / project / "overview.md"
    if overview.exists():
        return None

    pointers: list[str] = []
    if project_dir:
        p = Path(project_dir).expanduser().resolve()
        for name in OVERVIEW_SOURCE_CANDIDATES:
            candidate = p / name
            if candidate.exists():
                pointers.append(f"- `{candidate}`")

    today = datetime.now().date().isoformat()
    if pointers:
        pointers_block = "\n".join(pointers)
    else:
        pointers_block = (
            "- _(no CLAUDE.md / plan.md / ROADMAP.md / README.md found at the project root "
            "— synthesize the overview from code exploration instead)_"
        )

    content = _frontmatter({
        "name": "overview",
        "description": f"stub overview for {project} — awaiting upgrade on first model session",
        "type": "project",
        "project": project,
        "stub": True,
        "created": today,
    }) + (
        f"# {project} — overview (STUB)\n\n"
        "> This is an auto-generated placeholder written by the SessionStart hook so the session\n"
        "> bundle has *something* for project context. **Action for the model that loads this:**\n"
        "> read the source files listed below, synthesize a concise summary of purpose,\n"
        "> architecture, and non-obvious gotchas, and call\n"
        f"> `brain_save(type=\"project\", project=\"{project}\", name=\"overview\", content=...)`\n"
        "> to replace this stub. Future sessions will then see your real overview.\n\n"
        "## Source material\n\n"
        f"{pointers_block}\n"
    )

    overview.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(overview, content)
    _try_embed_upsert(overview)
    return overview


def is_overview_stub(path: Path) -> bool:
    """True when `path` has `stub: true` in its YAML frontmatter."""
    try:
        with path.open("r", encoding="utf-8") as f:
            head = f.read(2048)
    except OSError:
        return False
    if not head.startswith("---"):
        return False
    end = head.find("\n---", 3)
    if end == -1:
        return False
    try:
        fm = yaml.safe_load(head[3:end]) or {}
    except yaml.YAMLError:
        return False
    return bool(fm.get("stub"))


def write_checkpoint(project: str, summary: str) -> Path:
    root = vault_root()
    target = root / "projects" / project / "sessions"
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    # The machine suffix in the filename is deliberate: it's how the user spots
    # where unfinished/uncommitted work lives when scanning sessions/ (they hop
    # between machines). Everything that consumes these files picks them by
    # mtime, never by parsing the name, so the suffix is safe to carry.
    machine = machine_name()
    path = target / f"{stamp}-{machine}.md"
    if not summary.lstrip().startswith("---"):
        summary = _frontmatter({
            "name": f"session checkpoint {stamp} ({machine})",
            "description": f"automated session checkpoint for {project} on {machine}",
            "type": "session",
            "project": project,
            "timestamp": stamp,
            "machine": machine,
        }) + summary.strip() + "\n"
    _atomic_write(path, summary)
    return path


EXCLUDE_DIRS = frozenset({"archive", "_setup", ".index"})
# Bookkeeping files that live at the Brain/ root and are not memories: the Stop-hook
# audit log and the vault's table of contents. They were being embedded *and* returned
# as recall hits — `activity.md` surfaced as the #3 result for "windows setup" once
# lexical hits stopped sorting last (2026-08-24).
EXCLUDE_FILES = frozenset({"activity.md", "_index.md"})


def iter_indexable_md(root: Path):
    """Yield every `.md` file under root that's an actual memory — skipping the
    machine-local index, archive rollups, and setup scaffolding. Shared by
    stats(), the embed index sync, and anything else that enumerates the vault."""
    for p in root.rglob("*.md"):
        if any(part in EXCLUDE_DIRS for part in p.relative_to(root).parts):
            continue
        if p.name in EXCLUDE_FILES:
            continue
        yield p


def is_session_path(path: Path) -> bool:
    """Path-only test for a session checkpoint.

    Path-based rather than frontmatter-based so callers that only hold a path
    (the embed index) classify a file identically to callers that hold a parsed
    Memory (render's recall filter) without paying a file read.
    """
    return "sessions" in Path(path).parts


def read_frontmatter_type(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            head = f.read(2048)
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    end = head.find("\n---", 3)
    if end == -1:
        return None
    try:
        fm = yaml.safe_load(head[3:end]) or {}
    except yaml.YAMLError:
        return None
    val = fm.get("type")
    return val if isinstance(val, str) else None


def stats() -> dict:
    """Vault telemetry: counts, index size, oldest active checkpoint."""
    root = vault_root()

    total = 0
    by_type: dict[str, int] = {"user": 0, "feedback": 0, "project": 0, "reference": 0}
    for p in iter_indexable_md(root):
        total += 1
        t = read_frontmatter_type(p)
        if t in by_type:
            by_type[t] += 1

    oldest_checkpoint: str | None = None
    earliest_mtime: float | None = None
    sessions_glob = list((root / "projects").glob("*/sessions/*.md")) if (root / "projects").exists() else []
    for p in sessions_glob:
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if earliest_mtime is None or m < earliest_mtime:
            earliest_mtime = m
            oldest_checkpoint = datetime.fromtimestamp(m).date().isoformat()

    index_path = root / ".index" / "embeddings.sqlite"
    index_size_mb: float | None
    try:
        index_size_mb = round(index_path.stat().st_size / 1e6, 3) if index_path.exists() else None
    except OSError:
        index_size_mb = None

    archive_root = root / "archive"
    archive_size_mb: float | None
    if archive_root.exists():
        total_bytes = 0
        for f in archive_root.rglob("*"):
            try:
                if f.is_file():
                    total_bytes += f.stat().st_size
            except OSError:
                continue
        archive_size_mb = round(total_bytes / 1e6, 3)
    else:
        archive_size_mb = None

    return {
        "total_items": total,
        "by_type": by_type,
        "oldest_active_checkpoint": oldest_checkpoint,
        "index_size_mb": index_size_mb,
        "archive_size_mb": archive_size_mb,
    }


def forget_memory(rel_or_abs_path: str) -> Path:
    root = vault_root()
    p = Path(rel_or_abs_path)
    if not p.is_absolute():
        candidates = [root / p, root.parent / p]
        for c in candidates:
            if c.exists():
                p = c
                break
    if not p.exists():
        raise FileNotFoundError(f"memory not found: {rel_or_abs_path}")
    if root not in p.resolve().parents and p.resolve() != root:
        raise PermissionError(f"refusing to delete outside the Brain dir: {p}")
    p.unlink()
    _try_embed_delete(p)
    return p
