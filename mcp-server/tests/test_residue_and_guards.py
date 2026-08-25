"""Smaller guards found in the same 2026-08-24 review.

Three defensive gaps and one asymmetry between install and uninstall. None of them
were breaking anything on the day, but each is the kind of thing that fails once, in
production, in a way that looks like something else.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from brain_mcp import vault

REPO_ROOT = Path(__file__).resolve().parents[2]

from conftest import memory


def test_forget_refuses_the_brain_directory_itself(populated_vault: Path) -> None:
    """The old guard *permitted* the one path it most needed to refuse.

    It read `if root not in parents and resolved != root: raise` — so passing the
    Brain directory satisfied the second clause, skipped the raise, and fell through
    to `unlink()` on a directory.
    """
    with pytest.raises((PermissionError, IsADirectoryError)):
        vault.forget_memory(str(populated_vault))
    assert populated_vault.is_dir(), "the Brain directory must still exist"


def test_forget_refuses_paths_outside_the_vault(populated_vault: Path, tmp_path: Path) -> None:
    outsider = tmp_path / "not-a-memory.md"
    outsider.write_text("hello\n", encoding="utf-8")
    with pytest.raises(PermissionError):
        vault.forget_memory(str(outsider))
    assert outsider.exists()


def test_forget_still_deletes_a_real_memory(populated_vault: Path) -> None:
    target = populated_vault / "user" / "prefers-rust.md"
    assert target.exists()
    vault.forget_memory(str(target))
    assert not target.exists()


def test_search_survives_a_file_deleted_mid_query(
    populated_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file can vanish between ripgrep listing it and the sort stat-ing it.

    Checkpoint rollups, `brain forget`, and Obsidian Sync deletes all do this. The
    mtime lookup used to sit bare inside a `sorted` key, so the OSError propagated
    out of search_memories — while every other failure path in that function
    deliberately degrades instead.
    """
    ghost = populated_vault / "user" / "ghost.md"

    real_search = vault._ripgrep_search

    def _search_returning_a_ghost(query, root):
        hits = real_search(query, root)
        hits[ghost] = 3  # ranked above real hits, so it is definitely stat-ed
        return hits

    monkeypatch.setattr(vault, "_ripgrep_search", _search_returning_a_ghost)

    results = vault.search_memories("Rust")
    assert isinstance(results, list)


def test_mcp_warmup_takes_the_reindex_lock() -> None:
    """The one unbounded sync that didn't take the cross-process lock.

    `sync()` only checks the lock on its foreground path, so an MCP server starting
    while a SessionStart-spawned reindex drained the backlog had two processes
    embedding the same files and contending for the write lock.
    """
    source = (Path(vault.__file__).resolve().parent / "server.py").read_text(encoding="utf-8")
    warmup = source[source.index("def _background_embed_warmup") :]
    warmup = warmup[: warmup.index("\nasync def ")]
    assert "acquire_reindex_lock" in warmup
    assert "release_reindex_lock" in warmup
    assert warmup.index("acquire_reindex_lock") < warmup.index("EmbedIndex.sync")


UNINSTALLERS = [
    "brain-uninstall.py",
    "uninstall-mac.sh",
    "uninstall-linux.sh",
    "uninstall-windows.ps1",
]


@pytest.mark.parametrize("uninstaller", UNINSTALLERS)
def test_uninstall_removes_the_permission_rule(uninstaller: str) -> None:
    """Install and uninstall must be symmetric.

    Every installer writes `permissions.allow -> Bash(<brain_cmd>:*)`. No uninstaller
    removed it, so uninstalling deleted the brain wrapper but left a standing
    unprompted Bash approval for that path behind in settings.json.

    Since 2026-08-25 symmetry is structural rather than duplicated: both halves call
    `brain_settings_merge`, so the rule predicate cannot drift between them. This
    test now guards the routing; `test_permission_rule_round_trips` below still
    exercises the behaviour.
    """
    src = (REPO_ROOT / uninstaller).read_text(encoding="utf-8")
    assert "brain_settings_merge" in src, (
        f"{uninstaller} no longer routes through the shared merge module, so its "
        f"allow-rule and hook-ownership predicates can drift from the installers'"
    )


def test_permission_rule_round_trips(tmp_path: Path) -> None:
    """Whatever an installer writes, an uninstaller must be able to remove.

    Asserted against the *real* predicates rather than a copy, so a change to the
    rule format cannot silently orphan a rule in a user's settings.json.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "brain_settings_merge_residue", REPO_ROOT / "brain_settings_merge.py"
    )
    merge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(merge)

    for brain_cmd in (
        "C:/Users/x/.claude/brain.cmd",
        'BRAIN_AGENT_SURFACE=1 BRAIN_VAULT="/home/x/Vaults/Ai-Brain" '
        '"/home/x/src/Ai-Brain/mcp-server/.venv/bin/brain"',
    ):
        settings = {"permissions": {"allow": ["Bash(git status:*)"]}}
        merge.merge_permission_rule(settings, brain_cmd)
        written = settings["permissions"]["allow"]
        for sub in merge.AGENT_SUBCOMMANDS:
            assert f"Bash({brain_cmd} {sub}:*)" in written

        removed = merge.prune_permission_rules(settings)
        assert removed == len(merge.AGENT_SUBCOMMANDS), (
            f"uninstall did not recognise every rule for {brain_cmd!r}"
        )
        assert settings.get("permissions", {}).get("allow", []) == ["Bash(git status:*)"], (
            "pruning must leave unrelated rules alone"
        )


def test_no_stale_claim_that_truncation_is_free() -> None:
    """embed.py contradicted itself about whether the slice budget helps.

    The SYNC_CHUNK comment said cost was "independent of body length … truncating
    bodies buys nothing" while the measurement table 430 lines above recorded a 27%
    rebuild saving from exactly that. A reader following the comment would conclude
    BRAIN_EMBED_CHARS is pointless.
    """
    src = (Path(vault.__file__).resolve().parent / "embed.py").read_text(encoding="utf-8")
    assert "truncating bodies buys nothing" not in src
    assert not re.search(r"independent of body length[^\n]*\n[^\n]*512-token", src)
