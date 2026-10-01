"""Uninstall puts back the user's own files that install backed up.

Install replaces a hand-written CLAUDE.md or skills/brain/SKILL.md and keeps the
original as `<name>.brain-backup-<stamp>`. Uninstall used to delete the managed copy
and leave the user with no file and a backup they never heard about -- and for the
skill it rmtree'd the whole directory, backup included.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import load_repo_script

MARKER_TEXT = "<!-- managed-by: ai-brain -->\n# Brain\n"


@pytest.fixture
def un():
    mod = load_repo_script("brain-uninstall.py")
    mod.info = lambda msg: None
    return mod


def test_claude_md_backup_is_restored(un, tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "CLAUDE.md").write_text(MARKER_TEXT, encoding="utf-8")
    (cfg / "CLAUDE.md.brain-backup-20260101-090000").write_text("old mine\n", encoding="utf-8")
    (cfg / "CLAUDE.md.brain-backup-20260201-090000").write_text("my rules\n", encoding="utf-8")

    un.remove_managed_claude_md(cfg)

    assert (cfg / "CLAUDE.md").read_text(encoding="utf-8") == "my rules\n", "newest user backup"
    assert (cfg / "CLAUDE.md.brain-backup-20260201-090000").exists(), "the backup is kept"


def test_no_backup_means_no_file(un, tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "CLAUDE.md").write_text(MARKER_TEXT, encoding="utf-8")
    un.remove_managed_claude_md(cfg)
    assert not (cfg / "CLAUDE.md").exists()


def test_a_managed_backup_is_not_restored(un, tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "CLAUDE.md").write_text(MARKER_TEXT, encoding="utf-8")
    (cfg / "CLAUDE.md.brain-backup-20260301-090000").write_text(MARKER_TEXT, encoding="utf-8")
    un.remove_managed_claude_md(cfg)
    assert not (cfg / "CLAUDE.md").exists()


def test_skill_removal_keeps_the_users_backup_and_restores_it(un, tmp_path: Path) -> None:
    skill = tmp_path / "cfg" / "skills" / "brain"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: brain\n---\n" + MARKER_TEXT, encoding="utf-8")
    (skill / "SKILL.md.brain-backup-20260101-090000").write_text("my own skill\n", encoding="utf-8")

    un.remove_brain_skill(tmp_path / "cfg")

    assert (skill / "SKILL.md").read_text(encoding="utf-8") == "my own skill\n"
    assert (skill / "SKILL.md.brain-backup-20260101-090000").exists()


def test_skill_dir_with_other_files_is_left_in_place(un, tmp_path: Path) -> None:
    skill = tmp_path / "cfg" / "skills" / "brain"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: brain\n---\n" + MARKER_TEXT, encoding="utf-8")
    (skill / "notes.txt").write_text("mine\n", encoding="utf-8")

    un.remove_brain_skill(tmp_path / "cfg")

    assert not (skill / "SKILL.md").exists()
    assert (skill / "notes.txt").exists(), "files we never wrote are not ours to delete"


def test_a_lone_managed_skill_is_removed_entirely(un, tmp_path: Path) -> None:
    skill = tmp_path / "cfg" / "skills" / "brain"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: brain\n---\n" + MARKER_TEXT, encoding="utf-8")
    un.remove_brain_skill(tmp_path / "cfg")
    assert not skill.exists() and not (tmp_path / "cfg" / "skills").exists()
