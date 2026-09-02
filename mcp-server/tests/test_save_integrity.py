"""A save must never silently destroy a record, and forget must never reach past one.

Three findings from the 2026-09-01 review, all on the pre-approved agent surface:

- F10: `write_memory` composed `<slug>.md` and wrote over whatever was there. "Git
  discipline" and "git   discipline!" are one path, so a model saving a new rule
  under a title that slugifies like an old one erased a user correction with no
  trace. Every non-Latin title slugified to the bare `untitled`, so all of those
  collided with each other. Overwrite-by-title is a feature (the overview-stub
  upgrade relies on it), so the fix keeps a copy of what it replaces under
  `archive/versions/` and says so, rather than refusing.

- F11: a body opening with a markdown horizontal rule was mistaken for caller-supplied
  frontmatter and written verbatim, so the note had no `type`, parsed as `unknown`,
  and dropped out of every typed recall and out of stats.

- F26: `forget_memory` accepted anything inside `Brain/`, including `_index.md`,
  `activity.md` and the vector index.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest
import yaml

from brain_mcp import cli, server, vault


def _fm(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---")
    end = text.find("\n---", 3)
    assert end != -1
    return yaml.safe_load(text[3:end])


def _versions(brain: Path, rel_stem: str) -> list[Path]:
    d = brain.joinpath(*vault.VERSIONS_DIR, *Path(rel_stem).parts)
    return sorted(d.glob("*.md")) if d.exists() else []


def _run_cli(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code in (0, None), f"{argv} exited {exc.value.code}"


# --- F10: colliding titles keep the previous version ---------------------------------

def test_colliding_titles_keep_the_previous_version(vault_dir: Path) -> None:
    first = vault.save_memory("feedback", "Git discipline", "Never force-push.")
    assert not first.overwrote and first.previous_version is None
    original = first.path.read_text(encoding="utf-8")

    second = vault.save_memory("feedback", "git   discipline!", "Always rebase first.")

    assert second.path == first.path, "the two titles slugify identically"
    assert second.overwrote and second.previous_version is not None
    assert "Always rebase first." in second.path.read_text(encoding="utf-8")
    versions = _versions(vault_dir, "feedback/git-discipline")
    assert versions == [second.previous_version]
    assert versions[0].read_text(encoding="utf-8") == original, (
        "the archived version is the exact bytes that were replaced"
    )


def test_identical_resave_creates_no_version(vault_dir: Path) -> None:
    first = vault.save_memory("user", "Prefers tabs", "The user prefers tabs.")
    mtime = first.path.stat().st_mtime_ns

    again = vault.save_memory("user", "Prefers tabs", "The user prefers tabs.")

    assert again.unchanged and not again.overwrote and again.previous_version is None
    assert first.path.stat().st_mtime_ns == mtime, "nothing was written"
    assert _versions(vault_dir, "user/prefers-tabs") == []


def test_versions_are_invisible_to_the_memory_predicate(vault_dir: Path) -> None:
    """`archive/` is in EXCLUDE_DIRS, which is what keeps every version out of the
    index, `brain list`, recall and both preloads. Assert the property that carries
    all four rather than each consumer."""
    vault.save_memory("feedback", "rule", "v1")
    result = vault.save_memory("feedback", "rule", "v2")
    assert result.previous_version is not None
    assert not vault.is_memory_path(result.previous_version, vault_dir)
    assert [m.path for m in vault.list_memories()] == [result.path]


def test_version_cap_holds(vault_dir: Path) -> None:
    for i in range(vault.VERSION_KEEP + 3):
        vault.save_memory("reference", "Dashboards", f"body {i}")

    versions = _versions(vault_dir, "references/dashboards")
    assert len(versions) == vault.VERSION_KEEP
    kept = [v.read_text(encoding="utf-8") for v in versions]
    # Newest last: the final version archived is the body the last save replaced.
    assert f"body {vault.VERSION_KEEP + 1}" in kept[-1]
    assert not any("body 0" in k for k in kept), "the oldest versions are pruned"


def test_overwriting_a_stub_archives_nothing(vault_dir: Path) -> None:
    """The stub upgrade is the *intended* overwrite; a version of hook scaffolding
    would be noise in the one place that should hold only user records."""
    stub = vault.ensure_project_overview_stub("Widget", None)
    assert stub is not None and vault.is_overview_stub(stub)

    result = vault.save_memory("project", "overview", "Widget widgets.", project="Widget")

    assert result.path == stub
    assert result.overwrote and result.previous_version is None
    assert _versions(vault_dir, "projects/Widget/overview") == []
    assert not vault.is_overview_stub(stub)


def test_cli_reports_the_overwrite_on_stderr(vault_dir: Path, capsys) -> None:
    _run_cli(["save", "user", "Editor", "--content", "vim"])
    capsys.readouterr()
    _run_cli(["save", "user", "editor", "--content", "emacs"])
    out, err = capsys.readouterr()
    assert out.startswith("saved: ")
    assert "overwrote" in err and "previous version kept at" in err

    _run_cli(["save", "user", "editor", "--content", "emacs"])
    out, err = capsys.readouterr()
    assert out.startswith("unchanged: ") and err == ""


def test_mcp_result_reports_the_overwrite(vault_dir: Path) -> None:
    def call(content: str) -> dict:
        got = asyncio.run(server.call_tool(
            "brain_save", {"type": "user", "name": "Shell", "content": content}))
        return json.loads(got[0].text)

    first = call("zsh")
    assert first["overwrote"] is False and "previous_version" not in first
    second = call("fish")
    assert second["overwrote"] is True
    assert Path(second["previous_version"]).exists()
    third = call("fish")
    assert third["unchanged"] is True and third["overwrote"] is False


# --- F10: slugs ----------------------------------------------------------------------

def test_non_ascii_titles_get_distinct_files(vault_dir: Path) -> None:
    a = vault.save_memory("user", "Заметки", "cyrillic")
    b = vault.save_memory("user", "笔记", "cjk")
    assert a.path != b.path
    assert not a.overwrote and not b.overwrote
    for p in (a.path, b.path):
        assert p.name.startswith(f"{vault.SLUG_FALLBACK}-") and p.name != "untitled.md"


def test_the_same_non_ascii_title_maps_to_one_file() -> None:
    import unicodedata
    composed = unicodedata.normalize("NFC", "Zametki café")
    decomposed = unicodedata.normalize("NFD", "Zametki café")
    assert vault.slugify(composed) == vault.slugify(decomposed) == "zametki-cafe"
    assert vault.slugify("Заметки") == vault.slugify(unicodedata.normalize("NFD", "Заметки"))


@pytest.mark.parametrize("title, slug", [
    ("Café notes", "cafe-notes"),
    ("Git discipline", "git-discipline"),
    ("git   discipline!", "git-discipline"),
    ("naïve façade — über", "naive-facade-uber"),
    ("", "untitled"),
    ("   ", "untitled"),
])
def test_slugify_transliterates(title: str, slug: str) -> None:
    assert vault.slugify(title) == slug


# --- F11: caller frontmatter is validated, a horizontal rule is body -----------------

def test_a_body_opening_with_a_horizontal_rule_gets_frontmatter(vault_dir: Path) -> None:
    result = vault.save_memory("feedback", "Rule", "---\nNever do X.\n\nMore detail.")

    fm = _fm(result.path)
    assert fm["type"] == "feedback" and fm["name"] == "Rule"
    mem = vault.Memory.from_file(result.path)
    assert mem.type == "feedback"
    assert "Never do X." in mem.body
    assert vault.stats()["by_type"]["feedback"] == 1


def test_two_horizontal_rules_are_still_body(vault_dir: Path) -> None:
    """`---\\nRule one\\n---\\nRule two` parses as a YAML *string*, not a mapping."""
    result = vault.save_memory("user", "Rules", "---\nRule one\n---\nRule two\n")
    assert _fm(result.path)["type"] == "user"
    body = vault.Memory.from_file(result.path).body
    assert "Rule one" in body and "Rule two" in body


def test_caller_frontmatter_with_matching_type_is_preserved(vault_dir: Path) -> None:
    content = "---\nname: custom name\ntype: user\ndescription: hand-written\nextra: 1\n---\n\nBody.\n"
    result = vault.save_memory("user", "ignored title", content)

    fm = _fm(result.path)
    assert fm["name"] == "custom name" and fm["description"] == "hand-written"
    assert fm["type"] == "user" and fm["extra"] == 1
    assert fm["machine"], "missing housekeeping fields are filled in"
    assert vault.Memory.from_file(result.path).body.strip() == "Body."


def test_caller_frontmatter_without_a_type_is_filled_in(vault_dir: Path) -> None:
    result = vault.save_memory("feedback", "Scoped", "---\nname: x\n---\nBody.",
                               project="Widget")
    fm = _fm(result.path)
    assert fm["type"] == "feedback" and fm["project"] == "Widget" and fm["name"] == "x"


def test_caller_frontmatter_with_the_wrong_type_is_rejected(vault_dir: Path) -> None:
    with pytest.raises(ValueError, match="declares type 'user'.*requested 'feedback'"):
        vault.save_memory("feedback", "Rule", "---\ntype: user\n---\nBody.")
    assert not (vault_dir / "feedback" / "rule.md").exists(), "nothing is written"


def test_every_saved_memory_parses_with_its_requested_type(vault_dir: Path) -> None:
    """The invariant behind F11: whatever the body looks like, the file on disk
    reads back as the type that was asked for."""
    bodies = ["plain", "---\nhr first", "---\na\n---\nb", "---\nname: n\n---\nfm", "--- not a fence",
              "----\nfour dashes"]
    for i, body in enumerate(bodies):
        r = vault.save_memory("reference", f"ref {i}", body)
        assert vault.read_frontmatter_type(r.path) == "reference", body


# --- F26: forget is bounded by the memory predicate ----------------------------------

@pytest.mark.parametrize("rel", [
    "_index.md",
    "activity.md",
    "README.md",
    ".index/embeddings.sqlite",
    "archive/versions/feedback/rule/2026-01-01-000000-host.md",
    "_setup/notes.md",
    "user/notes.md.tmp",
])
def test_forget_refuses_vault_bookkeeping(vault_dir: Path, rel: str) -> None:
    target = vault_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("not a memory\n", encoding="utf-8")

    with pytest.raises(PermissionError, match="not a memory"):
        vault.forget_memory(str(target))
    assert target.exists()


def test_forget_still_deletes_memories_and_checkpoints(vault_dir: Path) -> None:
    memory = vault.save_memory("user", "Deletable", "bye").path
    checkpoint = vault.write_checkpoint("Widget", "bye")
    rollup = vault_dir / "projects" / "Widget" / "sessions" / "daily" / "2026-01-01.md"
    rollup.parent.mkdir(parents=True, exist_ok=True)
    rollup.write_text("rollup\n", encoding="utf-8")
    for p in (memory, checkpoint, rollup):
        vault.forget_memory(str(p))
        assert not p.exists()


def test_forget_decides_through_the_single_predicate() -> None:
    """The deletable set must be the listable set. Assert the wiring, not the list:
    a private exclusion inside forget would drift exactly as the three earlier
    copies of "is this a memory" did."""
    tree = ast.parse(Path(vault.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "forget_memory")
    calls = {getattr(n.func, "id", None) for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert "is_memory_path" in calls
    assert not any(name in ast.dump(fn) for name in vault.EXCLUDE_FILES), (
        "forget_memory names excluded files literally instead of using the predicate"
    )
