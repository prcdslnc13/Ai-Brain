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
boundary is the env var the approved command sets from inside — the generated
`brain-agent.py` launcher, run by the venv's python, on every platform — which the CLI
reads and refuses the file-import options under. An invocation *without* the variable
is simply not pre-approved and prompts like any other command.

The launcher replaced two earlier shapes on 2026-09-01, both of which are asserted
against below: a Windows `brain.cmd` forwarding `%*` (a command-injection hole — cmd.exe
re-expands the arguments after Git Bash has quoted them, so `x"&echo pwned` ran a second
command through the pre-approved wrapper; BatBadBut, CVE-2024-24576) and a POSIX
`BRAIN_AGENT_SURFACE=1 BRAIN_VAULT=... /bin/brain` env prefix, which Claude Code's rule
matcher does not reliably match past.

Reproduced 2026-08-25 against the live vault before the fix: the Windows hosts file
landed in `Brain/user/` as a preloading "user memory".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from brain_mcp import cli, vault
from conftest import load_repo_script

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_SERVER_SRC = REPO_ROOT / "mcp-server"


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
#
# These run the installer's real functions and the launcher it generates, in a
# subprocess, against a throwaway vault. The previous version of this section grepped
# brain-setup.py for two string fragments, which a comment satisfied -- and did, for a
# while, after the fragments moved into a docstring.

SETUP_SCRIPT = "brain-setup.py"
sm = load_repo_script("brain_settings_merge.py")


@pytest.fixture(params=["windows", "posix"])
def installer(request, monkeypatch: pytest.MonkeyPatch):
    """brain-setup.py imported fresh and pinned to one platform's path shape.

    The token is built the same way on both platforms now; parametrizing is what
    proves neither branch can drift back to a shell wrapper or an env prefix.
    """
    m = load_repo_script(SETUP_SCRIPT)
    if request.param == "windows":
        monkeypatch.setattr(m, "IS_WINDOWS", True)
        monkeypatch.setattr(
            m, "VENV_PY", Path(r"C:\Users\Jo Bloggs\src\Ai-Brain\mcp-server\.venv\Scripts\python.exe")
        )
    else:
        monkeypatch.setattr(m, "IS_WINDOWS", False)
        monkeypatch.setattr(
            m, "VENV_PY", Path("/home/jo bloggs/src/Ai-Brain/mcp-server/.venv/bin/python")
        )
    return m


ENV_ASSIGNMENT = re.compile(r"(^|\s)[A-Za-z_][A-Za-z0-9_]*=")


def test_the_approved_command_is_two_quoted_paths_and_nothing_else(installer, tmp_path):
    """The rendered __BRAIN_CMD__ must never be a `.cmd`/`.bat` (cmd.exe re-parses the
    arguments: command injection) and must never start with `VAR=value` (Claude Code's
    allow-rule matcher does not reliably match past it: the rule silently never
    applies). Under both platform branches, from the real function, not from text."""
    cfg = tmp_path / "claude dir"  # a space, deliberately: it must survive quoting
    cfg.mkdir()
    token = installer.brain_cmd_token(cfg, tmp_path / "vault")

    words = shlex.split(token)
    assert len(words) == 2, f"expected `\"<python>\" \"<launcher>\"`, got {token!r}"
    interpreter, launcher = words
    assert token == f'"{interpreter}" "{launcher}"', "both paths must be double-quoted"
    for word in words:
        assert not word.lower().endswith((".cmd", ".bat", ".ps1", ".sh")), (
            f"a shell wrapper is back in the approved command: {word}"
        )
    assert not ENV_ASSIGNMENT.search(token), f"env-var assignment prefix is back: {token!r}"

    expected_py = str(installer.VENV_PY)
    expected_launcher = str(cfg / installer.AGENT_LAUNCHER_NAME)
    if installer.IS_WINDOWS:
        expected_py = expected_py.replace("\\", "/")
        expected_launcher = expected_launcher.replace("\\", "/")
        assert "\\" not in token, "Git Bash eats single backslashes; the token must be forward-slashed"
    assert interpreter == expected_py
    assert launcher == expected_launcher
    assert (cfg / installer.AGENT_LAUNCHER_NAME).is_file(), "the token names a launcher that was not written"


def test_the_launcher_carries_the_managed_marker(installer, tmp_path):
    """So a re-run replaces it in place and the uninstaller knows it may delete it."""
    launcher = installer.write_agent_launcher(tmp_path, tmp_path / "vault")
    assert sm.has_managed_marker(launcher)
    assert sm.is_generated_launcher(launcher)


# ---- the launcher, executed ----------------------------------------------------

def _launcher_env(decoy_vault: Path) -> dict:
    """The environment a Claude Code Bash tool would hand the launcher, plus a decoy
    BRAIN_VAULT the launcher MUST override and the source tree on PYTHONPATH so the
    subprocess runs this checkout's brain_mcp rather than whatever is pip-installed."""
    env = os.environ.copy()
    env.pop(cli.AGENT_SURFACE_ENV, None)
    env["BRAIN_VAULT"] = str(decoy_vault)
    env["BRAIN_EMBED"] = "0"
    env["BRAIN_MACHINE"] = "test-host"
    env["PYTHONPATH"] = str(MCP_SERVER_SRC)
    return env


