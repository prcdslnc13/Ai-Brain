"""brain-prep: dump the session-start bundle as a markdown system prompt.

Use this with local models that don't support tool use, or with any harness
that has no SessionStart hook to inject the bundle for it:

    brain-prep --project MyProject | ollama run gemma3
    brain-prep --project MyProject --slim --budget-kb 12   # small context window

The budget defaults to the same BRAIN_BUNDLE_BUDGET_KB (72 KB) the Claude Code
hook uses. That is deliberately generous for a 200k-token context and far too
much for a local model on 8-32k, so `--budget-kb` and `--slim` exist to size
the same bundle down rather than making the caller hand-edit the output.
"""

from __future__ import annotations

import argparse
import sys

from . import vault
from ._console import force_utf8_stdio


def render(bundle: dict) -> str:
    lines: list[str] = []
    lines.append("# Long-term memory (loaded from Brain vault)")
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
        lines.append(
            f"> Entries marked `{vault._WHY_DEFERRED_MARKER}` had their **Why:** section "
            f"left out of this preload ({bundle['deferred_why_kb']} KB). The rule and its "
            f"**How to apply:** are intact; recall the memory by name when you need the "
            f"reasoning behind it — for an edge case, or before overriding it."
        )
        lines.append("")
    for section in bundle.get("sections", []):
        lines.append(f"## {section['label']}")
        for item in section["items"]:
            lines.append(f"### {item['path']}")
            lines.append(item["content"].strip())
            lines.append("")
    return "\n".join(lines)


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
