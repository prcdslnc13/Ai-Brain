"""The files the installer generates into a config dir, and how the uninstaller takes
them back out.

`test_installer_parity.py` asserts properties of the installer's *text*; these run its
functions against throwaway directories. Four findings from the 2026-09-01 security
review live here, each of which was a silent failure:

- F8: `render_global_claude_md` / `copy_brain_skill` overwrote a hand-written global
  CLAUDE.md or skill with no marker check and no backup.
- F21: `brain-launch.cmd` was written as UTF-8, but cmd.exe reads batch files in the
  OEM codepage, so a non-ASCII path set BRAIN_VAULT to a directory that does not
  exist and every hook failed silently; and a `%` in a baked path was consumed as a
  variable reference (`v%1x` spliced the hook's first argument in).
- F26: the uninstaller rmtree'd `skills/brain` without a marker check and printed
  "removed" even when rmtree refused; the installer's cleanup deleted `.mcp.json`
  without looking inside it.
- F2's upgrade path: an existing install has a `brain.cmd` (the command-injection
  wrapper) and permission rules naming it. A re-run must remove both, and the
  uninstaller must remove the legacy file too -- until now none of them did.

Nothing here touches a real config dir or the real vault.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import load_repo_script

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = REPO_ROOT / "templates"

sm = load_repo_script("brain_settings_merge.py")


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch):
    m = load_repo_script("brain-setup.py")
    # The worktree may have no venv; the launcher tests elsewhere run it for real.
    monkeypatch.setattr(m, "VENV_PY", Path(sys.executable))
    return m


@pytest.fixture
def uninstall():
    return load_repo_script("brain-uninstall.py")


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    d = tmp_path / "claude dir"
    d.mkdir()
    return d


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "Vaults" / "Ai Brain"
    (v / "Brain").mkdir(parents=True)
    return v


TOKEN = '"/repo/.venv/bin/python" "/home/x/.claude/brain-agent.py"'


def backups(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.brain-backup-*"))


# ------------------------------------------------------------------- F8 --------

def test_a_hand_written_global_claude_md_is_backed_up_not_clobbered(setup, cfg, vault, capsys):
    mine = cfg / "CLAUDE.md"
    mine.write_text("# My own rules\n\nNever use tabs.\n", encoding="utf-8")

    setup.render_global_claude_md(cfg, vault, TOKEN)

    kept = backups(cfg)
    assert len(kept) == 1, "an unmarked CLAUDE.md must be backed up before it is replaced"
    assert kept[0].read_text(encoding="utf-8") == "# My own rules\n\nNever use tabs.\n"
    rendered = mine.read_text(encoding="utf-8")
    assert sm.has_managed_marker(mine)
    assert TOKEN in rendered and str(vault) in rendered
    assert "__BRAIN_CMD__" not in rendered and "__BRAIN_VAULT__" not in rendered
    assert kept[0].name in capsys.readouterr().out, "the backup must be reported, not silent"


def test_a_hand_written_skill_is_backed_up_not_clobbered(setup, cfg):
    skill_dir = cfg / "skills" / "brain"
    skill_dir.mkdir(parents=True)
    theirs = skill_dir / "SKILL.md"
    theirs.write_text("---\nname: brain\n---\n# my skill\n", encoding="utf-8")

    setup.copy_brain_skill(cfg, TOKEN)

    kept = backups(skill_dir)
    assert len(kept) == 1
    assert kept[0].read_text(encoding="utf-8") == "---\nname: brain\n---\n# my skill\n"
    rendered = theirs.read_text(encoding="utf-8")
    assert rendered.startswith("---\n"), "the skill's YAML frontmatter must stay on line 1"
    assert sm.has_managed_marker(theirs)
    assert f"Bash({TOKEN} recall:*)" in rendered
    assert "__BRAIN_CMD__" not in rendered


def test_our_own_files_are_replaced_in_place_without_backups(setup, cfg, vault):
    for _ in range(3):
        setup.render_global_claude_md(cfg, vault, TOKEN)
        setup.copy_brain_skill(cfg, TOKEN)
    assert backups(cfg) == []
    assert backups(cfg / "skills" / "brain") == []


# ------------------------------------------------------------------- F21 -------

def test_the_batch_launcher_is_written_in_the_batch_codepage(setup, cfg, vault, monkeypatch):
    """cmd.exe reads .cmd files in the OEM codepage; an accented path must survive
    the round trip through THAT decoder, not UTF-8's."""
    accented = vault.parent / "Ai Brain é"
    accented.mkdir()
    monkeypatch.setattr(setup, "HOOKS_DIR", Path("/repo/hooks"))

    launch = setup.write_windows_launch_cmd(cfg, accented, encoding="cp437")

    raw = launch.read_bytes()
    text = raw.decode("cp437")
    assert f'set "BRAIN_VAULT={accented}"' in text
    assert raw != text.encode("utf-8"), "the file was written as UTF-8, which cmd.exe cannot read"
    assert b"\r\n" in raw, "batch files want CRLF"
    assert sm.has_managed_marker(launch) and sm.is_generated_launcher(launch)


