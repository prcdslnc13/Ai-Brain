"""Vault path resolution, frontmatter parsing, and search."""

from __future__ import annotations

import os
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


def project_basename(project_dir: str | None) -> str | None:
    if not project_dir:
        return None
    return Path(project_dir).resolve().name


@dataclass
class Memory:
    path: Path
    name: str
    description: str
    type: str
    body: str

    @classmethod
    def from_file(cls, path: Path) -> "Memory":
        text = path.read_text(encoding="utf-8")
        name = path.stem
        description = ""
        mtype = "unknown"
        body = text
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                try:
                    fm = yaml.safe_load(text[3:end]) or {}
                    name = fm.get("name", name)
                    description = fm.get("description", "")
                    mtype = fm.get("type", mtype)
                    body = text[end + 4 :].lstrip()
                except yaml.YAMLError:
                    pass
        return cls(path=path, name=name, description=description, type=mtype, body=body)

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
    else:
        target_dir = root / (mtype + "s" if mtype != "feedback" else "feedback")
        if mtype == "user":
            target_dir = root / "user"
        elif mtype == "reference":
            target_dir = root / "references"
    target_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(name)
    path = target_dir / f"{slug}.md"

    has_frontmatter = content.lstrip().startswith("---")
    if has_frontmatter:
        body = content
    else:
        description = content.strip().split("\n", 1)[0][:150]
        frontmatter = (
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"type: {mtype}\n"
            "---\n\n"
        )
        body = frontmatter + content.strip() + "\n"
    path.write_text(body, encoding="utf-8")
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
    return [Memory.from_file(p) for p in sorted(set(candidates))]


def _ripgrep_search(query: str, root: Path) -> set[Path]:
    rg = shutil.which("rg")
    matches: set[Path] = set()
    if rg:
        try:
            out = subprocess.run(
                [rg, "-l", "-i", "--type", "md", query, str(root)],
                capture_output=True, text=True, check=False,
            )
            for line in out.stdout.splitlines():
                if line.strip():
                    matches.add(Path(line.strip()))
        except Exception:
            pass
    else:
        q = query.lower()
        for p in root.rglob("*.md"):
            try:
                if q in p.read_text(encoding="utf-8").lower():
                    matches.add(p)
            except Exception:
                continue
    return {p for p in matches
            if "_setup" not in p.parts
            and ".index" not in p.parts
            and "archive" not in p.parts}


def search_memories(query: str, mtype: str | None = None, project: str | None = None) -> list[Memory]:
    """Hybrid search: vector top-K first (if available), then any extra ripgrep hits.

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
    extras = sorted(rg_hits - seen, key=lambda p: p.stat().st_mtime, reverse=True)
    for p in extras:
        ordered_paths.append(p)

    candidates = [Memory.from_file(p) for p in ordered_paths]
    if mtype:
        candidates = [m for m in candidates if m.type == mtype]
    if project:
        candidates = [m for m in candidates if f"/projects/{project}/" in str(m.path)]
    return candidates


def session_start_bundle(project: str | None = None) -> dict:
    """Return the standard preload bundle: index + user + feedback + project context.

    Honours BRAIN_BUNDLE_BUDGET_KB (default 32). The index, project overview, and latest
    session checkpoint are always included — they're small and load-bearing. User profile
    entries and feedback files are added in priority order until the budget is exhausted.
    """
    root = vault_root()
    try:
        budget_kb = float(os.environ.get("BRAIN_BUNDLE_BUDGET_KB", "32"))
    except ValueError:
        budget_kb = 32.0
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
            overview = proj_dir / "overview.md"
            if overview.exists():
                add_pinned(f"project:{project}:overview", overview)
            sessions_dir = proj_dir / "sessions"
            if sessions_dir.exists():
                latest = sorted(sessions_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
                if latest:
                    add_pinned(f"project:{project}:latest-session", latest[0])

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

    content = (
        "---\n"
        "name: overview\n"
        f"description: stub overview for {project} — awaiting upgrade on first model session\n"
        "type: project\n"
        "stub: true\n"
        f"created: {today}\n"
        "---\n\n"
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
    overview.write_text(content, encoding="utf-8")
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
    path = target / f"{stamp}.md"
    if not summary.lstrip().startswith("---"):
        summary = (
            "---\n"
            f"name: session checkpoint {stamp}\n"
            f"description: automated session checkpoint for {project}\n"
            "type: session\n"
            f"project: {project}\n"
            f"timestamp: {stamp}\n"
            "---\n\n"
        ) + summary.strip() + "\n"
    path.write_text(summary, encoding="utf-8")
    return path


EXCLUDE_DIRS = frozenset({"archive", "_setup", ".index"})


def iter_indexable_md(root: Path):
    """Yield every `.md` file under root that's an actual memory — skipping the
    machine-local index, archive rollups, and setup scaffolding. Shared by
    stats(), the embed index sync, and anything else that enumerates the vault."""
    for p in root.rglob("*.md"):
        if any(part in EXCLUDE_DIRS for part in p.relative_to(root).parts):
            continue
        yield p


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
