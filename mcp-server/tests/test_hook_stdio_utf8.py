"""The hooks must read their payload as UTF-8 whatever the console codepage is.

Claude Code hands every hook a UTF-8 JSON payload on stdin. A Python subprocess
on Windows decodes a pipe with the locale codepage (cp1252) unless told
otherwise, and the hook launcher sets only BRAIN_VAULT. The 2026-07-29 UTF-8 fix
covered `cli.py` and never reached `hooks/`: a cwd of `D:/tmp/Café—x` arrived as
`CafÃ©â€”x`, which `validate_project_name` accepts (a blacklist, by design), so
the overview stub, every checkpoint and every project-scoped feedback landed in
a mojibake project directory while the real project's memories never preloaded.
A Cyrillic cwd hit one of cp1252's undefined bytes, `sys.stdin.read()` raised,
and the hook died before emitting anything — no preload at all.

Same class as `test_the_installer_reads_and_writes_templates_as_utf8`: the
console encoding is an input the code must pin, not inherit. Each hook is run
as a real subprocess with `PYTHONIOENCODING=cp1252`, which is what a stock
Windows console gives a piped Python, on every platform — and the project name
must come out the other side intact.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = REPO_ROOT / "hooks"
MCP_DIR = REPO_ROOT / "mcp-server"

HOOK_SCRIPTS = sorted(
    p.name for p in HOOKS_DIR.glob("*.py") if not p.name.startswith("_")
)

# An accented Latin name (each byte is *defined* in cp1252, so it silently
# becomes mojibake) and a Cyrillic one (0x9F-range bytes are undefined, so the
# old read raised).
PROJECT_NAMES = ["Café—x", "Проект"]


def _transcript(path: Path) -> Path:
    lines = [
        {"type": "user", "message": {"role": "user", "content": "please do the thing"}},
        {"type": "assistant", "message": {"role": "assistant",
                                          "content": [{"type": "text", "text": "done"}]}},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


def _run_hook(script: str, payload: dict, vault_root: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("BRAIN_")}
    env.update({
        "BRAIN_VAULT": str(vault_root),
        "BRAIN_EMBED": "0",
        "BRAIN_AUTO_REINDEX": "0",
        "BRAIN_STALE_CHECK": "0",
        "BRAIN_MACHINE": "test-host",
        "PYTHONIOENCODING": "cp1252",
        "PYTHONUTF8": "0",
        # The source tree, not whatever copy is installed in the interpreter's
        # site-packages — same rule as pyproject's `pythonpath`.
        "PYTHONPATH": os.pathsep.join([str(MCP_DIR), str(HOOKS_DIR)]),
    })
    return subprocess.run(
        [sys.executable, str(HOOKS_DIR / script)],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        env=env,
        cwd=str(vault_root),
        timeout=120,
    )


@pytest.mark.parametrize("name", PROJECT_NAMES)
@pytest.mark.parametrize("script", ["stop.py", "pre_compact.py", "session_end.py", "session_start.py"])
def test_hook_keeps_a_non_ascii_project_name_under_cp1252(
    script: str, name: str, vault_dir: Path, tmp_path: Path
) -> None:
    vault_root = vault_dir.parent
    project_cwd = tmp_path / name
    project_cwd.mkdir()
    payload = {
        "cwd": str(project_cwd),
        "hook_event_name": script,
        "transcript_path": str(_transcript(tmp_path / "t.jsonl")),
    }
    proc = _run_hook(script, payload, vault_root)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")

    if script == "stop.py":
        rows = (vault_dir / "activity.md").read_text(encoding="utf-8")
        assert f" {name} [" in rows, rows
    else:
        project_dirs = sorted(p.name for p in (vault_dir / "projects").iterdir())
        assert name in project_dirs, project_dirs
        assert not any("Ã" in d or "\ufffd" in d for d in project_dirs), project_dirs

    # Whatever the hook says back must itself be valid UTF-8 JSON (or nothing):
    # Claude Code decodes hook stdout as UTF-8.
    out = proc.stdout.decode("utf-8")
    if out.strip():
        json.loads(out)


def test_only_common_touches_stdin() -> None:
    """One place reads the payload, so one place pins the encoding.

    A hook that grew its own `sys.stdin.read()` would reintroduce the bug for
    that hook alone, invisibly on macOS/Linux. Parsed, not grepped, so a comment
    mentioning stdin does not fail the build.
    """
    offenders: list[str] = []
    for script in HOOKS_DIR.glob("*.py"):
        if script.name == "_common.py":
            continue
        tree = ast.parse(script.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == "stdin"
                    and isinstance(node.value, ast.Name) and node.value.id == "sys"):
                offenders.append(f"{script.name}:{node.lineno}")
    assert not offenders, f"hooks reading stdin outside _common.read_payload: {offenders}"


def test_read_payload_forces_utf8_before_reading() -> None:
    """`_common.read_payload` must reconfigure the streams before the read —
    reconfiguring after would decode the payload with the old codec."""
    src = (HOOKS_DIR / "_common.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "read_payload")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
    force = next((c for c in calls
                  if isinstance(c.func, ast.Name) and c.func.id == "force_utf8_stdio"), None)
    read = next((c for c in calls
                 if isinstance(c.func, ast.Attribute) and c.func.attr == "read"), None)
    assert force is not None, "read_payload no longer forces UTF-8 stdio"
    assert read is not None
    assert force.lineno < read.lineno, "force_utf8_stdio must run before sys.stdin.read()"
