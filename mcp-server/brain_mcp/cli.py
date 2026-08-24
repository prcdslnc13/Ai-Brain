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
    brain checkpoint [project] --from-cherryd DB [--session N]
                     [--all-sessions] [--list-sessions] [--force]
    brain checkpoint [project] --from-pi SESSION.jsonl [--source TAG] [--force]
        (project is inferred from the session's cwd when omitted)
    brain stats
    brain doctor [--project P] [--json] [--quiet]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import render, vault
from ._console import force_utf8_stdio


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
    if args.from_cherryd and args.from_pi:
        raise SystemExit("error: --from-cherryd and --from-pi are mutually exclusive")
    if args.from_cherryd:
        return _cmd_checkpoint_cherryd(args)
    if args.from_pi:
        return _cmd_checkpoint_pi(args)
    if not args.project:
        raise SystemExit("error: checkpoint needs a project (or --from-cherryd DB)")
    summary = _read_body(args, args.summary)
    path = vault.write_checkpoint(args.project, summary)
    print(f"checkpoint: {path}")
    return 0


def _cmd_checkpoint_cherryd(args: argparse.Namespace) -> int:
    """Checkpoint straight out of a harness's own event log.

    For harnesses with no hook system, this is the only mechanism that
    survives the model running out of context: the log is already on disk, so
    a timer can capture the session without the model having to remember.
    """
    from . import transcript

    db = Path(args.from_cherryd).expanduser()
    try:
        if args.list_sessions:
            rows = transcript.cherryd_sessions(db)
            if args.json:
                print(json.dumps(rows, default=str))
            else:
                print(f"{len(rows)} session(s) in {db}")
                for r in rows:
                    print(f"  session {r['id']}  events={r['event_count']}  "
                          f"last={r['last_ts'] or '-'}  cwd={r['cwd'] or '-'}")
            return 0
        results = transcript.checkpoint_cherryd(
            db,
            session_id=args.session,
            project=args.project,
            all_sessions=args.all_sessions,
            force=args.force,
        )
    except transcript.CherrydError as e:
        raise SystemExit(f"error: {e}")

    if args.json:
        print(json.dumps(results, default=str))
        return 0
    for r in results:
        if r["written"]:
            print(f"checkpoint: {r['written']}  (session {r['session_id']}, "
                  f"project {r['project']})")
        else:
            print(f"skipped session {r['session_id']}: {r['reason']}")
    return 0


def _cmd_checkpoint_pi(args: argparse.Namespace) -> int:
    """Checkpoint straight out of a pi session file.

    The pi extension calls this on compaction, on a turn cadence, and on
    shutdown; a timer can call it on a session file nobody is attending. Either
    way the parsing and rendering stay here, so a pi checkpoint is
    byte-identical to one written by the Claude Code hooks.
    """
    from . import transcript

    try:
        result = transcript.checkpoint_pi(
            Path(args.from_pi),
            project=args.project,
            source=args.source or "pi",
            force=args.force,
        )
    except transcript.PiSessionError as e:
        raise SystemExit(f"error: {e}")

    if args.json:
        print(json.dumps(result, default=str))
        return 0
    if result["written"]:
        print(f"checkpoint: {result['written']}  (pi session "
              f"{result['session_id']}, project {result['project']})")
    else:
        print(f"skipped: {result['reason']}")
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
    p.add_argument("project", nargs="?",
                   help="project directory basename (inferred from the session cwd "
                        "when --from-cherryd is used)")
    p.add_argument("--summary", "-s", help="checkpoint body; omit to read stdin")
    p.add_argument("--file", "-f", help="read the summary from a file")
    p.add_argument("--from-cherryd", metavar="DB",
                   help="build the checkpoint from a cherryd SQLite event log "
                        "instead of stdin (for harnesses with no hooks)")
    p.add_argument("--from-pi", metavar="SESSION",
                   help="build the checkpoint from a pi session JSONL (or a "
                        "directory, whose newest session is used) instead of stdin")
    p.add_argument("--source", metavar="TAG",
                   help="label for the checkpoint header, e.g. pi:compact "
                        "(--from-pi only; default 'pi')")
    p.add_argument("--session", type=int,
                   help="cherryd session id; default is the most recently active")
    p.add_argument("--all-sessions", action="store_true",
                   help="checkpoint every cherryd session with new activity")
    p.add_argument("--list-sessions", action="store_true",
                   help="list the sessions in the event log and exit")
    p.add_argument("--force", action="store_true",
                   help="write even when nothing new happened since the last run")
    p.add_argument("--json", action="store_true", help="machine-readable output")
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
    # Windows consoles/pipes default to cp1252 in BOTH directions; see
    # _console.py for what that broke. stdin matters here because save bodies
    # arrive through a heredoc.
    force_utf8_stdio(include_stdin=True)
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
