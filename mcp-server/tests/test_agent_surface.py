"""The pre-approved CLI invocation may not read arbitrary local files.

The installers put the Brain command in `permissions.allow` so proactive saves never
raise a prompt — an unanswered prompt is indistinguishable from the model deciding not
to save, which is the failure the Brain exists to prevent. But pre-approval means
*unattended*: a prompt-injected model can run the approved command with no human in the
loop. `brain save user notes --file ~/.ssh/id_rsa` then copies that file into the vault,
where an ordinary `brain recall` hands it back and the SessionStart preload may load it
unasked — a local-file exfiltration primitive.

Narrowing the permission rule alone cannot fix this: Claude Code's rules are PREFIX
matches, so `Bash(<cmd> save:*)` still matches `save --file <anything>`. The enforceable
boundary is the env var baked into the approved prefix — the generated `brain.cmd` on
Windows, the `BRAIN_AGENT_SURFACE=1 BRAIN_VAULT=... /bin/brain` prefix on POSIX — which
the CLI reads and refuses the file-import options under. An invocation *without* the
variable is simply not pre-approved and prompts like any other command.

Reproduced 2026-08-25 against the live vault before the fix: the Windows hosts file
landed in `Brain/user/` as a preloading "user memory".
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from brain_mcp import cli, vault

REPO_ROOT = Path(__file__).resolve().parents[2]


def run(argv: list[str]) -> None:
    """`cli.main` always exits, including on success. Assert the success code."""
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code in (0, None), f"{argv} exited {exc.value.code}"


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    p = tmp_path / "id_rsa"
    p.write_text("-----BEGIN PRIVATE KEY-----\nhunter2\n", encoding="utf-8")
    return p


@pytest.fixture
def agent_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cli.AGENT_SURFACE_ENV, "1")


# ------------------------------------------------------------------ the gate --

RESTRICTED_INVOCATIONS = [
    ["save", "user", "notes", "--file", "SECRET"],
    ["save", "user", "notes", "-f", "SECRET"],
    ["checkpoint", "Ai-Brain", "--file", "SECRET"],
    ["checkpoint", "Ai-Brain", "-f", "SECRET"],
    ["checkpoint", "Ai-Brain", "--from-pi", "SECRET"],
    ["checkpoint", "--from-cherryd", "SECRET"],
    # Alternate orderings and smuggling shapes: the gate reads the parsed namespace,
    # never the raw argv, so none of these can slip past by rearrangement.
    ["save", "--file", "SECRET", "user", "notes"],
    ["save", "--project", "Ai-Brain", "--file", "SECRET", "project", "notes"],
    ["checkpoint", "--source", "pi:compact", "--from-pi", "SECRET", "Ai-Brain"],
    ["save", "user", "notes", "--file=SECRET"],
]


@pytest.mark.parametrize(
    "argv", RESTRICTED_INVOCATIONS, ids=[" ".join(a) for a in RESTRICTED_INVOCATIONS]
)
def test_restricted_options_are_refused_on_the_agent_surface(
    argv, vault_dir: Path, secret: Path, agent_surface, capsys
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([a.replace("SECRET", str(secret)) for a in argv])
    assert exc.value.code == 2, "rejected input should exit 2, like a bad --project"

    err = capsys.readouterr().err
    assert "not available on the agent surface" in err
    assert "Traceback" not in err
    assert "hunter2" not in err


@pytest.mark.parametrize(
    "argv", RESTRICTED_INVOCATIONS, ids=[" ".join(a) for a in RESTRICTED_INVOCATIONS]
)
def test_a_refused_invocation_writes_nothing(
    argv, vault_dir: Path, secret: Path, agent_surface
) -> None:
    """The point of the gate is that the secret never reaches the vault -- assert on
    the filesystem, not merely on the exception."""
    before = set(vault_dir.rglob("*"))
    with pytest.raises(SystemExit):
        cli.main([a.replace("SECRET", str(secret)) for a in argv])
    assert set(vault_dir.rglob("*")) == before

    blob = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in vault_dir.rglob("*.md")
        if p.is_file()
    )
    assert "hunter2" not in blob


@pytest.mark.parametrize("value", ["", "0"])
def test_the_gate_is_off_for_operators(
    value: str, vault_dir: Path, secret: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset or explicitly 0 -- the venv binary an operator or a timer runs."""
    monkeypatch.setenv(cli.AGENT_SURFACE_ENV, value)
    run(["save", "user", "notes", "--file", str(secret)])
    saved = (vault_dir / "user" / "notes.md").read_text(encoding="utf-8")
    assert "hunter2" in saved


