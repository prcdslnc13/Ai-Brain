"""Uninstall must not delete the shared venv while another config dir still uses it.

The sibling check scanned only `~/.claude*`, but `brain-setup.py` accepts any path as
a config dir: uninstalling `~/.claude-personal` removed the venv out from under an
install in, say, `D:/cfg/work`, and every hook there failed silently. Setup now
records each config dir in `.brain-installs.json` at the repo root, and uninstall
checks those, `~/.claude*` and `$CLAUDE_CONFIG_DIR` before deleting anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import load_repo_script


@pytest.fixture
def uninstall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mod = load_repo_script("brain-uninstall.py")
    repo = tmp_path / "repo"
    venv = repo / "mcp-server" / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(mod, "REPO_DIR", repo)
    monkeypatch.setattr(mod, "VENV_DIR", venv)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    mod.info = lambda msg: None
    return mod


def _install_into(cfg: Path, venv: Path) -> Path:
    cfg.mkdir(parents=True)
    (cfg / "settings.json").write_text(
        '{"hooks": {"Stop": [{"hooks": [{"command": "\\"%s/Scripts/python.exe\\" stop.py"}]}]}}'
        % venv.as_posix(), encoding="utf-8")
    return cfg


def test_a_recorded_install_outside_home_keeps_the_venv(uninstall, tmp_path: Path) -> None:
    sibling = _install_into(tmp_path / "cfg" / "work", uninstall.VENV_DIR)
    uninstall.brain_settings_merge.record_install(uninstall.REPO_DIR, sibling)

    uninstall.remove_venv([tmp_path / "home" / ".claude-personal"])

    assert uninstall.VENV_DIR.exists(), "the venv was deleted while a recorded install used it"


def test_claude_config_dir_is_checked_too(uninstall, tmp_path: Path, monkeypatch) -> None:
    sibling = _install_into(tmp_path / "elsewhere", uninstall.VENV_DIR)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(sibling))

    uninstall.remove_venv([])

    assert uninstall.VENV_DIR.exists()


def test_an_unreferenced_venv_is_removed(uninstall, tmp_path: Path) -> None:
    stale = tmp_path / "cfg" / "old"
    stale.mkdir(parents=True)  # recorded once, no longer references the venv
    uninstall.brain_settings_merge.record_install(uninstall.REPO_DIR, stale)

    uninstall.remove_venv([])

    assert not uninstall.VENV_DIR.exists()


def test_the_registry_round_trips(uninstall, tmp_path: Path) -> None:
    sm = uninstall.brain_settings_merge
    a, b = tmp_path / "a", tmp_path / "b"
    sm.record_install(uninstall.REPO_DIR, a)
    sm.record_install(uninstall.REPO_DIR, a)
    sm.record_install(uninstall.REPO_DIR, b)
    assert sorted(map(str, sm.recorded_installs(uninstall.REPO_DIR))) == sorted([str(a), str(b)])
    sm.forget_installs(uninstall.REPO_DIR, [a])
    assert sm.recorded_installs(uninstall.REPO_DIR) == [b]
    assert (uninstall.REPO_DIR / sm.INSTALLS_FILE).read_bytes().count(b"\r") == 0


def test_an_unreadable_registry_adds_nothing(uninstall) -> None:
    (uninstall.REPO_DIR / uninstall.brain_settings_merge.INSTALLS_FILE).write_text("{nope", encoding="utf-8")
    assert uninstall.brain_settings_merge.recorded_installs(uninstall.REPO_DIR) == []


def test_setup_records_the_config_dir_before_any_wiring() -> None:
    src = (Path(__file__).resolve().parents[2] / "brain-setup.py").read_text(encoding="utf-8")
    body = src[src.index("def install_one("):]
    assert body.index("record_install(") < body.index("render_global_claude_md("), (
        "a half-finished install must already be on record"
    )
