"""Shared checkpoint logic for PreCompact and SessionEnd hooks.

Reads a Claude Code transcript JSONL and writes a structured checkpoint markdown file
into the vault under Brain/projects/<project>/sessions/. No LLM call is made — the
checkpoint is a structural extract (user messages, tool calls summary, assistant final
text). The next session-start preload will surface the most recent checkpoint, so the
NEXT model gets to summarize/integrate it as needed.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# brain_mcp is installed in mcp-server/.venv (non-editable) — the hook command launches us
# via that interpreter, so this import works without sys.path tricks. BRAIN_VAULT must be
# set in env by the hook command.
from brain_mcp import vault as _vault  # noqa: E402


_COMMAND_TAG_PREFIXES = ("<local-command-", "<command-")


def _is_command_wrapper(text: str) -> bool:
    """Claude Code wraps slash-command input/output in synthetic XML-ish tags that
    arrive as 'user' role entries in the transcript. A turn whose content is
    entirely these wrappers is not a real user turn."""
    stripped = text.strip()
    return bool(stripped) and stripped.startswith(_COMMAND_TAG_PREFIXES)


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text" and c.get("text"):
                    parts.append(c["text"])
                elif c.get("type") == "tool_use":
                    parts.append(f"[tool_use: {c.get('name', '?')}]")
                elif c.get("type") == "tool_result":
                    parts.append("[tool_result]")
        return " ".join(parts)
    return ""


def parse_transcript(path: Path) -> dict:
    user_msgs: list[str] = []
    assistant_msgs: list[str] = []
    tool_calls: list[str] = []

    if not path.exists():
        return {"user_msgs": [], "assistant_msgs": [], "tool_calls": []}

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg = obj.get("message") or obj
            role = obj.get("type") or msg.get("role") or obj.get("role")

            if role == "user":
                text = _extract_text(msg.get("content") if isinstance(msg, dict) else msg)
                if text and not text.startswith("[tool_result") and not _is_command_wrapper(text):
                    user_msgs.append(text.strip())
            elif role == "assistant":
                content = msg.get("content") if isinstance(msg, dict) else msg
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict):
                            if c.get("type") == "text" and c.get("text"):
                                assistant_msgs.append(c["text"].strip())
                            elif c.get("type") == "tool_use":
                                tool_calls.append(c.get("name", "?"))
                elif isinstance(content, str):
                    assistant_msgs.append(content.strip())

    return {
        "user_msgs": user_msgs,
        "assistant_msgs": assistant_msgs,
        "tool_calls": tool_calls,
    }


def render_checkpoint(parsed: dict, *, source: str, project: str | None) -> str:
    user_msgs = parsed["user_msgs"]
    assistant_msgs = parsed["assistant_msgs"]
    tool_calls = parsed["tool_calls"]

    from collections import Counter
    tool_counts = Counter(tool_calls)

    lines: list[str] = []
    lines.append(f"# Session checkpoint ({source})")
    lines.append("")
    lines.append(f"- project: {project or 'unknown'}")
    lines.append(f"- captured: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"- user turns: {len(user_msgs)}")
    lines.append(f"- assistant turns: {len(assistant_msgs)}")
    if tool_counts:
        top = ", ".join(f"{n}×{name}" for name, n in tool_counts.most_common(8))
        lines.append(f"- tool calls: {sum(tool_counts.values())} ({top})")
    lines.append("")

    lines.append("## What the user asked for")
    lines.append("")
    for i, msg in enumerate(user_msgs[:8], 1):
        snippet = msg.replace("\n", " ")[:300]
        lines.append(f"{i}. {snippet}")
    if len(user_msgs) > 8:
        lines.append(f"... and {len(user_msgs) - 8} more user turns")
    lines.append("")

    lines.append("## Final assistant message")
    lines.append("")
    if assistant_msgs:
        last = assistant_msgs[-1]
        lines.append(last[:2000])
    lines.append("")

    return "\n".join(lines)


def write_session_checkpoint(transcript_path: str | None, project: str | None, source: str) -> Path | None:
    if not project:
        project = "unknown"
    if not transcript_path:
        return None
    parsed = parse_transcript(Path(transcript_path))
    if len(parsed["user_msgs"]) < 1:
        return None  # nothing meaningful happened
    if len(parsed["assistant_msgs"]) < 1 and len(parsed["tool_calls"]) < 1:
        return None  # user typed but the model never did anything — not worth a checkpoint
    body = render_checkpoint(parsed, source=source, project=project)
    return _vault.write_checkpoint(project, body)
