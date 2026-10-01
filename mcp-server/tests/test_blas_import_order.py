"""The single-thread BLAS setting only works if nothing imports numpy first.

`brain_mcp/__init__` sets OPENBLAS_NUM_THREADS and friends, because numpy's bundled
OpenBLAS pre-allocates ~750 MB of commit per process at its default thread count and
the SessionStart hook spawns several brain processes at once (strixlappy,
2026-09-14). The variables are read once, at numpy's import, so a module-level
`import numpy` anywhere that runs before `brain_mcp` would silently undo it.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HEAVY = {"numpy", "onnxruntime", "fastembed"}
BLAS_VARS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")


def _module_level_imports(tree: ast.Module) -> set[str]:
    """Top-level names imported when the module loads: function bodies excluded,
    everything else (try/if blocks, class bodies) included."""
    found: set[str] = set()
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
        stack.extend(ast.iter_child_nodes(node))
    return found


def test_no_module_imports_a_blas_user_at_load_time() -> None:
    sources = [*(REPO_ROOT / "mcp-server" / "brain_mcp").glob("*.py"), *(REPO_ROOT / "hooks").glob("*.py")]
    offenders = []
    for py in sources:
        heavy = _module_level_imports(ast.parse(py.read_text(encoding="utf-8"))) & HEAVY
        if heavy:
            offenders.append(f"{py.relative_to(REPO_ROOT).as_posix()}: {sorted(heavy)}")
    assert not offenders, offenders


@pytest.mark.parametrize("entry", ["brain_mcp.cli", "brain_mcp.server", "brain_mcp.vault"])
def test_importing_an_entry_point_pins_blas_and_loads_no_numpy(entry: str) -> None:
    env = {k: v for k, v in os.environ.items() if k not in BLAS_VARS}
    env["OMP_NUM_THREADS"] = ""  # empty is not an override; it must still become "1"
    code = (
        "import os, sys; import " + entry + "; "
        "print(','.join(os.environ.get(v, '-') for v in " + repr(BLAS_VARS) + ")); "
        "print('numpy' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=str(REPO_ROOT / "mcp-server"), env=env,
        capture_output=True, text=True, check=True, timeout=120,
    ).stdout.split()
    assert out == ["1,1,1", "False"], out
