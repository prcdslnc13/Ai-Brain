"""brain-prep: dump the session-start bundle as a markdown system prompt.

Use this with local models that don't support tool use, or with any harness
that has no SessionStart hook to inject the bundle for it:

    brain-prep --project MyProject | ollama run gemma3
    brain-prep --project MyProject --slim --budget-kb 12   # small context window

The budget defaults to the same BRAIN_BUNDLE_BUDGET_KB (72 KB) the Claude Code
hook uses. That is deliberately generous for a 200k-token context and far too
much for a local model on 8-32k, so `--budget-kb` and `--slim` exist to size
the same bundle down rather than making the caller hand-edit the output.

This module and `render.py` are the only two allowed to put vault text in front
of a model, and every path through here fences it (ROADMAP 3F). `render()` is
the single-document form (brain-prep, the pi preload, the MCP client); the
Claude Code hooks use `render_parts()`, which splits the same bundle into
several documents each under the harness's per-hook output cap.
"""

from __future__ import annotations

import argparse
import sys

from . import vault
from ._console import force_utf8_stdio

TITLE = "# Long-term memory (loaded from Brain vault)"
CATALOGUE_HEADING = "## Saved but not loaded"
CATALOGUE_INTRO = (
    "> These memories exist in the vault but did not fit this preload. Any of them may "
    "matter to this session — run `brain recall <name>` to load one, or `brain list "
    "--type <type>` to see them all."
)
_WHY_NOTE_FULL = (
    "> Entries marked `{marker}` had their **Why:** section left out of this preload "
    "({kb} KB). The rule and its **How to apply:** are intact; recall the memory by "
    "name when you need the reasoning behind it — for an edge case, or before "
    "overriding it."
)
_WHY_NOTE_SHORT = (
    "> `{marker}` marks a **Why:** left out of the preload; recall the memory by name "
    "for the reasoning."
)


def _catalogue_lines(bundle: dict) -> list[str]:
    """One `- type/name` line per memory the bundle's byte budget skipped."""
    items = bundle.get("skipped_items") or []
    return [f"- {vault.neutralize_fence(i['name'])}" for i in items]


def _catalogue_block(entries: list[str]) -> str:
    if not entries:
        return ""
    return "\n".join([CATALOGUE_HEADING + f" ({len(entries)})", CATALOGUE_INTRO] + entries) + "\n"


def render(bundle: dict) -> str:
    """The whole bundle as one markdown document (legacy/single-output form)."""
    lines: list[str] = []
    lines.append(TITLE)
    lines.append("")
    consumed = bundle.get("budget_consumed_kb")
    limit = bundle.get("budget_limit_kb")
    if consumed is not None and limit is not None:
        skipped = bundle.get("skipped_sections") or {}
        skip_parts = [f"{n} {label}" for label, n in skipped.items() if n]
        skip_str = f" · skipped {', '.join(skip_parts)}" if skip_parts else ""
        lines.append(f"> budget: {consumed}/{limit} KB{skip_str}")
        lines.append("")
    if bundle.get("deferred_why_kb"):
        # Explained once here rather than per entry, so the marker itself stays short.
        lines.append(_WHY_NOTE_FULL.format(marker=vault._WHY_DEFERRED_MARKER,
                                           kb=bundle["deferred_why_kb"]))
        lines.append("")
    # Everything above this point is ours; everything below is vault content, so it
    # goes inside the trust fence (ROADMAP 3F). `vault.fence` defangs forged markers
    # across the whole block — section labels and paths included, since a label
    # carries a project name and a path a filename, both writer-controlled.
    body: list[str] = []
    for section in bundle.get("sections", []):
        body.append(f"## {section['label']}")
        for item in section["items"]:
            body.append(f"### {item['path']}")
            body.append(item["content"].strip())
            body.append("")
    if body:
        lines.append(bundle.get("trust_notice") or vault.TRUST_NOTICE)
        lines.append("")
        lines.append(vault.fence("\n".join(body)))
        lines.append("")
    catalogue = _catalogue_block(_catalogue_lines(bundle))
    if catalogue:
        lines.append(catalogue)
    return "\n".join(lines)