def test_an_unencodable_path_fails_loudly_and_writes_nothing(setup, cfg, vault, monkeypatch):
    cyrillic = vault.parent / "Сумка"
    cyrillic.mkdir()
    monkeypatch.setattr(setup, "HOOKS_DIR", Path("/repo/hooks"))

    with pytest.raises(setup.LauncherError) as exc:
        setup.write_windows_launch_cmd(cfg, cyrillic, encoding="cp437")

    message = str(exc.value)
    assert "cp437" in message and "re-run" in message and "BRAIN_VAULT" in message
    assert not (cfg / setup.LAUNCH_CMD_NAME).exists()
    assert not list(cfg.glob("*.tmp"))


def test_an_unencodable_path_is_a_reported_settings_failure_not_a_crash(
    setup, cfg, vault, monkeypatch, capsys
):
    """On Windows the launcher is written from merge_settings_json; a failure there is
    a partial install the user is told about (exit 1 later), never a traceback."""
    cyrillic = vault.parent / "Сумка"
    cyrillic.mkdir()
    monkeypatch.setattr(setup, "IS_WINDOWS", True)
    monkeypatch.setattr(setup, "HOOKS_DIR", Path("/repo/hooks"))
    monkeypatch.setattr(setup, "batch_file_encoding", lambda: "cp437")

    ok, reason = setup.merge_settings_json(cfg, cyrillic, TOKEN)

    assert ok is False and "cp437" in reason
    assert not (cfg / "settings.json").exists(), "no hooks may be wired to a launcher that was not written"
    assert "Traceback" not in capsys.readouterr().err


def test_percent_signs_in_baked_paths_are_escaped_but_batch_syntax_is_not(setup, cfg, tmp_path, monkeypatch):
    """`v%1x` in a path would splice the hook's first argument into BRAIN_VAULT."""
    vault = tmp_path / "v%1x"
    vault.mkdir()
    monkeypatch.setattr(setup, "VENV_PY", Path(r"C:\Program Files (%PY%)\python.exe"))
    monkeypatch.setattr(setup, "HOOKS_DIR", Path(r"C:\src\100%\hooks"))

    text = setup.write_windows_launch_cmd(cfg, vault, encoding="cp437").read_text(encoding="cp437")

    assert 'set "BRAIN_VAULT=' + str(vault).replace("%", "%%") + '"' in text
    assert "(%%PY%%)" in text and r"100%%\hooks" in text
    assert text.count("%~1") == 1, "the hook-name argument must still expand"
    assert text.count("%ERRORLEVEL%") == 1, "the exit code must still expand"
    # Every remaining % is either doubled or part of one of the two batch tokens.
    stripped = text.replace("%%", "").replace("%~1", "").replace("%ERRORLEVEL%", "")
    assert "%" not in stripped, stripped