def test_the_gate_is_off_when_the_variable_is_absent(
    vault_dir: Path, secret: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(cli.AGENT_SURFACE_ENV, raising=False)
    run(["save", "user", "notes", "--file", str(secret)])
    assert "hunter2" in (vault_dir / "user" / "notes.md").read_text(encoding="utf-8")


# ------------------------------------------- the surface a model actually needs --

def test_ordinary_agent_operations_still_work(
    vault_dir: Path, agent_surface, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recall, inline save, list, stats, forget and an inline checkpoint are the
    proactive-memory surface. If the gate broke these it would be worse than the bug."""
    run(["save", "user", "prefers-tabs", "--content", "The user prefers tabs."])
    run(["save", "feedback", "scoped", "--content", "A rule.", "--project", "Ai-Brain"])
    run(["checkpoint", "Ai-Brain", "--summary", "did a thing"])
    run(["list", "--type", "user"])
    run(["recall", "tabs"])
    run(["stats"])
    out = capsys.readouterr().out
    assert "prefers-tabs" in out and "did a thing" not in out

    run(["forget", str(vault_dir / "user" / "prefers-tabs.md")])
    assert not (vault_dir / "user" / "prefers-tabs.md").exists()


def test_stdin_bodies_still_work(
    vault_dir: Path, agent_surface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heredoc form the skill teaches must survive -- it is how every multi-line
    proactive save arrives."""
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("A multi-line\nmemory body.\n"))
    run(["save", "user", "via-stdin"])
    assert "multi-line" in (vault_dir / "user" / "via-stdin.md").read_text(encoding="utf-8")


# --------------------------------------------------------------- anti-drift ----

def test_every_path_reading_option_is_gated() -> None:
    """The invariant, not the instance: cli.py may not grow a NEW option that opens a
    caller-supplied path without adding it to RESTRICTED_OPTIONS. This repo's bug
    pattern is a fix landing at one of N sites, and a fourth import flag added to
    `checkpoint` would silently reopen the hole for every pre-approved session.
    """
    src = (REPO_ROOT / "mcp-server" / "brain_mcp" / "cli.py").read_text(encoding="utf-8")
    # Every `args.<dest>` handed to open() or Path() is a path read off the caller's
    # disk. `path` is the exception: `forget` takes one, but vault.forget_memory
    # refuses anything outside the Brain directory, so it is not an import primitive.
    opened = set(re.findall(r"(?:open|Path)\(\s*args\.([a-z_]+)", src))
    allowed = set(cli.RESTRICTED_OPTIONS) | {"path"}
    assert opened <= allowed, (
        f"cli.py reads {sorted(opened - allowed)} off disk without gating it; add the "
        f"dest to RESTRICTED_OPTIONS or route it through the vault"
    )


def test_restricted_options_all_exist_in_the_parser() -> None:
    """A dest renamed in the parser but not here would silently disable the gate."""
    dests = set()

    def walk(parser: argparse.ArgumentParser) -> None:
        for action in parser._actions:
            dests.add(action.dest)
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict):  # a subparsers action; --type is a list
                for candidate in choices.values():
                    if isinstance(candidate, argparse.ArgumentParser):
                        walk(candidate)

    walk(cli.build_parser())
    assert set(cli.RESTRICTED_OPTIONS) <= dests


def test_forget_cannot_delete_outside_the_vault(vault_dir: Path, secret: Path) -> None:
    """`forget` is on the agent surface and takes a path, so an unattended model can
    call it. Deleting arbitrary files would be worse than reading them."""
    with pytest.raises((PermissionError, FileNotFoundError)):
        vault.forget_memory(str(secret))
    assert secret.exists()


# --------------------------------------------------- the installer sets it ------

# Both spellings, because the pre-approved command is built two ways: the generated
# Windows `brain.cmd` wrapper sets the variable with `set`, while the POSIX BRAIN_CMD
# is an env prefix on the command itself. ROADMAP 3G retired the three platform
# installers on 2026-08-25, so one file now has to carry both.
INSTALLER_FRAGMENTS = ['set "BRAIN_AGENT_SURFACE=1"', "BRAIN_AGENT_SURFACE=1 BRAIN_VAULT="]


def test_the_installer_bakes_the_gate_into_the_approved_command() -> None:
    """One installer, one boundary — and it must hold on both platforms.

    The pre-approval is a *prefix* match, so narrowing the rule cannot express
    "no --file"; the variable baked into the approved command is the only thing that
    can. An installer that forgets it produces a pre-approved command with the FULL
    CLI surface, and nothing else in the system would notice.
    """
    src = (REPO_ROOT / "brain-setup.py").read_text(encoding="utf-8")
    for fragment in INSTALLER_FRAGMENTS:
        assert fragment in src, (
            f"brain-setup.py no longer sets {cli.AGENT_SURFACE_ENV} via {fragment!r}"
        )


def test_the_skill_does_not_pre_approve_the_whole_cli() -> None:
    """SKILL.md carries its own `allowed-tools` pre-approval -- a second door that
    narrowing settings.json alone would leave wide open."""
    skill = (REPO_ROOT / "templates" / "skills" / "brain" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    line = next(ln for ln in skill.splitlines() if ln.startswith("allowed-tools:"))
    assert "Bash(__BRAIN_CMD__:*)" not in line, "the blanket skill pre-approval is back"
    assert "Bash(__BRAIN_CMD__ recall:*)" in line


def test_the_pi_extension_keeps_the_full_surface_for_its_own_spawns_only() -> None:
    """pi drives `checkpoint --from-pi` as the operator, so its own spawns clear the
    gate — but in *their* environment, never in `process.env`. pi's `exec` has no
    per-call env, so the extension used to assign `process.env.BRAIN_AGENT_SURFACE =
    "0"`, and the model's shell tool inherits process.env: every command the model
    ran had the gate cleared, on the one surface the gate exists to bound
    (2026-09-01). The spawn therefore goes through node:child_process with an
    explicit env and no shell."""
    src = (REPO_ROOT / "pi" / "extensions" / "brain.ts").read_text(encoding="utf-8")
    assert 'BRAIN_AGENT_SURFACE: "0"' in src, "the extension's own spawns must clear the gate"
    assert not re.search(r"process\.env\.BRAIN_AGENT_SURFACE\s*=", src), (
        "assigning BRAIN_AGENT_SURFACE process-wide clears the gate for the model's shell tool"
    )
    assert "pi.exec(" not in src, "pi.exec cannot scope env per call; spawn via node:child_process"
    assert "shell: false" in src
    assert 'from "node:child_process"' in src
