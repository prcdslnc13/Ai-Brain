"""What an elastic memory looks like in a preload, and which machines it loads on.

Measured 2026-11-11 on the real vault: 81.9 KB of loadable memories against about
58 KB that 7 hook parts deliver, with 33 memories catalogued instead of loaded. Two
things were taking the room without carrying anything:

- **Frontmatter** was 18.8 KB (23%). In a preload every field of it is a repeat: the
  name is in the path header, the description is the opening of the body, the type is
  the section heading.
- **Notes about one machine** loaded on all of them: about 15 of 32 user memories.

Both fixes are lossless, and that is the property under test. Nothing on disk changes,
recall still returns everything, and a scope the code cannot read loads the memory
rather than hiding it. A third fix rides along: `user/profile` fills first.

These are invariant tests on purpose. The bundle is built once and rendered by three
frontends (`render`, `render_parts`, the raw dict over MCP), so each property is
asserted on the bundle and on both renderings rather than on one path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brain_mcp import brain_prep, vault

from conftest import memory

THIS = "test-host"  # conftest pins BRAIN_MACHINE to this


def _scoped(path: Path, body: str, machines: str, mtype: str = "user") -> Path:
    """A memory whose frontmatter carries a `machines:` line, written verbatim."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {path.stem}\ndescription: {body[:40]}\ntype: {mtype}\n"
        f"machines: {machines}\n---\n\n{body}\n",
        encoding="utf-8", newline="\n",
    )
    return path


def _elastic_paths(bundle: dict) -> list[str]:
    return [
        item["path"]
        for section in bundle["sections"]
        if section["label"] in ("user", "feedback")
        for item in section["items"]
    ]


def _all_renderings(bundle: dict) -> list[str]:
    """Every form in which a frontend puts this bundle in front of a model."""
    return [brain_prep.render(bundle), "\n".join(brain_prep.render_parts(bundle))]


# ------------------------------------------------------------------ frontmatter


def test_no_elastic_item_carries_frontmatter(vault_dir: Path) -> None:
    brain = vault_dir
    memory(brain / "user" / "profile.md", "profile", "user", "I am the user.")
    memory(brain / "feedback" / "rule.md", "a rule", "feedback", "Always do X.")
    memory(brain / "projects" / "Proj" / "feedback" / "p.md", "p rule", "feedback", "Do Y here.")

    bundle = vault.session_start_bundle("Proj")

    items = [i for s in bundle["sections"] for i in s["items"]]
    assert len(items) == 3
    for item in items:
        assert not item["content"].startswith("---"), item["path"]
        assert "description:" not in item["content"], item["path"]
    for text in _all_renderings(bundle):
        assert "description:" not in text
        assert "Always do X." in text and "I am the user." in text


def test_pinned_items_keep_their_frontmatter(vault_dir: Path) -> None:
    """Overview and checkpoint are narrative, and their frontmatter says what they are."""
    memory(vault_dir / "projects" / "Proj" / "overview.md", "overview", "project", "# Proj")
    bundle = vault.session_start_bundle("Proj")
    (overview,) = [
        i for s in bundle["sections"] if s["label"].endswith(":overview") for i in s["items"]
    ]
    assert overview["content"].startswith("---")


