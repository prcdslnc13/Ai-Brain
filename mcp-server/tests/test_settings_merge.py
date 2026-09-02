"""Behavioural tests for the shared settings.json merge/prune.

`test_installer_parity.py` asserts the four installers stay textually in sync; it
cannot assert what they *do*, because running them for real needs four operating
systems. Since 2026-08-25 they all shell out to (or import) one stdlib-only module,
`brain_settings_merge.py`, so the behaviour is testable exactly once — right here.

Two regressions these tests exist to prevent, both silent and both destructive:

1. The installers assigned `settings["hooks"][event] = definition` after pruning
   Brain-owned entries, which deleted every third-party group registered for that
   event. A user with their own SessionStart hook lost it to a Brain install.
2. Unparseable JSON was caught, treated as `{}`, and written back — an installer
   erasing the user's entire Claude configuration to add a hook block.

Nothing here touches a real config dir; every case builds its own tmp_path.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "brain_settings_merge.py"
POSIX_TEMPLATE = REPO_ROOT / "templates" / "settings.hooks.json"
WIN_TEMPLATE = REPO_ROOT / "templates" / "settings.hooks.win.json"


def _load_module():
    """Import by path: the module lives at the repo root, which is not on sys.path.

    (conftest puts `mcp-server/` and `hooks/` there, deliberately — see its docstring.)
    """
    spec = importlib.util.spec_from_file_location("brain_settings_merge", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("brain_settings_merge", module)
    spec.loader.exec_module(module)
    return module


sm = _load_module()

BRAIN_PYTHON = "/repo/mcp-server/.venv/bin/python"
BRAIN_HOOKS = "/repo/hooks"
BRAIN_VAULT = "/vault/Ai-Brain"
# The current approved-command shape: two quoted paths, no env prefix, no .cmd.
BRAIN_CMD = f'"{BRAIN_PYTHON}" "/home/x/.claude/brain-agent.py"'

HOOK_EVENTS = sorted(json.loads(POSIX_TEMPLATE.read_text(encoding="utf-8"))["hooks"])


def posix_merge(settings_path: Path) -> dict:
    return sm.merge(
        settings_path,
        POSIX_TEMPLATE,
        brain_cmd=BRAIN_CMD,
        brain_python=BRAIN_PYTHON,
        brain_hooks=BRAIN_HOOKS,
        brain_vault=BRAIN_VAULT,
    )


def third_party_settings() -> dict:
    """A settings.json with somebody else's hook on EVERY event we install into."""
    return {
        "model": "opus",
        "env": {"FOO": "bar"},
        "hooks": {
            event: [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": f"/usr/local/bin/their-{event}.sh"}
                    ],
                }
            ]
            for event in HOOK_EVENTS
        },
        "permissions": {"allow": ["Bash(ls:*)", "Read(//tmp/**)"]},
    }


def write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def all_commands(settings: dict, event: str) -> list[str]:
    out = []
    for group in settings["hooks"].get(event, []):
        for hook in group.get("hooks", []):
            out.append(hook.get("command", ""))
    return out


# ------------------------------------------------------ preserve third parties --

def test_third_party_hooks_survive_two_merges(tmp_path):
    """The headline regression: install must not evict a user's own hooks.

    Run twice, because "append instead of assign" and "idempotent" pull in opposite
    directions — the naive fix duplicates the Brain block on every re-run.
    """
    settings_path = write_json(tmp_path / "settings.json", third_party_settings())

    posix_merge(settings_path)
    posix_merge(settings_path)

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    for event in HOOK_EVENTS:
        commands = all_commands(settings, event)
        theirs = [c for c in commands if "their-" in c]
        ours = [c for c in commands if BRAIN_HOOKS in c]
        assert theirs == [f"/usr/local/bin/their-{event}.sh"], (
            f"{event}: third-party hook lost or duplicated: {commands}"
        )
        assert len(ours) == 1, f"{event}: expected exactly one Brain hook, got {ours}"