# --- Multi-part delivery ----------------------------------------------------------
#
# Claude Code caps one hook command's `additionalContext` at 10,000 chars and spills
# anything larger to a file the model never reads (see vault.PRELOAD_PARTS). So the
# hooks register PRELOAD_PARTS entries per event and each renders one part. The parts
# may arrive in any order and any subset, so every part is self-describing, carries
# its own trust notice and its own fence, and is safe to read alone. Items are never
# split across parts; an item larger than a whole part is clipped with a marker.


def _unit_text(section_label: str, item: dict) -> str:
    return f"### {item['path']}\n{item['content'].strip()}\n"


def _units(bundle: dict) -> list[tuple[str, str, str]]:
    """(section label, item path, rendered text) in bundle order."""
    out = []
    for section in bundle.get("sections", []):
        for item in section["items"]:
            out.append((section["label"], item["path"], _unit_text(section["label"], item)))
    return out


def _part_document(bundle: dict, index: int, total: int, body_units: list[tuple[str, str, str]],
                   banner: str, catalogue: str, first: bool) -> str:
    lines: list[str] = []
    if first and banner:
        lines.append(banner.rstrip("\n"))
        lines.append("")
    lines.append(f"{TITLE} — part {index} of {total}")
    lines.append("")
    if first:
        consumed = bundle.get("budget_consumed_kb")
        limit = bundle.get("budget_limit_kb")
        if consumed is not None and limit is not None:
            lines.append(f"> budget: {consumed}/{limit} KB, delivered in {total} part(s)")
            lines.append("")
    has_marker = any(vault._WHY_DEFERRED_MARKER in text for _, _, text in body_units)
    if has_marker and bundle.get("deferred_why_kb"):
        note = _WHY_NOTE_FULL if first else _WHY_NOTE_SHORT
        lines.append(note.format(marker=vault._WHY_DEFERRED_MARKER, kb=bundle["deferred_why_kb"]))
        lines.append("")
    if body_units:
        lines.append((bundle.get("trust_notice") or vault.TRUST_NOTICE) if first
                     else vault.TRUST_NOTICE_SHORT)
        lines.append("")
        body: list[str] = []
        current = None
        for label, _, text in body_units:
            if label != current:
                body.append(f"## {label}")
                current = label
            body.append(text)
        lines.append(vault.fence("\n".join(body)))
        lines.append("")
    if catalogue:
        lines.append(catalogue)
    return "\n".join(lines)


def _clip_unit(unit: tuple[str, str, str], room: int) -> tuple[str, str, str]:
    label, path, text = unit
    stem = vault.memory_display_name(path).split(" ")[0].split("/")[-1]
    clipped, _ = vault.clip_text(text, max(room, 1), f"`brain recall {stem}` for the rest")
    return label, path, clipped


