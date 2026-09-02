"""Shared fixtures.

Every test runs against a throwaway vault, never the user's real one: these tests
write, delete, and corrupt files, and `BRAIN_VAULT` is read from the environment at
call time by everything in the package, so a leaked env var would point that
destruction at `~/Vaults/Ai-Brain`. The `vault` fixture is therefore autouse-adjacent
by convention — ask for it in any test that touches the package.

Vector search is off by default (`BRAIN_EMBED=0`): loading the ONNX model costs
~5-10s and the vast majority of these tests are about wiring, not similarity. Tests
that genuinely need vectors ask for the `embedding_vault` fixture instead.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "mcp-server"))
sys.path.insert(0, str(REPO_ROOT / "hooks"))


def load_repo_script(filename: str):
    """Import a repo-root script (`brain-setup.py`, `brain-uninstall.py`, ...) by path.

    Their names carry a dash, so they cannot be imported by name, and the repo root
    is deliberately not on sys.path. Every call returns a FRESH module object that is
    not registered in sys.modules, so a test can monkeypatch `IS_WINDOWS` or `VENV_PY`
    on its copy without leaking into another test's.
    """
    import importlib.util

    path = REPO_ROOT / filename
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_")[:-3], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def memory(path: Path, name: str, mtype: str, body: str, description: str | None = None) -> Path:
    """Write a well-formed memory file with valid frontmatter."""
    desc = description if description is not None else body.strip().split("\n", 1)[0][:150]
    fm = f"---\nname: {name}\ndescription: {desc}\ntype: {mtype}\n---\n\n"
    return _write(path, fm + body.strip() + "\n")


@pytest.fixture
def vault_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty but structurally valid vault, wired into BRAIN_VAULT."""
    root = tmp_path / "vault"
    brain = root / "Brain"
    for sub in ("user", "feedback", "references", "projects"):
        (brain / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BRAIN_VAULT", str(root))
    monkeypatch.setenv("BRAIN_EMBED", "0")
    monkeypatch.setenv("BRAIN_MACHINE", "test-host")
    _reset_module_state()
    yield brain
    _reset_module_state()


@pytest.fixture
def populated_vault(vault_dir: Path) -> Path:
    """A vault with one memory of each type plus a session checkpoint."""
    memory(vault_dir / "user" / "prefers-rust.md", "prefers rust", "user",
           "I prefer Rust over Go for systems work.")
    memory(vault_dir / "feedback" / "no-force-push.md", "no force push", "feedback",
           "Never force-push to a shared branch.")
    memory(vault_dir / "references" / "dashboards.md", "dashboards", "reference",
           "Oncall dashboards live at grafana.internal.")
    memory(vault_dir / "projects" / "Widget" / "overview.md", "overview", "project",
           "Widget is a thing that widgets.")
    _write(vault_dir / "projects" / "Widget" / "sessions" / "2026-01-01-1200-test-host.md",
           "---\nproject: Widget\ntimestamp: 2026-01-01T12:00\n---\n\nDid some Widget work.\n")
    return vault_dir


@pytest.fixture
def embedding_vault(vault_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Same vault, but with vector search enabled.

    Skips rather than fails when fastembed/numpy or the cached model is absent, so
    the suite still runs on a machine that has never downloaded the ONNX weights.
    """
    monkeypatch.delenv("BRAIN_EMBED", raising=False)
    pytest.importorskip("numpy")
    pytest.importorskip("fastembed")
    from brain_mcp import embed
    if not embed._model_is_cached(embed._CACHE_DIR):
        pytest.skip("embedding model not cached on this machine")
    return vault_dir


def _reset_module_state() -> None:
    """Clear module-level caches that would otherwise leak between vaults.

    These are process-global by design (a long-lived MCP server wants them), which
    makes them a cross-test contamination hazard: a matrix cached from one tmp vault
    would be served to the next.
    """
    try:
        from brain_mcp import embed
        embed._MATRIX_CACHE.update(key=None, paths=None, mat=None)
    except Exception:
        pass
    try:
        from brain_mcp import vault as _v
        _v._machine_name_cache = None
    except Exception:
        pass