def test_every_template_event_is_installed(tmp_path):
    settings_path = tmp_path / "settings.json"
    report = posix_merge(settings_path)
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert sorted(settings["hooks"]) == HOOK_EVENTS
    assert report["events"] == HOOK_EVENTS


def test_unrelated_top_level_settings_are_untouched(tmp_path):
    settings_path = write_json(tmp_path / "settings.json", third_party_settings())
    posix_merge(settings_path)
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["model"] == "opus"
    assert settings["env"] == {"FOO": "bar"}
    assert "Bash(ls:*)" in settings["permissions"]["allow"]
    assert "Read(//tmp/**)" in settings["permissions"]["allow"]


def test_multiple_groups_matchers_and_inner_hooks_all_survive(tmp_path):
    """Events can hold several groups, each with a matcher and several inner hooks."""
    settings_path = write_json(tmp_path / "settings.json", {
        "hooks": {
            "Stop": [
                {"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "a.sh"},
                    {"type": "command", "command": "b.sh"},
                ]},
                {"matcher": "Edit", "hooks": [{"type": "command", "command": "c.sh"}]},
                {"hooks": [{"type": "command", "command": "d.sh"}]},
            ],
        },
    })
    posix_merge(settings_path)
    posix_merge(settings_path)

    stop = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]["Stop"]
    assert stop[0]["matcher"] == "Bash"
    assert [h["command"] for h in stop[0]["hooks"]] == ["a.sh", "b.sh"]
    assert stop[1]["matcher"] == "Edit"
    assert [h["command"] for h in stop[1]["hooks"]] == ["c.sh"]
    assert [h["command"] for h in stop[2]["hooks"]] == ["d.sh"]
    assert len(stop) == 4, "the Brain group should be appended exactly once"
    assert BRAIN_HOOKS in stop[3]["hooks"][0]["command"]


def test_malformed_non_brain_entries_are_passed_through(tmp_path):
    """Somebody else's malformed config is not ours to normalize or delete."""
    settings_path = write_json(tmp_path / "settings.json", {
        "hooks": {
            "Stop": ["a bare string", {"hooks": "not-a-list"},
                     {"hooks": [{"type": "command", "command": "keep.sh"}]}],
        },
    })
    posix_merge(settings_path)
    stop = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]["Stop"]
    assert stop[0] == "a bare string"
    assert stop[1] == {"hooks": "not-a-list"}
    assert stop[2]["hooks"][0]["command"] == "keep.sh"


def test_permission_rules_are_added_once_and_only_once(tmp_path):
    """One narrow rule per agent subcommand, and a re-run must not accumulate them."""
    settings_path = write_json(tmp_path / "settings.json", third_party_settings())
    posix_merge(settings_path)
    posix_merge(settings_path)
    allow = json.loads(settings_path.read_text(encoding="utf-8"))["permissions"]["allow"]
    brain_rules = [r for r in allow if "brain" in r.lower()]
    assert brain_rules == [
        f"Bash({BRAIN_CMD} {sub}:*)" for sub in sm.AGENT_SUBCOMMANDS
    ]


