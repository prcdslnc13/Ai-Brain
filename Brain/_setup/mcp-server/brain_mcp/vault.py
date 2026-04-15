"""Vault path resolution, frontmatter parsing, and search."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
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

    def to_dict(self) -> dict:
        return {
            "path": str(self.path.relative_to(vault_root().parent)),
            "name": self.name,
            "description": self.description,
            "type": self.type,
            "body": self.body,
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
    return path


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
        if "_setup" not in p.parts and ".pending-saves" not in p.parts and not p.name.startswith("_")
    ]
    return [Memory.from_file(p) for p in sorted(set(candidates))]


def search_memories(query: str, mtype: str | None = None, project: str | None = None) -> list[Memory]:
    """Use ripgrep for content search; fall back to substring scan if rg is missing."""
    root = vault_root()
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

    matches = {p for p in matches if "_setup" not in p.parts and ".pending-saves" not in p.parts}
    candidates = [Memory.from_file(p) for p in matches]

    if mtype:
        candidates = [m for m in candidates if m.type == mtype]
    if project:
        candidates = [m for m in candidates if f"/projects/{project}/" in str(m.path)]

    candidates.sort(key=lambda m: m.path.stat().st_mtime, reverse=True)
    return candidates


def session_start_bundle(project: str | None = None) -> dict:
    """Return the standard preload bundle: index + user + feedback + project context."""
    root = vault_root()
    bundle: dict = {"loaded_at": datetime.now().isoformat(timespec="seconds"), "sections": []}

    def add(label: str, files: list[Path]):
        items = []
        for f in files:
            try:
                items.append({
                    "path": str(f.relative_to(root.parent)),
                    "content": f.read_text(encoding="utf-8"),
                })
            except Exception:
                continue
        if items:
            bundle["sections"].append({"label": label, "items": items})

    index_file = root / "_index.md"
    if index_file.exists():
        add("index", [index_file])
    add("user", sorted((root / "user").glob("*.md")) if (root / "user").exists() else [])
    add("feedback", sorted((root / "feedback").rglob("*.md")) if (root / "feedback").exists() else [])

    if project:
        proj_dir = root / "projects" / project
        if proj_dir.exists():
            overview = proj_dir / "overview.md"
            if overview.exists():
                add(f"project:{project}:overview", [overview])
            sessions_dir = proj_dir / "sessions"
            if sessions_dir.exists():
                latest = sorted(sessions_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
                if latest:
                    add(f"project:{project}:latest-session", [latest[0]])

    pending = root / ".pending-saves"
    if pending.exists():
        markers = sorted(pending.glob("*"))
        if markers:
            bundle["pending_saves"] = [m.name for m in markers]

    return bundle


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
    return p