def render_parts(bundle: dict, parts: int | None = None, cap_chars: int | None = None,
                 banner: str = "") -> list[str]:
    """Split the bundle into at most `parts` documents of at most `cap_chars` each.

    Part 1 carries the banner (outside the fence), the pinned sections and whatever
    elastic items follow; later parts continue in bundle order (project feedback →
    global feedback → user). Whatever does not fit in `parts` documents — and whatever
    the byte budget already skipped — is catalogued by name on the LAST part, so it
    is one recall away instead of invisible. Returns fewer than `parts` documents
    when the bundle fits in fewer; never an empty list unless the bundle is empty.
    """
    if parts is None:
        parts = vault.PRELOAD_PARTS
    if cap_chars is None:
        cap_chars = vault.hook_output_cap()
    parts = max(1, int(parts))
    cap_chars = max(200, int(cap_chars))

    units = _units(bundle)
    budget_catalogue = _catalogue_lines(bundle)
    if not units and not budget_catalogue and not banner:
        return []

    # Packing must be identical in every hook process, but only part 1's process
    # knows the real banner (it is the one that runs the doctor). So part 1 is packed
    # against a fixed-size placeholder and the real banner — clipped to that size —
    # is substituted only when the final documents are rendered. Without this, a
    # 600-char banner shrank part 1 in one process while five others packed it full,
    # and the items on the boundary were delivered by nobody.
    reserve = vault.banner_reserve_chars()
    placeholder = "#" * reserve
    real_banner = _clip_banner(banner, reserve)

    def doc(index: int, body: list, catalogue: str, first: bool, total: int = parts,
            final: bool = False) -> str:
        shown_banner = real_banner if final else placeholder
        return _part_document(bundle, index, total, body, shown_banner, catalogue, first)

    filled: list[list[tuple[str, str, str]]] = []
    remaining = list(units)
    for index in range(1, parts + 1):
        if not remaining:
            break
        first = index == 1
        body: list[tuple[str, str, str]] = []
        while remaining:
            candidate = body + [remaining[0]]
            if len(doc(index, candidate, "", first)) <= cap_chars:
                body.append(remaining.pop(0))
                continue
            if not body:
                # A single item larger than a whole part: clip it rather than
                # emit a part that is over the cap (and would spill) or empty.
                # The probe carries an empty unit so the notice, fence and section
                # header are all counted in the overhead.
                unit = remaining.pop(0)
                probe = (unit[0], unit[1], "")
                room = cap_chars - len(doc(index, [probe], "", first))
                clipped = _clip_unit(unit, room)
                while len(doc(index, [clipped], "", first)) > cap_chars and room > 100:
                    room -= 100
                    clipped = _clip_unit(unit, room)
                body.append(clipped)
            break
        filled.append(body)

    # Catalogue: whatever did not fit in `parts` parts, plus what the byte budget
    # already skipped. It lives on the last part and must itself fit there. First
    # evict trailing units from the last part into it (each eviction trades a whole
    # item for one line, so this converges fast); only when nothing is left to evict
    # is the list itself shortened, with a "... and N more" tail.
    if not filled:
        filled.append([])
    entries = [
        f"- {vault.neutralize_fence(vault.memory_display_name(path))}"
        for _, path, _ in remaining
    ] + budget_catalogue
    shown = len(entries)

    def catalogue_text() -> str:
        if not entries:
            return ""
        visible = entries[:shown]
        if shown < len(entries):
            visible.append(f"- … and {len(entries) - shown} more (`brain list` shows them all)")
        return _catalogue_block(visible)

    while True:
        last = len(filled)
        catalogue = catalogue_text()
        if len(doc(last, filled[-1], catalogue, last == 1, total=last)) <= cap_chars:
            break
        can_evict = len(filled[-1]) > 1 or (len(filled[-1]) == 1 and last > 1)
        if can_evict:
            _, path, _ = filled[-1].pop()
            entries.insert(0, f"- {vault.neutralize_fence(vault.memory_display_name(path))}")
            shown += 1
            if not filled[-1]:
                filled.pop()
            continue
        if shown > 0:
            shown -= 1
            continue
        break

    total = len(filled)
    catalogue = catalogue_text()
    docs = []
    for i, body in enumerate(filled, start=1):
        docs.append(doc(i, body, catalogue if i == total else "", i == 1, total=total,
                        final=True))
    return docs


def _clip_banner(banner: str, reserve: int) -> str:
    """The real banner, cut to the reserve it was packed against.

    A banner longer than the reserve would push part 1 over the cap and spill the
    whole part, banner included; a clipped banner still names the first findings
    and says where the rest are.
    """
    banner = (banner or "").rstrip("\n")
    if len(banner) <= reserve:
        return banner
    tail = "\n- … (more findings; run `brain doctor`)"
    return banner[: max(0, reserve - len(tail))].rstrip() + tail


def main() -> None:
    force_utf8_stdio()
    parser = argparse.ArgumentParser(description="Print the brain session-start bundle as markdown.")
    parser.add_argument("--project", help="project basename to include")
    parser.add_argument(
        "--budget-kb", type=float, metavar="KB",
        help="cap the bundle at KB kilobytes instead of BRAIN_BUNDLE_BUDGET_KB "
             "(default 72). The default is sized for a Claude-scale context; a "
             "local model on an 8-32k window needs a much smaller number -- 72 KB "
             "is roughly 18k tokens of system prompt before the conversation starts.",
    )
    parser.add_argument(
        "--slim", action="store_true",
        help="index + user + feedback only, dropping the project overview and "
             "session checkpoints (the same shape the SubagentStart hook injects). "
             "Keeps the behavioral rules, sheds the bulk.",
    )
    args = parser.parse_args()
    try:
        bundle = vault.session_start_bundle(args.project, budget_kb=args.budget_kb,
                                            slim=args.slim)
    except Exception as e:
        print(f"brain-prep error: {e}", file=sys.stderr)
        sys.exit(1)
    print(render(bundle))


if __name__ == "__main__":
    main()