@pytest.fixture
def launched(vault_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A generated launcher for a throwaway vault, plus the token that names it.

    VENV_PY is pointed at the interpreter running the tests: the worktree may have
    no venv at all, and what is under test is the launcher, not pip.
    """
    m = load_repo_script(SETUP_SCRIPT)
    monkeypatch.setattr(m, "VENV_PY", Path(sys.executable))
    cfg = tmp_path / "config dir"
    cfg.mkdir()
    token = m.brain_cmd_token(cfg, vault_dir.parent)
    decoy = tmp_path / "decoy-vault"
    (decoy / "Brain" / "user").mkdir(parents=True)
    return {
        "token": token,
        "launcher": cfg / m.AGENT_LAUNCHER_NAME,
        "env": _launcher_env(decoy),
        "vault": vault_dir,
        "decoy": decoy / "Brain",
    }


def run_launcher(launched: dict, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(launched["launcher"]), *args],
        env=launched["env"], capture_output=True, text=True, encoding="utf-8",
        timeout=120, check=False,
    )


def _all_markdown(root: Path) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8", errors="ignore") for p in root.rglob("*.md") if p.is_file()
    )


def test_the_generated_launcher_sets_the_gate(launched: dict, secret: Path) -> None:
    """Executed, not grepped: `brain-agent.py save user x --file <secret>` must exit 2
    with the agent-surface refusal, and the secret must reach neither vault."""
    res = run_launcher(launched, ["save", "user", "notes", "--file", str(secret)])
    assert res.returncode == 2, res.stderr
    assert "not available on the agent surface" in res.stderr
    assert "Traceback" not in res.stderr
    assert "hunter2" not in _all_markdown(launched["vault"])
    assert "hunter2" not in _all_markdown(launched["decoy"])


def test_the_generated_launcher_bakes_in_the_vault(launched: dict) -> None:
    """BRAIN_VAULT comes from the launcher, never from the caller's environment: a
    model (or a hook) cannot redirect a pre-approved save into another directory."""
    res = run_launcher(launched, ["save", "user", "baked-in", "--content", "hello"])
    assert res.returncode == 0, res.stderr
    assert (launched["vault"] / "user" / "baked-in.md").is_file()
    assert not (launched["decoy"] / "user" / "baked-in.md").exists()


BATBADBUT_PAYLOAD = 'zzq"&echo INJECTED'


def _injected(output: str) -> bool:
    """A line that is exactly the sentinel is the output of an injected `echo`; the
    payload merely being echoed back inside a longer line is not."""
    return any(line.strip() == "INJECTED" for line in output.splitlines())


def test_argv_reaches_the_cli_unparsed(launched: dict) -> None:
    """The payload must arrive as one argument and be treated as data."""
    res = run_launcher(launched, ["recall", BATBADBUT_PAYLOAD, "--json"])
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout)["query"] == BATBADBUT_PAYLOAD
    assert not _injected(res.stdout + res.stderr)


def _git_bash() -> str | None:
    """The bash Claude Code's Bash tool runs on this platform, or None.

    On Windows that is Git Bash, found explicitly: a bare `which bash` can resolve to
    System32/bash.exe, which is WSL -- a different machine for these purposes.
    """
    if sys.platform != "win32":
        return shutil.which("bash")
    for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"),
                 os.environ.get("ProgramFiles(x86)")):
        if not root:
            continue
        for rel in ("Git/bin/bash.exe", "Git/usr/bin/bash.exe"):
            candidate = Path(root) / rel
            if candidate.is_file():
                return str(candidate)
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


def test_the_approved_command_survives_a_batbadbut_payload_through_bash(launched: dict) -> None:
    """The live reproduction of F2, run against the fix: the approved command exactly as
    the model would type it into the Bash tool, with the argument that used to run
    `echo INJECTED` as a second command through brain.cmd. Nothing may be injected,
    and the query must arrive intact."""
    bash = _git_bash()
    if not bash:
        pytest.skip("no bash on this machine")
    command = f"{launched['token']} recall 'zzq\"&echo INJECTED' --json"
    res = subprocess.run(
        [bash, "-c", command], env=launched["env"],
        capture_output=True, text=True, encoding="utf-8", timeout=120, check=False,
    )
    assert res.returncode == 0, res.stderr
    assert not _injected(res.stdout + res.stderr), f"command injection through the approved command:\n{res.stdout}"
    assert json.loads(res.stdout)["query"] == BATBADBUT_PAYLOAD


# ---- the second pre-approval door -----------------------------------------------

def test_the_skill_pre_approves_exactly_the_agent_subcommands() -> None:
    """SKILL.md carries its own `allowed-tools` pre-approval -- a second door that
    narrowing settings.json alone would leave open. Set equality with the shared
    subcommand list, so neither side can grow (`reindex`) or shrink (a proactive
    `save` that prompts) without the other noticing."""
    skill = (REPO_ROOT / "templates" / "skills" / "brain" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    line = next(ln for ln in skill.splitlines() if ln.startswith("allowed-tools:"))
    rules = {r.strip() for r in line[len("allowed-tools:"):].split(",") if r.strip()}
    expected = {f"Bash(__BRAIN_CMD__ {sub}:*)" for sub in sm.AGENT_SUBCOMMANDS}
    assert rules == expected, (
        f"skill pre-approves {sorted(rules - expected)} that settings.json does not, "
        f"and misses {sorted(expected - rules)}"
    )
    assert "Bash(__BRAIN_CMD__:*)" not in line, "the blanket skill pre-approval is back"


def test_the_pi_extension_keeps_the_full_surface() -> None:
    """pi drives `checkpoint --from-pi` as the operator. If BRAIN_PI_CMD ever points at
    something that sets the gate, every automatic checkpoint would fail with an exit 2
    that nothing surfaces."""
    src = (REPO_ROOT / "pi" / "extensions" / "brain.ts").read_text(encoding="utf-8")
    assert 'process.env.BRAIN_AGENT_SURFACE = "0"' in src
