"""Shared recall/list payload shaping: caps, session filtering, compact rendering.

Both frontends (the MCP server and the `brain` CLI) route recall/list output
through here so every client — Claude Code, LMStudio, pi — sees identical,
*bounded* payloads. This module exists because an uncapped recall once handed a
local model 200k+ tokens in a single call: every session checkpoint for a
project mentions the project's name, so a substring search on the project
matched the entire sessions/ history, and a model-supplied large top_k +
full_body returned all of it.

Hard limits (env-overridable):

  BRAIN_RECALL_MAX_K            max results per recall regardless of top_k (10)
  BRAIN_RECALL_PREVIEW_CHARS    preview length per result body (300)
  BRAIN_RECALL_MAX_BODY_CHARS   per-file cap even with full_body=true (6000)
  BRAIN_RECALL_MAX_TOTAL_CHARS  cap on the whole rendered payload (20000)

Rendered output is compact markdown, not pretty-printed JSON — models read it
just as well and it avoids spending ~25-30% of the tokens on JSON syntax and
repeated keys.
"""

from __future__ import annotations

import os

from . import vault

DEFAULT_TOP_K = 3


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name, str(default)))
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def max_top_k() -> int:
    return _env_int("BRAIN_RECALL_MAX_K", 10, 1, 50)


def preview_chars() -> int:
    return _env_int("BRAIN_RECALL_PREVIEW_CHARS", 300, 50, 5000)


def max_body_chars() -> int:
    return _env_int("BRAIN_RECALL_MAX_BODY_CHARS", 6000, 200, 100_000)


def max_total_chars() -> int:
    return _env_int("BRAIN_RECALL_MAX_TOTAL_CHARS", 20_000, 1000, 500_000)


def _is_session_checkpoint(m: vault.Memory) -> bool:
    return "sessions" in m.path.parts or m.type == "session"


def _clip(text: str, limit: int) -> tuple[str, bool]:
    text = text.strip()
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + "…", True


def recall_payload(
    query: str,
    mtype: str | None = None,
    project: str | None = None,
    top_k: int | None = None,
    full_body: bool = False,
    include_sessions: bool = False,
) -> dict:
    """Run a capped search and return a structured payload.

    Keys: query, shown, total_matches, sessions_excluded, truncated (bool),
    results (list of {path, name, type, body, body_truncated}).
    """
    if top_k is None or top_k < 1:
        top_k = DEFAULT_TOP_K
    top_k = min(top_k, max_top_k())

    matches = vault.search_memories(query=query, mtype=mtype, project=project)
    sessions_excluded = 0
    if not include_sessions:
        kept = []
        for m in matches:
            if _is_session_checkpoint(m):
                sessions_excluded += 1
            else:
                kept.append(m)
        matches = kept

    per_body = max_body_chars() if full_body else preview_chars()
    total_budget = max_total_chars()
    consumed = 0
    results: list[dict] = []
    overflow_paths: list[str] = []

    for m in matches[:top_k]:
        body, clipped = _clip(m.body, per_body)
        rel = str(m.path.relative_to(vault.vault_root().parent))
        if consumed + len(body) > total_budget and results:
            overflow_paths.append(rel)
            continue
        consumed += len(body)
        results.append({
            "path": rel,
            "name": m.name,
            "type": m.type,
            "body": body,
            "body_truncated": clipped,
        })

    return {
        "query": query,
        "shown": len(results),
        "total_matches": len(matches),
        "sessions_excluded": sessions_excluded,
        "overflow_paths": overflow_paths,
        "full_body": full_body,
        "results": results,
    }


def render_recall(payload: dict) -> str:
    """Render a recall payload as compact markdown."""
    lines: list[str] = []
    header = f"{payload['shown']} of {payload['total_matches']} matches for \"{payload['query']}\""
    notes: list[str] = []
    if payload["sessions_excluded"]:
        notes.append(
            f"{payload['sessions_excluded']} session checkpoint(s) excluded — "
            "re-run with include_sessions to search them"
        )
    if payload["total_matches"] > payload["shown"] and not payload["overflow_paths"]:
        notes.append("raise top_k or tighten the query for more")
    if notes:
        header += f" ({'; '.join(notes)})"
    lines.append(header)

    if not payload["results"]:
        lines.append("")
        lines.append("No memories matched.")
        return "\n".join(lines)

    for r in payload["results"]:
        lines.append("")
        suffix = ""
        if r["body_truncated"]:
            suffix = (
                "  [truncated — recall with full_body for more]"
                if not payload["full_body"]
                else "  [truncated at body cap — read the file for the rest]"
            )
        lines.append(f"### {r['path']}  [{r['type']}]{suffix}")
        lines.append(r["body"])

    if payload["overflow_paths"]:
        lines.append("")
        lines.append(
            "Payload cap reached; bodies omitted for: "
            + ", ".join(payload["overflow_paths"])
        )
    return "\n".join(lines)


def list_payload(
    mtype: str | None = None,
    project: str | None = None,
    include_sessions: bool = False,
) -> dict:
    memories = vault.list_memories(mtype=mtype, project=project)
    sessions_excluded = 0
    if not include_sessions:
        kept = []
        for m in memories:
            if _is_session_checkpoint(m):
                sessions_excluded += 1
            else:
                kept.append(m)
        memories = kept
    root_parent = vault.vault_root().parent
    return {
        "count": len(memories),
        "sessions_excluded": sessions_excluded,
        "memories": [
            {
                "path": str(m.path.relative_to(root_parent)),
                "type": m.type,
                "description": m.description,
            }
            for m in memories
        ],
    }


def render_list(payload: dict) -> str:
    """Render a list payload as one markdown line per memory — no bodies."""
    lines = [f"{payload['count']} memories"]
    if payload["sessions_excluded"]:
        lines[0] += f" ({payload['sessions_excluded']} session checkpoint(s) excluded)"
    by_type: dict[str, list[dict]] = {}
    for m in payload["memories"]:
        by_type.setdefault(m["type"], []).append(m)
    for t in sorted(by_type):
        lines.append("")
        lines.append(f"## {t}")
        for m in by_type[t]:
            desc = f" — {m['description']}" if m["description"] else ""
            lines.append(f"- {m['path']}{desc}")
    return "\n".join(lines)
