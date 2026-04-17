"""Brain MCP server (stdio transport).

Exposes the Ai-Brain vault as a small, typed tool surface that any MCP-capable client
(Claude Code, LMStudio, Open WebUI, msty, etc.) can call.
"""

from __future__ import annotations

import json
import os
import sys
import threading

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import vault

server: Server = Server("brain")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="brain_session_start",
            description=(
                "Load the standard session preload bundle from the vault: index, user profile, "
                "all feedback, and (if a project is given) the project overview + most recent "
                "session checkpoint. Idempotent and cheap. Call this at the start of every conversation "
                "if your environment didn't already inject the bundle."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "basename of the project directory (e.g. 'MyProject'). Optional.",
                    }
                },
            },
        ),
        Tool(
            name="brain_recall",
            description=(
                "Search the vault for memories matching a query. Use this proactively whenever the user "
                "mentions a project, person, tool, or topic you might already know about — do not wait "
                "to be asked. Returns previews by default to keep responses small; if a hit looks "
                "relevant and you need its full content, recall again with full_body=true and a tighter "
                "query."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": ["user", "feedback", "project", "reference"],
                        "description": "optional filter by memory type",
                    },
                    "project": {
                        "type": "string",
                        "description": "optional filter to a specific project basename",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "max number of hits to return (default 5).",
                        "default": 5,
                    },
                    "full_body": {
                        "type": "boolean",
                        "description": "return the full file body instead of a ~600-char preview "
                                       "(default false).",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="brain_save",
            description=(
                "Write a memory to the vault. Call this proactively whenever you learn something that "
                "matches the auto-memory taxonomy. ALL THREE fields 'type', 'name', and 'content' are "
                "required. Do NOT save code patterns, git history, ephemeral state, or things derivable "
                "from the current code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["user", "feedback", "project", "reference"],
                        "description": (
                            "REQUIRED. Pick exactly one: "
                            "'user' = facts about the user (role, preferences, expertise); "
                            "'feedback' = a behavior rule from the user (corrections or validated approaches); "
                            "'project' = ongoing project context (decisions, deadlines, stakeholders) — "
                            "also requires the 'project' field; "
                            "'reference' = pointer to an external system (Linear project, Slack channel, etc)."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "REQUIRED. Short title for the memory, 3-8 words. Will be slugified for the "
                            "filename (e.g. 'Prefer Rust over Go' becomes 'prefer-rust-over-go.md')."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "REQUIRED. The memory body in plain markdown. For 'feedback' and 'project' "
                            "types, structure as: the rule/fact, then a 'Why:' line, then a "
                            "'How to apply:' line. Frontmatter is added automatically — do not include it."
                        ),
                    },
                    "project": {
                        "type": "string",
                        "description": (
                            "REQUIRED only when type='project'. The basename of the project DIRECTORY "
                            "(e.g. 'AiBrain' for ~/src/AiBrain), NOT a category or topic name. Omit "
                            "this field for type='user', 'feedback', or 'reference'."
                        ),
                    },
                },
                "required": ["type", "name", "content"],
            },
        ),
        Tool(
            name="brain_list",
            description="Enumerate memories, optionally filtered by type and/or project. All fields are optional.",
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["user", "feedback", "project", "reference"],
                        "description": "Optional. Filter to one memory type. Omit to list every type.",
                    },
                    "project": {
                        "type": "string",
                        "description": (
                            "Optional. Project directory basename (e.g. 'AiBrain') to filter results "
                            "to one project's memories. Only meaningful with type='project'."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="brain_forget",
            description=(
                "Delete a memory file. Use the 'path' value from a prior brain_recall or brain_list "
                "result; relative paths are resolved against the vault root."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "REQUIRED. Path to the memory file to delete. Use the value from a prior "
                            "brain_recall result (e.g. 'Brain/user/profile.md')."
                        ),
                    },
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="brain_checkpoint",
            description=(
                "Write a session checkpoint for a project. Call this at the end of a meaningful work "
                "session — when the user signals completion, when you finish a multi-step task, or when "
                "context is about to be lost. Both 'project' and 'summary' are required."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": (
                            "REQUIRED. Project directory basename (e.g. 'AiBrain') — NOT a category. "
                            "If unsure, use the basename of the current working directory."
                        ),
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "REQUIRED. Plain markdown summary of the session. Cover: what was "
                            "attempted, what worked, what failed, decisions made, open threads."
                        ),
                    },
                },
                "required": ["project", "summary"],
            },
        ),
        Tool(
            name="brain_stats",
            description=(
                "Report vault telemetry: counts, index size, oldest active checkpoint. "
                "Useful for health checks."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


def _ok(payload) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": msg}))]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[TextContent]:
    args = arguments or {}
    try:
        if name == "brain_session_start":
            return _ok(vault.session_start_bundle(args.get("project")))
        if name == "brain_recall":
            top_k = int(args.get("top_k", 5))
            full_body = bool(args.get("full_body", False))
            results = vault.search_memories(
                query=args["query"],
                mtype=args.get("type"),
                project=args.get("project"),
            )
            truncated_total = len(results)
            results = results[:max(1, top_k)]
            body_chars = None if full_body else 600
            return _ok({
                "count": len(results),
                "total_matches": truncated_total,
                "preview": not full_body,
                "results": [m.to_dict(body_chars=body_chars) for m in results],
            })
        if name == "brain_save":
            path = vault.write_memory(
                mtype=args["type"],
                name=args["name"],
                content=args["content"],
                project=args.get("project"),
            )
            return _ok({"saved": str(path)})
        if name == "brain_list":
            results = vault.list_memories(
                mtype=args.get("type"),
                project=args.get("project"),
            )
            return _ok({"count": len(results), "memories": [m.to_dict() for m in results]})
        if name == "brain_forget":
            path = vault.forget_memory(args["path"])
            return _ok({"forgot": str(path)})
        if name == "brain_checkpoint":
            path = vault.write_checkpoint(args["project"], args["summary"])
            return _ok({"checkpoint": str(path)})
        if name == "brain_stats":
            return _ok(vault.stats())
        return _err(f"unknown tool: {name}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


def _background_embed_warmup() -> None:
    """Pre-load the embedding model and sync the index in a background thread.

    MCP clients (LMStudio, Claude Code) launch the server as a fresh process per
    session; the on-disk HF cache primed by `brain-setup.py` doesn't carry over
    in-memory model weights, so the first foreground `brain_recall` would otherwise
    pay the full 5-10s ONNX load and exceed per-tool timeouts. Doing the load
    eagerly at startup means the model is hot by the time a tool call arrives.
    """
    if os.environ.get("BRAIN_EMBED", "1") == "0":
        return
    try:
        from . import embed
        embed.EmbedIndex.sync()
    except Exception as e:
        print(f"brain embed background warmup: {e}", file=sys.stderr)


async def run() -> None:
    threading.Thread(target=_background_embed_warmup, daemon=True).start()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
