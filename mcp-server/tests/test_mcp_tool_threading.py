"""MCP tool calls run off the event loop, and still one at a time.

Every tool is synchronous file and sqlite work, and `call_tool` ran it directly
on the stdio server's event loop. A recall that spent its 5 s sync budget froze
the whole server for those 5 s: no ping answered, no cancellation read. The
handler now hands the work to a thread. It still holds a lock, because the vault
and embedding code were written for one caller per process.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import sys
import threading
import time
from pathlib import Path

import pytest

from brain_mcp import render, server


def _slow_recall(seconds: float, record: list | None = None):
    active = {"n": 0}
    lock = threading.Lock()

    def recall_payload(**kwargs):
        with lock:
            active["n"] += 1
            if record is not None:
                record.append(active["n"])
        time.sleep(seconds)
        with lock:
            active["n"] -= 1
        return {"query": kwargs["query"], "results": [], "omitted": 0}

    return recall_payload


def test_a_slow_tool_does_not_block_the_loop(vault_dir: Path, monkeypatch) -> None:
    monkeypatch.setattr(render, "recall_payload", _slow_recall(0.5))
    monkeypatch.setattr(render, "render_recall", lambda payload: "ok")

    async def main() -> int:
        task = asyncio.create_task(server.call_tool("brain_recall", {"query": "x"}))
        ticks = 0
        while not task.done():
            await asyncio.sleep(0.01)
            ticks += 1
        assert task.result()[0].text == "ok"
        return ticks

    # Blocking on the loop yields about one tick; a free loop ticks throughout.
    assert asyncio.run(main()) >= 10


def test_concurrent_calls_are_serialized(vault_dir: Path, monkeypatch) -> None:
    concurrency: list[int] = []
    monkeypatch.setattr(render, "recall_payload", _slow_recall(0.1, concurrency))
    monkeypatch.setattr(render, "render_recall", lambda payload: "ok")

    async def main() -> None:
        await asyncio.gather(*(server.call_tool("brain_recall", {"query": f"q{i}"})
                               for i in range(3)))

    asyncio.run(main())
    assert concurrency == [1, 1, 1]


def test_a_raising_tool_still_returns_an_error_result(vault_dir: Path,
                                                      monkeypatch) -> None:
    def boom(**kwargs):
        raise RuntimeError("index exploded")

    monkeypatch.setattr(render, "recall_payload", boom)
    out = asyncio.run(server.call_tool("brain_recall", {"query": "x"}))
    assert "RuntimeError: index exploded" in out[0].text
    # And the lock was released: the next call runs.
    monkeypatch.setattr(render, "recall_payload", _slow_recall(0))
    monkeypatch.setattr(render, "render_recall", lambda payload: "ok")
    assert asyncio.run(server.call_tool("brain_recall", {"query": "y"}))[0].text == "ok"


# ------------------------------------------- Windows: nothing may wait on the stdin pipe
#
# With tools in worker threads, the transport's reader thread has a blocking read
# pending on stdin whenever the loop is idle. On Windows two things then block until
# that read completes, which is never, since a client waiting on a result sends
# nothing more. Both were reproduced against the live server on 2026-10-01:
#
#   * loading numpy's extension DLL in a tool thread (the first recall hung), and
#   * starting a subprocess that inherits the server's stdin.
#
# The old on-the-loop handler never hit either, because a blocked loop never issued
# the next read. A pipe-level reproduction needs Windows, so these assert the two
# properties that remove the hazard, everywhere.


def _subprocess_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr in {"run", "Popen", "call", "check_call",
                                       "check_output"}):
            yield node


def _dict_names_with_stdin(tree: ast.AST) -> set[str]:
    """Names assigned a dict literal that has a "stdin" key (the `**kwargs` form)."""
    names = set()
    for node in ast.walk(tree):
        targets, value = [], None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if isinstance(value, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value == "stdin" for k in value.keys):
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def test_every_subprocess_in_the_package_sets_stdin() -> None:
    """A child must never inherit the MCP server's stdin: on Windows the spawn hangs,
    and anywhere a child that reads stdin would consume protocol bytes."""
    package = Path(server.__file__).parent
    offenders, seen = [], 0
    for src in sorted(package.glob("*.py")):
        tree = ast.parse(src.read_text(encoding="utf-8"))
        with_stdin = _dict_names_with_stdin(tree)
        for call in _subprocess_calls(tree):
            seen += 1
            explicit = any(k.arg == "stdin" for k in call.keywords)
            via_kwargs = any(k.arg is None and isinstance(k.value, ast.Name)
                             and k.value.id in with_stdin for k in call.keywords)
            if not (explicit or via_kwargs):
                offenders.append(f"{src.name}:{call.lineno}")
    assert seen >= 4, "the scan found too few calls; is it still looking?"
    assert not offenders, f"subprocess call without stdin=: {offenders}"


def test_run_imports_native_modules_before_reading_stdin(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(server, "_preload_native_modules", lambda: order.append("preload"))
    monkeypatch.setattr(server, "_background_embed_warmup", lambda: None)

    class _Stop(Exception):
        pass

    @contextlib.asynccontextmanager
    async def fake_stdio_server():
        order.append("stdio")
        raise _Stop
        yield  # pragma: no cover

    monkeypatch.setattr(server, "stdio_server", fake_stdio_server)
    with pytest.raises(_Stop):
        asyncio.run(server.run())
    assert order == ["preload", "stdio"]


def test_preload_imports_numpy() -> None:
    server._preload_native_modules()
    assert "numpy" in sys.modules
