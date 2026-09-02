"""The lexical query is data, never rg syntax.

`vault._ripgrep_search` used to run `[rg, "-c", "-i", "--type", "md", query, root]`.
The query is positional there, so one that begins with a dash is parsed by rg as an
option — and `--pre=<cmd>` runs <cmd> against every file in the vault. The query
reaches this function from the CLI (argparse forwards anything after `--`) and from
the MCP tool, i.e. from a model. The docstring also promised a *literal* match while
rg treated the query as a regex: `foo(` exited 2 and the lexical half of the recall
silently returned nothing.

These tests fake the subprocess boundary, because rg is not on PATH on every machine
that runs the suite, and assert the shape of the argv rather than any one hostile
query — the shape is what makes every such query inert.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from brain_mcp import vault
from conftest import memory


HOSTILE_QUERIES = [
    "--pre=calc.exe",          # rg option that executes a command per file
    "-e",                      # an option expecting an argument
    "--",                      # the option terminator itself
    "foo(",                    # invalid regex; rg exits 2
    "a.b*c[",                  # regex metacharacters that should match literally
    "",                        # empty pattern must not swallow the root as the pattern
]


def _fake_rg(monkeypatch: pytest.MonkeyPatch, captured: list[list[str]]):
    """Route shutil.which to a stub rg and capture what subprocess.run is handed."""
    monkeypatch.setattr(vault.shutil, "which", lambda name: "/fake/bin/rg" if name == "rg" else None)

    def run(argv, **kwargs):
        captured.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(vault.subprocess, "run", run)


@pytest.mark.parametrize("query", HOSTILE_QUERIES)
def test_query_is_passed_as_a_fixed_string_pattern_after_dash_e(
    vault_dir: Path, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    captured: list[list[str]] = []
    _fake_rg(monkeypatch, captured)

    vault._ripgrep_search(query, vault_dir)

    assert len(captured) == 1, "exactly one rg invocation per lexical search"
    argv = captured[0]
    assert argv[0] == "/fake/bin/rg"

    # The query appears exactly once, and only as the argument of `-e`.
    assert argv.count(query) == 1 or query in ("-e", "--"), argv
    e_at = argv.index("-e")
    assert argv[e_at + 1] == query, f"query must be the operand of -e: {argv}"

    # `--` terminates option parsing before the root, so the root is never
    # mistaken for a pattern and a dash-led query never for an option.
    sep_at = len(argv) - 1 - argv[::-1].index("--")
    assert sep_at > e_at + 1, f"`--` must come after the -e operand: {argv}"
    assert argv[sep_at + 1] == str(vault_dir)
    assert argv[-1] == str(vault_dir), "the root is the final argument"

    # Fixed-string matching: the docstring promises a literal match.
    assert "-F" in argv[:e_at], "-F must precede the pattern"
    # Case-insensitive, per-file counts — the ranking signal the merge relies on.
    assert "-i" in argv[:e_at]
    assert "-c" in argv[:e_at]


def test_options_all_precede_the_pattern_operand(
    vault_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No option may follow the pattern: anything after `-e <q>` is `--` then root.

    That ordering is what keeps a future edit from sliding an option (or, worse,
    the query) back into the positional tail.
    """
    captured: list[list[str]] = []
    _fake_rg(monkeypatch, captured)
    vault._ripgrep_search("windows setup", vault_dir)
    argv = captured[0]
    e_at = argv.index("-e")
    assert argv[e_at + 2:] == ["--", str(vault_dir)]
    assert all(a.startswith("-") for a in argv[1:e_at]
               if a not in ("md",)), f"non-option before -e: {argv}"


def test_count_output_is_parsed_from_the_rg_shape(
    vault_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rg -c` prints `path:count`; Windows paths contain colons, so rpartition."""
    hit = vault_dir / "user" / "x.md"
    memory(hit, "x", "user", "windows setup twice: windows setup")
    monkeypatch.setattr(vault.shutil, "which", lambda name: "/fake/bin/rg")

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=f"{hit}:2\n", stderr="")

    monkeypatch.setattr(vault.subprocess, "run", run)
    assert vault._ripgrep_search("windows setup", vault_dir) == {hit: 2}


@pytest.mark.parametrize("query", ["foo(", "--pre=calc.exe", "A.B*C["])
def test_python_fallback_counts_the_literal_substring_case_insensitively(
    vault_dir: Path, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    """Without rg the fallback must agree with `-F -i -c`: literal, case-folded.

    Keeping the two branches semantically identical is the point — a recall must
    not rank differently depending on whether the machine has rg installed.
    """
    monkeypatch.setattr(vault.shutil, "which", lambda name: None)
    hit = vault_dir / "user" / "hit.md"
    memory(hit, "hit", "user", f"lead line\n{query.upper()} and again {query.lower()}")
    memory(vault_dir / "user" / "miss.md", "miss", "user", "nothing relevant here")

    hits = vault._ripgrep_search(query, vault_dir)
    assert hits == {hit: 2}, hits
