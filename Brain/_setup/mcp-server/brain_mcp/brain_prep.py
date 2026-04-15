"""brain-prep: dump the session-start bundle as a markdown system prompt.

Use this with local models that don't support tool use:

    brain-prep --project MyProject | ollama run gemma3
"""

from __future__ import annotations

import argparse
import sys

from . import vault


def render(bundle: dict) -> str:
    lines: list[str] = []
    lines.append("# Long-term memory (loaded from Brain vault)")
    lines.append("")
    if bundle.get("pending_saves"):
        lines.append(f"⚠ pending save markers: {', '.join(bundle['pending_saves'])}")
        lines.append("")
    for section in bundle.get("sections", []):
        lines.append(f"## {section['label']}")
        for item in section["items"]:
            lines.append(f"### {item['path']}")
            lines.append(item["content"].strip())
            lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Print the brain session-start bundle as markdown.")
    parser.add_argument("--project", help="project basename to include")
    args = parser.parse_args()
    try:
        bundle = vault.session_start_bundle(args.project)
    except Exception as e:
        print(f"brain-prep error: {e}", file=sys.stderr)
        sys.exit(1)
    print(render(bundle))


if __name__ == "__main__":
    main()
