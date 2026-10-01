"""Everything the Brain writes into the vault is LF, on every OS.

`_atomic_write` used `write_text` without `newline`, and `append_activity` opened
activity.md in text mode, so on Windows every "\n" became CRLF: a memory had
different bytes depending on which machine last saved it (41 of the 50
strixlappy-stamped notes were CRLF on 2026-10-01), and activity.md mixed Windows
and Mac rows. The same class as the save-events.jsonl fix in #35.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain_mcp import vault

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_a_saved_memory_is_lf(vault_dir: Path) -> None:
    path = vault.save_memory("feedback", "Line endings", "rule\n\n**Why:** reason\n").path
    assert b"\r" not in path.read_bytes()


def test_a_checkpoint_is_lf(vault_dir: Path) -> None:
    path = vault.write_checkpoint("Widget", "line one\nline two\n")
    assert b"\r" not in path.read_bytes()


def test_activity_rows_are_lf(vault_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import _common

    monkeypatch.setenv("BRAIN_VAULT", str(vault_dir.parent))
    _common.append_activity("2026-10-01 12:00 claude Widget [sig=N] - one")
    _common.append_activity("2026-10-01 12:01 claude Widget [sig=N] - two")
    data = (vault_dir / "activity.md").read_bytes()
    assert b"\r" not in data and data.count(b"\n") >= 2


# A text-mode write that does not pin `newline` is the bug; bytes writes are fine.
_TEXT_WRITE = re.compile(
    r"""\.write_text\(|\.open\(\s*["'][wax]\+?["']|\bopen\([^)]*["'][wax]\+?["']"""
)


def test_every_text_write_in_the_package_and_hooks_pins_newline() -> None:
    offenders = []
    for py in [*(REPO_ROOT / "mcp-server" / "brain_mcp").glob("*.py"), *(REPO_ROOT / "hooks").glob("*.py")]:
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if _TEXT_WRITE.search(code) and "newline=" not in code:
                offenders.append(f"{py.relative_to(REPO_ROOT).as_posix()}:{lineno}: {line.strip()}")
    assert not offenders, offenders