def test_batch_file_encoding_is_a_real_codec(setup, monkeypatch):
    import codecs

    assert codecs.lookup(setup.batch_file_encoding())
    monkeypatch.setattr(setup, "IS_WINDOWS", False)
    assert codecs.lookup(setup.batch_file_encoding())


# ------------------------------------------------------------------- F26 -------

def _mcp_json(cfg: Path, payload) -> Path:
    p = cfg / ".mcp.json"
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return p


def test_cleanup_deletes_only_a_brain_only_mcp_json(setup, cfg):
    ours = _mcp_json(cfg, {"mcpServers": {"brain": {
        "command": "/repo/.venv/bin/python", "args": ["-m", "brain_mcp"],
        "env": {"BRAIN_VAULT": "/v"},
    }}})
    setup.cleanup(cfg)
    assert not ours.exists()


@pytest.mark.parametrize("payload", [
    {"mcpServers": {"brain": {"command": "python", "args": ["-m", "brain_mcp"]},
                    "github": {"command": "gh-mcp"}}},
    {"mcpServers": {"github": {"command": "gh-mcp"}}},
    {"mcpServers": {"brain": {"command": "something-else-entirely"}}},
    {"mcpServers": {"brain": {"args": ["-m", "brain_mcp"]}}, "other": 1},
    "{ not json",
    "[]",
], ids=["shared", "foreign", "not-our-brain", "extra-keys", "unparseable", "list"])
def test_cleanup_leaves_any_other_mcp_json_alone(setup, cfg, payload, capsys):
    theirs = _mcp_json(cfg, payload)
    before = theirs.read_bytes()
    setup.cleanup(cfg)
    assert theirs.read_bytes() == before
    assert "leaving" in capsys.readouterr().out


def test_uninstall_leaves_an_unmarked_skill_alone(uninstall, cfg, capsys):
    skill_dir = cfg / "skills" / "brain"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: brain\n---\n# theirs\n", encoding="utf-8")

    uninstall.remove_brain_skill(cfg)

    assert (skill_dir / "SKILL.md").is_file()
    out = capsys.readouterr().out
    assert "leaving" in out and "removed" not in out


def test_uninstall_removes_our_skill_and_reports_what_is_on_disk(uninstall, setup, cfg, capsys):
    setup.copy_brain_skill(cfg, TOKEN)
    uninstall.remove_brain_skill(cfg)
    assert not (cfg / "skills").exists(), "an emptied skills/ dir is tidied away"
    assert "removed" in capsys.readouterr().out


def test_uninstall_does_not_claim_a_removal_that_did_not_happen(uninstall, setup, cfg, monkeypatch, capsys):
    setup.copy_brain_skill(cfg, TOKEN)

    def refuse(path, *args, **kwargs):
        raise PermissionError(13, "in use", str(path))

    monkeypatch.setattr(uninstall.shutil, "rmtree", refuse)
    uninstall.remove_brain_skill(cfg)
    out = capsys.readouterr().out
    assert (cfg / "skills" / "brain" / "SKILL.md").is_file()
    assert "still present" in out and "✓ removed" not in out


# ------------------------------------------------------ F2 upgrade + uninstall --

LEGACY_BRAIN_CMD = (
    "@echo off\r\n"
    "rem Generated by setup-windows.ps1 - do not edit by hand. Re-run setup-windows.ps1 to regenerate.\r\n"
    "setlocal\r\n"
    'set "BRAIN_VAULT=C:\\Vaults\\Ai-Brain"\r\n'
    '"C:\\repo\\.venv\\Scripts\\brain.exe" %*\r\n'
)