def test_no_blanket_permission_rule_is_written(tmp_path):
    """The superseded shape pre-approved the whole CLI, including every subcommand
    added in future. It must not come back."""
    settings_path = write_json(tmp_path / "settings.json", third_party_settings())
    posix_merge(settings_path)
    allow = json.loads(settings_path.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert f"Bash({BRAIN_CMD}:*)" not in allow
    assert "reindex" not in " ".join(allow)


# Every approved-command prefix ever written, oldest first. The predicate must keep
# recognising all of them: prune runs before re-add, so a shape it misses is a stale
# standing approval that survives every re-install -- and the two middle ones are
# the command-injection wrapper and the never-matching env prefix, respectively.
EVERY_PREFIX_EVER = [
    'BRAIN_VAULT="/old/vault" "/old/.venv/bin/brain"',
    "C:/Users/x/.claude/brain.cmd",
    'BRAIN_AGENT_SURFACE=1 BRAIN_VAULT="/v" "/repo/.venv/bin/brain"',
    '"C:/repo/.venv/Scripts/python.exe" "C:/Users/x/.claude/brain-agent.py"',
    '"/repo/.venv/bin/python" "/home/x/.claude/brain-agent.py"',
]


@pytest.mark.parametrize("prefix", EVERY_PREFIX_EVER)
def test_every_rule_shape_ever_written_is_recognised(prefix) -> None:
    for sub in sm.AGENT_SUBCOMMANDS:
        assert sm.is_brain_permission_rule(f"Bash({prefix} {sub}:*)"), prefix
    assert sm.is_brain_permission_rule(f"Bash({prefix}:*)"), f"blanket form of {prefix}"


@pytest.mark.parametrize("rule", [
    "Bash(git status:*)", "Bash(ls:*)", "Read(//tmp/**)",
    "Bash(python brain_things.py:*)", "Bash(cat ~/.claude/brain-notes.md:*)",
])
def test_foreign_rules_are_not_claimed(rule) -> None:
    assert not sm.is_brain_permission_rule(rule)


@pytest.mark.parametrize(
    "legacy",
    [
        'Bash(BRAIN_VAULT="/old/vault" "/old/.venv/bin/brain":*)',
        "Bash(C:/Users/x/.claude/brain.cmd:*)",
        "Bash(C:/Users/x/.claude/brain.cmd recall:*)",
        'Bash(BRAIN_AGENT_SURFACE=1 BRAIN_VAULT="/v" "/old/.venv/bin/brain" save:*)',
        'Bash("/old/.venv/bin/python" "/old/.claude/brain-agent.py" save:*)',
    ],
    ids=["posix-blanket", "windows-blanket", "windows-cmd-per-sub", "posix-env-prefix",
         "launcher-at-old-path"],
)
def test_superseded_permission_rules_are_pruned(tmp_path, legacy):
    """Prune runs before re-add, so a shape the predicate misses is a stale standing
    approval -- often for a path that no longer exists -- that survives every re-install.
    The `.cmd` rules are the ones that matter most: an existing install upgrades by
    re-running setup, and the injection hole stays open until they are gone."""
    base = third_party_settings()
    base["permissions"]["allow"].append(legacy)
    settings_path = write_json(tmp_path / "settings.json", base)
    posix_merge(settings_path)
    allow = json.loads(settings_path.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert legacy not in allow
    assert "Bash(ls:*)" in allow


def test_windows_template_merges_and_is_idempotent(tmp_path):
    """The Windows path renders a different command shape; ownership must still match."""
    settings_path = write_json(tmp_path / "settings.json", third_party_settings())
    launch = r"C:\Users\x\.claude\brain-launch.cmd"
    for _ in range(2):
        sm.merge(
            settings_path,
            WIN_TEMPLATE,
            brain_cmd='"C:/repo/.venv/Scripts/python.exe" "C:/Users/x/.claude/brain-agent.py"',
            brain_launch=launch,
        )
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    for event in HOOK_EVENTS:
        commands = all_commands(settings, event)
        assert len([c for c in commands if "brain-launch" in c]) == 1
        assert [c for c in commands if "their-" in c]
        # Forward slashes: Git Bash eats single backslashes out of hook commands.
        # (Checked on the commands themselves -- the JSON encoding of the quotes
        # around the launcher path is a backslash too, and a legitimate one.)
        for command in commands:
            assert "\\" not in command, command


def test_placeholders_are_substituted_on_the_parsed_structure():
    """A backslash path spliced into JSON *text* is an escape sequence: `D:\\new\\tab`
    became a newline and a tab, and a `\\U` made the template unparseable. The
    substitution has to happen on parsed strings, where a path is just a value."""
    block = sm.render_hooks_template(
        POSIX_TEMPLATE.read_text(encoding="utf-8"),
        brain_python=r"D:\new\tab\python.exe",
        brain_hooks=r"D:\Users\x\hooks",
        brain_vault=r'D:\v"q\Ai-Brain',
    )
    command = block["Stop"][0]["hooks"][0]["command"]
    assert r"D:\new\tab\python.exe" in command
    assert r"D:\Users\x\hooks" in command
    assert r'D:\v"q\Ai-Brain' in command
    assert "\n" not in command and "\t" not in command
    # The Windows launcher path is still forward-slashed (Git Bash eats backslashes).
    win = sm.render_hooks_template(
        WIN_TEMPLATE.read_text(encoding="utf-8"), brain_launch=r"C:\Users\Jo Bloggs\.claude\brain-launch.cmd"
    )
    assert win["Stop"][0]["hooks"][0]["command"] == '"C:/Users/Jo Bloggs/.claude/brain-launch.cmd" stop'


def test_an_unparseable_template_is_a_settings_error(tmp_path):
    with pytest.raises(sm.SettingsError, match="template"):
        sm.render_hooks_template("{ not json")
    with pytest.raises(sm.SettingsError, match="hooks"):
        sm.render_hooks_template('["no hooks object"]')


def test_settings_are_written_as_utf8_not_ascii_escapes(tmp_path):
    """`ensure_ascii=True` turned an accented username into `\\u00e9` sequences that
    are valid JSON but unreadable in the file the user is told to inspect."""
    settings_path = tmp_path / "settings.json"
    sm.merge(
        settings_path, POSIX_TEMPLATE, brain_cmd=BRAIN_CMD,
        brain_python=BRAIN_PYTHON, brain_hooks=BRAIN_HOOKS, brain_vault="/home/josé/Vaults",
    )
    raw = settings_path.read_bytes()
    assert "josé".encode("utf-8") in raw
    assert b"\\u00e9" not in raw


# --------------------------------------------------------------- uninstall ----

def test_prune_removes_only_brain_owned_entries(tmp_path):
    settings_path = write_json(tmp_path / "settings.json", third_party_settings())
    posix_merge(settings_path)

    report = sm.prune(settings_path, brain_hooks=BRAIN_HOOKS)

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert report["removed"] == len(HOOK_EVENTS) + len(sm.AGENT_SUBCOMMANDS)
    for event in HOOK_EVENTS:
        assert all_commands(settings, event) == [f"/usr/local/bin/their-{event}.sh"]
    assert settings["permissions"]["allow"] == ["Bash(ls:*)", "Read(//tmp/**)"]
    assert settings["model"] == "opus"


def test_install_then_uninstall_restores_the_original_file(tmp_path):
    """Symmetry, asserted end to end: what install adds, uninstall takes back out."""
    original = third_party_settings()
    settings_path = write_json(tmp_path / "settings.json", original)
    posix_merge(settings_path)
    sm.prune(settings_path, brain_hooks=BRAIN_HOOKS)
    assert json.loads(settings_path.read_text(encoding="utf-8")) == original


def test_prune_drops_an_empty_hooks_block(tmp_path):
    settings_path = tmp_path / "settings.json"
    posix_merge(settings_path)
    sm.prune(settings_path, brain_hooks=BRAIN_HOOKS)
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "hooks" not in settings
    assert "permissions" not in settings


def test_prune_leaves_an_unparseable_file_alone(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{ this is not json", encoding="utf-8")
    before = settings_path.read_bytes()
    with pytest.raises(sm.SettingsError):
        sm.prune(settings_path, brain_hooks=BRAIN_HOOKS)
    assert settings_path.read_bytes() == before
    # The CLI turns that into a note, not a failure: uninstall must never abort.
    assert sm.main(["prune", "--settings", str(settings_path)]) == 0
    assert settings_path.read_bytes() == before


# ------------------------------------------------- refuse to clobber garbage --

BAD_INPUTS = {
    "malformed": '{"hooks": {,}}',
    "truncated": '{\n  "permissions": {\n    "allow": ["Bash(ls:*)"',
    "json_list": '["not", "an", "object"]\n',
    "bare_string": '"just a string"\n',
    "json_null": "null\n",
    "json_number": "42\n",
    "trailing_comma": '{"model": "opus",}\n',
}


@pytest.mark.parametrize("label", sorted(BAD_INPUTS))
def test_unsafe_settings_are_left_byte_identical(tmp_path, label, capsys):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(BAD_INPUTS[label], encoding="utf-8")
    before = settings_path.read_bytes()

    with pytest.raises(sm.SettingsError):
        posix_merge(settings_path)
    assert settings_path.read_bytes() == before

    rc = sm.main([
        "merge",
        "--settings", str(settings_path),
        "--template", str(POSIX_TEMPLATE),
        "--brain-cmd", BRAIN_CMD,
        "--brain-python", BRAIN_PYTHON,
        "--brain-hooks", BRAIN_HOOKS,
        "--brain-vault", BRAIN_VAULT,
    ])
    assert rc == sm.EXIT_SETTINGS_UNSAFE
    assert settings_path.read_bytes() == before

    err = capsys.readouterr().err
    assert str(settings_path) in err, "the failure must name the file"
    assert "re-run setup" in err, "the failure must say how to repair it"
    # No temp file left behind next to the settings file.
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_non_utf8_settings_are_refused(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_bytes(b'{"model": "\xff\xfe opus"}')
    before = settings_path.read_bytes()
    with pytest.raises(sm.SettingsError):
        posix_merge(settings_path)
    assert settings_path.read_bytes() == before


@pytest.mark.parametrize("content", ["", "   \n\t\n  "])
def test_empty_and_whitespace_only_files_are_treated_as_empty_settings(tmp_path, content):
    """Safe to overwrite: there is nothing in them to lose, and the installers used
    to create exactly this file themselves when settings.json was missing."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(content, encoding="utf-8")
    posix_merge(settings_path)
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert sorted(settings["hooks"]) == HOOK_EVENTS


def test_missing_file_is_created(tmp_path):
    settings_path = tmp_path / "nested" / "settings.json"
    posix_merge(settings_path)
    assert json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]


def test_bom_prefixed_settings_are_parsed_not_refused(tmp_path):
    """PowerShell 5.1's `Set-Content -Encoding UTF8` writes a BOM; a real user's
    settings.json can carry one, and refusing it would be a false alarm."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"model": "opus"}).encode())
    posix_merge(settings_path)
    assert json.loads(settings_path.read_text(encoding="utf-8"))["model"] == "opus"


# ------------------------------------------------------------ backup + write --

def test_a_backup_exists_before_a_successful_mutation(tmp_path):
    settings_path = write_json(tmp_path / "settings.json", third_party_settings())
    original = settings_path.read_bytes()

    report = posix_merge(settings_path)

    assert report["wrote"] is True
    backup = Path(report["backup"])
    assert backup.is_file(), "a mutation of an existing settings.json must be backed up"
    assert backup.read_bytes() == original
    assert settings_path.read_bytes() != original


def test_a_no_op_rerun_writes_nothing_and_takes_no_backup(tmp_path):
    """Otherwise every re-run of setup litters the config dir with identical copies."""
    settings_path = write_json(tmp_path / "settings.json", third_party_settings())
    posix_merge(settings_path)
    after_first = settings_path.read_bytes()
    backups_after_first = sorted(p.name for p in tmp_path.glob("*.brain-backup-*"))

    report = posix_merge(settings_path)

    assert report["wrote"] is False
    assert report["backup"] == ""
    assert settings_path.read_bytes() == after_first
    assert sorted(p.name for p in tmp_path.glob("*.brain-backup-*")) == backups_after_first


def test_no_backup_for_a_file_with_nothing_to_lose(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("   \n", encoding="utf-8")
    report = posix_merge(settings_path)
    assert report["backup"] == ""
    assert not list(tmp_path.glob("*.brain-backup-*"))


def test_a_failed_replace_leaves_the_original_intact(tmp_path, monkeypatch):
    """The reason the write is a temp file + os.replace and not an open('w').

    A crash between truncate and write leaves a settings.json that Claude Code cannot
    parse and the user cannot recover — the exact file this module refuses to touch.
    """
    settings_path = write_json(tmp_path / "settings.json", third_party_settings())
    original = settings_path.read_bytes()

    def boom(src, dst):
        raise OSError(13, "permission denied")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        posix_merge(settings_path)

    assert settings_path.read_bytes() == original
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, f"temp file left behind: {leftovers}"


def test_cli_reports_a_write_failure_without_losing_the_file(tmp_path, monkeypatch, capsys):
    settings_path = write_json(tmp_path / "settings.json", third_party_settings())
    original = settings_path.read_bytes()
    monkeypatch.setattr(os, "replace", lambda src, dst: (_ for _ in ()).throw(OSError("nope")))

    rc = sm.main([
        "merge",
        "--settings", str(settings_path),
        "--template", str(POSIX_TEMPLATE),
        "--brain-cmd", BRAIN_CMD,
        "--brain-python", BRAIN_PYTHON,
        "--brain-hooks", BRAIN_HOOKS,
        "--brain-vault", BRAIN_VAULT,
    ])
    assert rc == sm.EXIT_SETTINGS_UNSAFE
    assert settings_path.read_bytes() == original
    assert "left unchanged" in capsys.readouterr().err


def test_settings_are_written_with_lf_endings(tmp_path):
    settings_path = tmp_path / "settings.json"
    posix_merge(settings_path)
    assert b"\r\n" not in settings_path.read_bytes()


# ------------------------------------------------------ ownership predicate ---

@pytest.mark.parametrize("command", [
    "BRAIN_VAULT=/v /repo/.venv/bin/python /repo/hooks/stop.py",
    "C:/Users/x/.claude/brain-launch.cmd stop",
    r"C:\Users\x\.claude\brain-launch.cmd stop",
    "C:/repo/hooks/stop.py",
    r"C:\repo\HOOKS\stop.py",
])
def test_brain_commands_are_recognised(command):
    assert sm.is_brain_command(command, hooks_dir="C:/repo/hooks")


@pytest.mark.parametrize("command", [
    "/usr/local/bin/their-hook.sh",
    "echo hello",
    "npx some-linter --fix",
    "",
    None,
    42,
])
def test_foreign_commands_are_not_claimed(command):
    assert not sm.is_brain_command(command, hooks_dir="/repo/hooks")


def test_installed_commands_are_always_self_recognised(tmp_path):
    """Guard against a template edit that outruns the ownership predicate.

    If a command we install is not detectable as ours, the *next* install cannot
    prune it and the user silently accumulates a second set of Brain hooks per run.
    Fail at install time instead of leaking duplicates forever.
    """
    for template, kwargs in (
        (POSIX_TEMPLATE, {"brain_python": BRAIN_PYTHON, "brain_hooks": BRAIN_HOOKS,
                          "brain_vault": BRAIN_VAULT}),
        (WIN_TEMPLATE, {"brain_launch": "C:/x/.claude/brain-launch.cmd"}),
    ):
        block = sm.render_hooks_template(
            template.read_text(encoding="utf-8"), **kwargs
        )
        sm._assert_block_is_ownable(
            block, kwargs.get("brain_hooks", ""), kwargs.get("brain_launch", "")
        )


# ------------------------------------------------------- managed files ---------

MARKED = f"{sm.MANAGED_MARKER}\n# ours\n"


def test_has_managed_marker_reads_only_the_head(tmp_path):
    ours = tmp_path / "CLAUDE.md"
    ours.write_text(MARKED, encoding="utf-8")
    assert sm.has_managed_marker(ours)

    frontmatter = tmp_path / "SKILL.md"
    frontmatter.write_text(f"---\nname: brain\n---\n{sm.MANAGED_MARKER}\n\n# Brain\n", encoding="utf-8")
    assert sm.has_managed_marker(frontmatter), "the skill's marker sits after its frontmatter"

    buried = tmp_path / "buried.md"
    buried.write_text("\n" * (sm.MARKER_HEAD_LINES + 5) + sm.MANAGED_MARKER + "\n", encoding="utf-8")
    assert not sm.has_managed_marker(buried), "a marker past the head is a mention, not a claim"

    assert not sm.has_managed_marker(tmp_path / "missing.md")


@pytest.mark.parametrize("header", [
    "rem Generated by brain-setup.py - do not edit by hand.",
    "rem Generated by setup-windows.ps1 - do not edit by hand. Re-run setup-windows.ps1 to regenerate.",
    f"# {sm.MANAGED_MARKER}",
])
def test_generated_launchers_are_recognised_in_every_form_ever_written(tmp_path, header):
    """The retired Windows installer wrote its own header, and the brain.cmd it left
    behind is precisely the file the uninstaller must be able to remove."""
    launcher = tmp_path / "brain.cmd"
    launcher.write_text(f"@echo off\r\n{header}\r\n", encoding="utf-8")
    assert sm.is_generated_launcher(launcher)


def test_a_users_own_file_at_a_launcher_name_is_not_claimed(tmp_path):
    theirs = tmp_path / "brain.cmd"
    theirs.write_text("@echo off\r\nrem my own wrapper\r\n", encoding="utf-8")
    assert not sm.is_generated_launcher(theirs)


def test_write_managed_text_backs_up_a_users_file_and_replaces_ours(tmp_path):
    target = tmp_path / "CLAUDE.md"
    target.write_text("# my own global instructions\n", encoding="utf-8")

    backup = sm.write_managed_text(target, MARKED)
    assert backup is not None and backup.is_file()
    assert backup.read_text(encoding="utf-8") == "# my own global instructions\n"
    assert target.read_text(encoding="utf-8") == MARKED

    # Ours now: replaced in place, no second backup.
    again = sm.write_managed_text(target, MARKED + "# v2\n")
    assert again is None
    assert [p.name for p in tmp_path.glob("*.brain-backup-*")] == [backup.name]


def test_write_managed_text_refuses_text_without_the_marker(tmp_path):
    """Otherwise the next run would back our own output up as a user file, forever."""
    with pytest.raises(ValueError, match="managed-by"):
        sm.write_managed_text(tmp_path / "x.md", "# no marker here\n")
    assert not (tmp_path / "x.md").exists()


def test_write_managed_text_honours_the_encoding(tmp_path):
    target = tmp_path / "launch.cmd"
    sm.write_managed_text(target, f"rem {sm.MANAGED_MARKER}\r\nset X=caf\u00e9\r\n", encoding="cp437")
    assert target.read_bytes() == f"rem {sm.MANAGED_MARKER}\r\nset X=caf\u00e9\r\n".encode("cp437")
    with pytest.raises(UnicodeEncodeError):
        sm.write_managed_text(tmp_path / "bad.cmd", f"rem {sm.MANAGED_MARKER}\r\nset X=\u0421\r\n", encoding="cp437")
    assert not (tmp_path / "bad.cmd").exists()
    assert not list(tmp_path.glob("*.tmp")), "a failed encode must not leave a temp file"


def test_an_unownable_template_is_rejected(tmp_path):
    template = tmp_path / "bad.json"
    template.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "/opt/somewhere/else.py"}]}
    ]}}), encoding="utf-8")
    with pytest.raises(sm.SettingsError, match="duplicate"):
        sm.merge(tmp_path / "settings.json", template, brain_cmd=BRAIN_CMD)