def test_stripping_is_lossless_and_has_an_opt_out(vault_dir: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    f = memory(vault_dir / "feedback" / "rule.md", "a rule", "feedback", "Always do X.",
               description="a hand-written summary")
    before = f.read_bytes()

    vault.session_start_bundle(None)
    assert f.read_bytes() == before, "the preload must never rewrite a memory"
    assert vault.Memory.from_file(f).description == "a hand-written summary"

    monkeypatch.setenv("BRAIN_PRELOAD_STRIP_FRONTMATTER", "0")
    (item,) = vault.collect_preload_candidates(None)["elastic"]
    assert item["content"].startswith("---")
    assert "a hand-written summary" in item["content"]


def test_a_body_that_opens_with_a_rule_line_is_not_mistaken_for_frontmatter() -> None:
    """`---` then prose is a markdown horizontal rule, not a mapping to throw away."""
    text = "---\nNever do this.\n---\n\nThe rest of the note.\n"
    assert vault.strip_frontmatter(text) == text
    assert vault.strip_frontmatter("no frontmatter at all\n") == "no frontmatter at all\n"


# ---------------------------------------------------------------- machine scope


def test_a_memory_scoped_to_another_machine_is_left_out_everywhere(vault_dir: Path) -> None:
    memory(vault_dir / "user" / "everywhere.md", "everywhere", "user", "Applies on every box.")
    _scoped(vault_dir / "user" / "elsewhere.md", "Only true on the other box.", "[other-box]")
    _scoped(vault_dir / "feedback" / "elsewhere-rule.md", "A rule for the other box.",
            "[other-box]", mtype="feedback")

    for slim in (False, True):
        bundle = vault.session_start_bundle(None, slim=slim)
        assert _elastic_paths(bundle) == ["Brain/user/everywhere.md"]
        # Left out on purpose is not "did not fit": it must not be catalogued as missing.
        assert bundle["skipped_items"] == []
        assert sorted(i["name"] for i in bundle["scoped_out_items"]) == [
            "feedback/elsewhere-rule", "user/elsewhere"]
        for text in _all_renderings(bundle):
            assert "Only true on the other box." not in text
            assert "A rule for the other box." not in text
            assert brain_prep.CATALOGUE_HEADING not in text


@pytest.mark.parametrize("machines", [
    f"[{THIS}]",
    f"[other-box, {THIS}]",
    THIS,                # a bare string instead of a list
    "[Test-Host]",       # compared slugified, the form machine_name() returns
    "['Test Host']",
])
def test_a_memory_scoped_to_this_machine_loads(vault_dir: Path, machines: str) -> None:
    _scoped(vault_dir / "user" / "here.md", "True on this box.", machines)
    bundle = vault.session_start_bundle(None)
    assert _elastic_paths(bundle) == ["Brain/user/here.md"]
    assert bundle["scoped_out_items"] == []


@pytest.mark.parametrize("machines", ["[]", "5", "{a: b}", "[3, null]", "''", "null"])
def test_an_unreadable_scope_loads_the_memory(vault_dir: Path, machines: str) -> None:
    """Fail open. A typo must load a memory too widely, never hide it."""
    _scoped(vault_dir / "user" / "typo.md", "Still loads.", machines)
    assert _elastic_paths(vault.session_start_bundle(None)) == ["Brain/user/typo.md"]


def test_scope_has_an_opt_out(vault_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _scoped(vault_dir / "user" / "elsewhere.md", "Only true on the other box.", "[other-box]")
    monkeypatch.setenv("BRAIN_PRELOAD_MACHINE_SCOPE", "0")
    bundle = vault.session_start_bundle(None)
    assert _elastic_paths(bundle) == ["Brain/user/elsewhere.md"]
    assert bundle["scoped_out_items"] == []


def test_the_preload_says_how_many_were_scoped_out_and_against_which_machine(
        vault_dir: Path) -> None:
    """A wrong machine name hides this machine's own memories. This line is where it shows."""
    memory(vault_dir / "user" / "everywhere.md", "everywhere", "user", "Applies on every box.")
    _scoped(vault_dir / "user" / "a.md", "Other box A.", "[other-box]")
    _scoped(vault_dir / "user" / "b.md", "Other box B.", "[other-box]")

    bundle = vault.session_start_bundle(None)
    assert bundle["machine"] == THIS
    note = "2 memories scoped to other machines were left out (this machine: `test-host`)"
    assert note in brain_prep.render(bundle)
    parts = brain_prep.render_parts(bundle)
    assert note in parts[0]
    assert all(note not in p for p in parts[1:])


def test_nothing_scoped_out_means_no_note(vault_dir: Path) -> None:
    memory(vault_dir / "user" / "everywhere.md", "everywhere", "user", "Applies on every box.")
    bundle = vault.session_start_bundle(None)
    for text in _all_renderings(bundle):
        assert "scoped to other machines" not in text


def test_scope_narrows_only_the_preload(vault_dir: Path) -> None:
    """Recall and list still see a scoped memory on every machine."""
    _scoped(vault_dir / "user" / "elsewhere.md", "Podman lives on the other box.",
            "[other-box]")
    assert [m.path.name for m in vault.list_memories("user")] == ["elsewhere.md"]
    assert [m.path.name for m in vault.search_memories("Podman")] == ["elsewhere.md"]


def test_a_save_keeps_the_scope_it_was_given(vault_dir: Path) -> None:
    """The scope is set through caller frontmatter, the route all three frontends share."""
    result = vault.save_memory(
        "user", "podman on the other box",
        "---\nmachines: [Other-Box]\n---\nThe other box has podman, not docker.",
    )
    fm = vault._read_frontmatter_head(result.path)
    assert vault.machine_scope(fm) == ["other-box"]
    assert fm["machine"] == THIS, "saved-on is still stamped, and is a different field"
    assert _elastic_paths(vault.session_start_bundle(None)) == []


# ---------------------------------------------------------------- profile first


def test_the_profile_fills_first_in_the_user_section(vault_dir: Path) -> None:
    """A name sort put it behind every `a-*` .. `o-*` note, where the budget cut it."""
    for name in ("aardvark", "mac-thing", "zebra"):
        memory(vault_dir / "user" / f"{name}.md", name, "user", f"{name} note.")
    memory(vault_dir / "user" / "profile.md", "profile", "user", "I am the user.")

    assert _elastic_paths(vault.session_start_bundle(None)) == [
        "Brain/user/profile.md", "Brain/user/aardvark.md",
        "Brain/user/mac-thing.md", "Brain/user/zebra.md",
    ]


def test_the_profile_survives_a_budget_that_drops_other_user_memories(vault_dir: Path) -> None:
    for i in range(20):
        memory(vault_dir / "user" / f"a-note-{i:02d}.md", f"note {i}", "user", "Filler. " * 150)
    memory(vault_dir / "user" / "profile.md", "profile", "user", "I am the user.")

    bundle = vault.session_start_bundle(None, budget_kb=6)
    assert bundle["skipped_items"], "the fixture must overflow the budget to mean anything"
    assert "Brain/user/profile.md" in _elastic_paths(bundle)
