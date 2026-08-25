"""Shell scripts must keep LF line endings.

A CRLF shebang (`#!/usr/bin/env bash\\r`) makes the kernel look for an interpreter
literally named "bash\\r", so the script dies with "bad interpreter: No such file or
directory" before running a line. Every heredoc terminator breaks too.

This is a Windows-development hazard with no local symptom: the repo is developed on
Windows and these scripts only ever *run* on macOS and Linux, so nothing here would
notice. It happened during this very review — a Python helper that rewrote three
uninstallers with the platform default newline silently converted all of them, turning
the whole diff into a full-file rewrite that hid the 24-line change inside it.

Keep the whole repo LF; nothing in it requires CRLF.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

TEXT_SUFFIXES = {".sh", ".ps1", ".py", ".md", ".json", ".toml", ".ts", ".cmd"}


def _tracked_text_files():
    """Only files git tracks.

    Deliberately `git ls-files` rather than a directory walk: build trees, virtualenvs
    and tool caches are full of vendored CRLF that says nothing about this repo, and a
    check that cries wolf about `.pytest_cache/README.md` gets switched off.
    """
    import subprocess

    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        pytest.skip("not a git checkout")
    for rel in result.stdout.split("\0"):
        if not rel:
            continue
        path = REPO_ROOT / rel
        if path.suffix in TEXT_SUFFIXES and path.is_file():
            yield path


def test_no_file_uses_crlf():
    offenders = []
    for path in _tracked_text_files():
        data = path.read_bytes()
        if b"\r\n" in data:
            offenders.append(
                f"{path.relative_to(REPO_ROOT).as_posix()} ({data.count(chr(13).encode() + chr(10).encode())} CRLF)"
            )
    assert not offenders, (
        "CRLF line endings found: " + ", ".join(offenders) + ". A CRLF shebang makes a "
        "POSIX shell script unrunnable, and a CRLF heredoc terminator never matches. "
        "Write files with newline='\\n' (or bytes) when editing from Windows."
    )


@pytest.mark.parametrize(
    "script",
    # The four POSIX shell scripts were retired by ROADMAP 3G (2026-08-25). The two
    # Python entry points inherit the same hazard: they carry `#!/usr/bin/env python3`
    # and are documented as directly executable, so a CRLF first line is still fatal.
    ["brain-setup.py", "brain-uninstall.py", "brain_settings_merge.py"],
)
def test_posix_scripts_have_a_clean_shebang(script):
    """The specific failure: a trailing CR on line 1 kills the script outright."""
    first_line = (REPO_ROOT / script).read_bytes().split(b"\n", 1)[0]
    assert first_line.startswith(b"#!"), f"{script} lost its shebang"
    assert not first_line.endswith(b"\r"), (
        f"{script} has a CRLF shebang — POSIX will look for an interpreter named "
        f"'bash\\r' and fail with 'bad interpreter'"
    )
