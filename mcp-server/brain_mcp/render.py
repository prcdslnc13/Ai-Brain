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

It is also *fenced*: bodies and descriptions are vault content, so they go inside
`vault.fence()` behind a notice naming them as data (ROADMAP 3F). This module and
`brain_prep` are the only two that render vault text to a model, which is what makes
one trust convention reachable — keep it that way.
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
    return vault.is_session_path(m.path) or m.type == "session"


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
        body, clipped = _clip(vault.neutralize_fence(m.body), per_body)
        rel = str(m.path.relative_to(vault.vault_root().parent))
        if consumed + len(body) > total_budget and results:
            overflow_paths.append(rel)
            continue
        consumed += len(body)
        results.append({
            "path": rel,
            "name": m.name,
            "type": m.type,
            "machine": m.machine,
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

    # Recall is the other door onto the same content the preload carries, so it
    # gets the same fence (ROADMAP 3F) — the short notice, because unlike the
    # preload this is paid per call. See vault.fence.
    lines.append("")
    lines.append(vault.TRUST_NOTICE_SHORT)

    bodies: list[str] = []
    for r in payload["results"]:
        bodies.append("")
        suffix = ""
        if r["body_truncated"]:
            suffix = (
                "  [truncated — recall with full_body for more]"
                if not payload["full_body"]
                else "  [truncated at body cap — read the file for the rest]"
            )
        tag = r["type"]
        if r.get("machine"):
            tag += f" @ {r['machine']}"
        bodies.append(f"### {r['path']}  [{tag}]{suffix}")
        bodies.append(r["body"])

    lines.append(vault.fence("\n".join(bodies)))

    if payload["overflow_paths"]:
        lines.append("")
        lines.append(
            "Payload cap reached; bodies omitted for: "
            + ", ".join(payload["overflow_paths"])
        )
    return "\n".join(lines)


def max_list_items() -> int:
    return _env_int("BRAIN_LIST_MAX_ITEMS", 300, 10, 5000)


def list_description_chars() -> int:
    return _env_int("BRAIN_LIST_DESC_CHARS", 100, 40, 1000)


def max_list_total_chars() -> int:
    return _env_int("BRAIN_LIST_MAX_TOTAL_CHARS", 24_000, 1000, 500_000)


def _round_robin_by_type(memories: list) -> list:
    """Order memories so truncation takes evenly from every type.

    `list_memories` returns path order, which on this vault means `feedback/`,
    `projects/`, `references/`, `user/`. Truncating that order in place dropped
    *every* user and reference memory while the 224-entry project bucket was still
    being emitted — losing a whole category is much worse than losing a slice of
    each, especially as user and feedback are the two the model's behaviour depends
    on. Within a type the original order is preserved.
    """
    groups: dict[str, list] = {}
    for m in memories:
        groups.setdefault(m.type, []).append(m)
    ordered: list = []
    index = 0
    while len(ordered) < len(memories):
        added = False
        for bucket in groups.values():
            if index < len(bucket):
                ordered.append(bucket[index])
                added = True
        if not added:
            break
        index += 1
    return ordered


def list_payload(
    mtype: str | None = None,
    project: str | None = None,
    include_sessions: bool = False,
) -> dict:
    """Enumerate memories, bounded the way recall is.

    `list` was the one path through this module with no cap at all, which quietly
    reintroduced the failure the module exists to prevent. Measured 2026-08-24
    against a 917-file vault: a default `brain list` rendered 57 KB (~14k tokens) and
    `--include-sessions` rendered 140 KB (~35k tokens) — and that second number grows
    by one checkpoint per session, forever. The 2026-07-11 blowup that motivated
    `render.py` was the same shape, just through `recall`.

    Truncating an enumeration is genuinely lossy in a way truncating a preview is
    not, so the cap is generous and the omission is always reported with the filters
    that would narrow it.
    """
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
    item_cap = max_list_items()
    total_budget = max_list_total_chars()
    desc_cap = list_description_chars()

    entries: list[dict] = []
    consumed = 0
    for m in _round_robin_by_type(memories):
        if len(entries) >= item_cap:
            break
        desc, _ = _clip(vault.neutralize_fence(m.description), desc_cap)
        rel = str(m.path.relative_to(root_parent))
        # Char budget is the backstop for a corpus of few but enormous descriptions;
        # `and entries` keeps a pathological first item from yielding an empty list.
        if consumed + len(rel) + len(desc) > total_budget and entries:
            break
        consumed += len(rel) + len(desc)
        entries.append({
            "path": rel,
            "type": m.type,
            "machine": m.machine,
            "description": desc,
        })

    return {
        "count": len(entries),
        "total_matches": len(memories),
        "omitted": len(memories) - len(entries),
        "sessions_excluded": sessions_excluded,
        "memories": entries,
    }


def render_list(payload: dict) -> str:
    """Render a list payload as one markdown line per memory — no bodies."""
    total = payload.get("total_matches", payload["count"])
    header = (
        f"{payload['count']} memories"
        if payload["count"] == total
        else f"{payload['count']} of {total} memories"
    )
    notes: list[str] = []
    if payload["sessions_excluded"]:
        notes.append(f"{payload['sessions_excluded']} session checkpoint(s) excluded")
    if payload.get("omitted"):
        notes.append(
            f"{payload['omitted']} omitted at the payload cap — narrow with "
            "--type/--project, or raise BRAIN_LIST_MAX_ITEMS"
        )
    lines = [header + (f" ({'; '.join(notes)})" if notes else "")]
    by_type: dict[str, list[dict]] = {}
    for m in payload["memories"]:
        by_type.setdefault(m["type"], []).append(m)
    # No bodies here, but a description is still writer-controlled text arriving in
    # the model's context — same fence, so the convention has no exceptions.
    body: list[str] = []
    for t in sorted(by_type):
        body.append("")
        body.append(f"## {t}")
        for m in by_type[t]:
            desc = f" — {m['description']}" if m["description"] else ""
            machine = f" [{m['machine']}]" if m.get("machine") else ""
            body.append(f"- {m['path']}{machine}{desc}")
    if body:
        lines.append("")
        lines.append(vault.TRUST_NOTICE_SHORT)
        lines.append(vault.fence("\n".join(body)))
    return "\n".join(lines)