def test_a_rerun_removes_the_injection_wrapper_and_its_rules(setup, cfg, vault, capsys):
    """An existing install has brain.cmd on disk and `Bash(<cfg>/brain.cmd <sub>:*)`
    rules pre-approving it. The upgrade is `brain-setup.py` run again; afterwards
    neither may remain, or the hole stays open on every machine that already had
    the Brain."""
    legacy = cfg / "brain.cmd"
    legacy.write_text(LEGACY_BRAIN_CMD, encoding="utf-8")
    legacy_rules = [f"Bash(C:/Users/x/.claude/brain.cmd {sub}:*)" for sub in sm.AGENT_SUBCOMMANDS]
    (cfg / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash(git status:*)", *legacy_rules]},
    }), encoding="utf-8")

    token = setup.brain_cmd_token(cfg, vault)
    ok, _ = setup.merge_settings_json(cfg, vault, token)
    setup.cleanup(cfg)

    assert ok
    allow = json.loads((cfg / "settings.json").read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(git status:*)" in allow
    assert not [r for r in allow if "brain.cmd" in r], allow
    assert [r for r in allow if "brain-agent.py" in r], allow
    assert not legacy.exists(), "the legacy wrapper must be deleted, not left inert"
    assert "removed legacy brain.cmd" in capsys.readouterr().out


def test_cleanup_leaves_a_brain_cmd_it_cannot_identify(setup, cfg, capsys):
    theirs = cfg / "brain.cmd"
    theirs.write_text("@echo off\r\nrem my own thing\r\n", encoding="utf-8")
    setup.cleanup(cfg)
    assert theirs.exists()
    assert "leaving it alone" in capsys.readouterr().err


def test_uninstall_removes_every_generated_launcher_including_the_legacy_one(uninstall, setup, cfg, vault, capsys):
    setup.write_agent_launcher(cfg, vault)
    setup.write_windows_launch_cmd(cfg, vault, encoding="ascii")
    (cfg / "brain.cmd").write_text(LEGACY_BRAIN_CMD, encoding="utf-8")
    theirs = cfg / "brain-agent.py"  # overwrite ours with an unmarked user file
    theirs.write_text("print('mine')\n", encoding="utf-8")

    uninstall.remove_generated_launchers(cfg)

    assert not (cfg / "brain-launch.cmd").exists()
    assert not (cfg / "brain.cmd").exists()
    assert theirs.is_file(), "a file we cannot identify as ours is not ours to delete"
    out = capsys.readouterr().out
    assert "removed brain.cmd" in out and "removed brain-launch.cmd" in out
    assert "brain-agent.py does not say it was generated" in out


def test_uninstall_step_list_is_the_same_on_every_platform(uninstall):
    """The launcher step used to be Windows-only, so a POSIX uninstall left
    brain-agent.py behind (and a Mac that once ran the Windows installer over a
    shared home kept its brain.cmd forever)."""
    src = (REPO_ROOT / "brain-uninstall.py").read_text(encoding="utf-8")
    assert "if IS_WINDOWS:\n        step(" not in src
    assert "remove_generated_launchers(claude_dir)" in src


def test_install_then_uninstall_returns_the_config_dir_to_empty(setup, uninstall, cfg, vault, monkeypatch):
    """Symmetry, end to end, through the real functions on the real platform."""
    monkeypatch.setattr(uninstall, "IS_WINDOWS", setup.IS_WINDOWS)
    token = setup.brain_cmd_token(cfg, vault)
    setup.render_global_claude_md(cfg, vault, token)
    setup.copy_brain_skill(cfg, token)
    ok, reason = setup.merge_settings_json(cfg, vault, token)
    assert ok, reason
    setup.cleanup(cfg)
    assert (cfg / setup.AGENT_LAUNCHER_NAME).is_file()

    uninstall.prune_settings_hooks(cfg)
    uninstall.remove_managed_claude_md(cfg)
    uninstall.remove_brain_skill(cfg)
    uninstall.remove_generated_launchers(cfg)

    leftovers = {p.name for p in cfg.rglob("*") if ".brain-backup-" not in p.name}
    assert leftovers == {"settings.json"}, leftovers
    assert json.loads((cfg / "settings.json").read_text(encoding="utf-8")) == {}
