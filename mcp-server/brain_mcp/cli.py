"""`brain` — command-line frontend over the vault.

The token-cheap alternative to the MCP server: agents that have a shell tool
(Claude Code via the brain skill, pi, anything else) invoke these subcommands
instead of loading 8 MCP tool schemas into every session. Output is the same
capped, compact markdown the MCP server emits — both frontends route through
`render.py`, so behavior stays identical whichever transport a client uses.

Reads BRAIN_VAULT from the environment, like everything else in this package.

    brain recall <query> [--type T] [--project P] [--top-k N] [--full-body]
                 [--include-sessions] [--json]
    brain save <type> <name> [--content TEXT | --file PATH] [--project P]
        (with neither --content nor --file, the body is read from stdin —
         use a heredoc for multi-line content)
    brain list [--type T] [--project P] [--include-sessions] [--json]
    brain forget <path>
    brain checkpoint <project> [--summary TEXT | --file PATH]  (stdin fallback)
    brain stats
    brain doctor [--project P] [--json] [--quiet]
"""

from __future__ import annotations

import argparse
import json
import sys

from . import render, vault


def _read_body(args: argparse.Namespace, flag_value: str | None) -> str:
    if flag_value is not None:
        return flag_value
    if getattr(args, "file", None):
        with open(args.file, "r", encoding="utf-8") as f:
            return f.read()
    data = sys.stdin.read()
    if not data.strip():
        raise SystemExit(
            "error: no content — pass --content, --file, or pipe the body via stdin"
        )
    return data


def _cmd_recall(args: argparse.Namespace) -> int:
    payload = render.recall_payload(
        query=" ".join(args.query),
        mtype=args.type,
        project=args.project,
        top_k=args.top_k,
        full_body=args.full_body,
        include_sessions=args.include_sessions,
    )
    if args.json:
        print(json.dumps(payload, default=str))
    else:
        print(render.render_recall(payload))
    return 0


def _cmd_save(args: argparse.Namespace) -> int:
    content = _read_body(args, args.content)
    path = vault.write_memory(
        mtype=args.type, name=args.name, content=content, project=args.project
    )
    print(f"saved: {path}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    payload = render.list_payload(
        mtype=args.type, project=args.project, include_sessions=args.include_sessions
    )
    if args.json:
        print(json.dumps(payload, default=str))
    else:
        print(render.render_list(payload))
    return 0


def _cmd_forget(args: argparse.Namespace) -> int:
    path = vault.forget_memory(args.path)
    print(f"forgot: {path}")
    return 0


def _cmd_checkpoint(args: argparse.Namespace) -> int:
    summary = _read_body(args, args.summary)
    path = vault.write_checkpoint(args.project, summary)
    print(f"checkpoint: {path}")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    print(json.dumps(vault.stats(), default=str))
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor

    findings = doctor.check(args.project, args.cwd)
    if args.json:
        print(json.dumps(findings))
    else:
        for f in findings:
            if args.quiet and f["severity"] in ("ok", "info"):
                continue
            print(f"[{f['severity'].upper():5s}] {f['code']}: {f['message']}")
            if f.get("hint"):
                print(f"        -> {f['hint']}")
    return 0 if doctor.worst_severity(findings) != "error" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brain", description="Ai-Brain memory CLI (reads BRAIN_VAULT from env)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("recall", help="search memories")
    p.add_argument("query", nargs="+", help="search terms")
    p.add_argument("--type", choices=sorted(vault.VALID_TYPES))
    p.add_argument("--project", help="filter to a project basename")
    p.add_argument("--top-k", type=int, default=render.DEFAULT_TOP_K)
    p.add_argument("--full-body", action="store_true",
                   help="full (still capped) bodies instead of previews")
    p.add_argument("--include-sessions", action="store_true",
                   help="include session checkpoints in results")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_recall)

    p = sub.add_parser("save", help="write a memory")
    p.add_argument("type", choices=sorted(vault.VALID_TYPES))
    p.add_argument("name", help="short title, 3-8 words (slugified for the filename)")
    p.add_argument("--content", "-c", help="memory body; omit to read stdin")
    p.add_argument("--file", "-f", help="read the body from a file")
    p.add_argument("--project", help="project basename (required for type=project)")
    p.set_defaults(func=_cmd_save)

    p = sub.add_parser("list", help="enumerate memories (paths + descriptions)")
    p.add_argument("--type", choices=sorted(vault.VALID_TYPES))
    p.add_argument("--project")
    p.add_argument("--include-sessions", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("forget", help="delete a memory file")
    p.add_argument("path", help="path from a prior recall/list result")
    p.set_defaults(func=_cmd_forget)

    p = sub.add_parser("checkpoint", help="write a session checkpoint")
    p.add_argument("project", help="project directory basename")
    p.add_argument("--summary", "-s", help="checkpoint body; omit to read stdin")
    p.add_argument("--file", "-f", help="read the summary from a file")
    p.set_defaults(func=_cmd_checkpoint)

    p = sub.add_parser("stats", help="vault telemetry")
    p.set_defaults(func=_cmd_stats)

    p = sub.add_parser("doctor", help="run health checks")
    p.add_argument("--project")
    p.add_argument("--cwd", help="project working dir for the stale-uncommitted check")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true", help="warn/error findings only")
    p.set_defaults(func=_cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> None:
    # Windows consoles/pipes default to cp1252 — in BOTH directions. stdout
    # would crash print()ing a memory with characters outside cp1252, and stdin
    # would mangle a UTF-8 heredoc body into mojibake (2026-07-28: em dashes
    # saved as `â€"`, and an undecodable 0x9D byte from a curly quote killed a
    # save mid-command). Force UTF-8 end to end, like the vault files.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    args = build_parser().parse_args(argv)
    try:
        sys.exit(args.func(args))
    except SystemExit:
        raise
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
