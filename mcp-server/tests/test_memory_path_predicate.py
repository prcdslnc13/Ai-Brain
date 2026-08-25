"""One predicate decides what counts as a memory.

There were three disagreeing answers: `iter_indexable_md` applied EXCLUDE_DIRS +
EXCLUDE_FILES, `list_memories` applied its own `_setup`/leading-underscore filter, and
`doctor.NON_MEMORY_NAMES` kept a third list that alone knew about README.md. The
visible symptom (2026-08-24): `brain list` returned `activity.md` — the 224 KB
Stop-hook audit log — and the vault's `README.md` as memories of type `unknown`, while
both were correctly absent from recall and from the vector index.
"""

from __future__ import annotations

from pathlib import Path

from brain_mcp import doctor, render, vault

from conftest import memory


def test_bookkeeping_files_are_not_memories(populated_vault: Path) -> None:
    (populated_vault / "activity.md").write_text("| turn | [sig=Y]\n", encoding="utf-8")
    (populated_vault / "README.md").write_text("# The vault\n", encoding="utf-8")
    (populated_vault / "_index.md").write_text("# Index\n", encoding="utf-8")

    names = {m.path.name for m in vault.list_memories()}
    assert "activity.md" not in names
    assert "README.md" not in names
    assert "_index.md" not in names
    assert "prefers-rust.md" in names, "real memories must still be listed"


def test_list_and_index_agree_on_the_corpus(populated_vault: Path) -> None:
    """The two enumerations must not disagree about what the vault contains.

    They did: `activity.md` was in one and not the other, which is how a 224 KB audit
    log ended up rendered as a memory.
    """
    (populated_vault / "activity.md").write_text("| turn | [sig=Y]\n", encoding="utf-8")
    (populated_vault / "README.md").write_text("# The vault\n", encoding="utf-8")

    listed = {m.path.resolve() for m in vault.list_memories()}
    indexed = {p.resolve() for p in vault.iter_indexable_md(populated_vault)}
    assert listed == indexed, f"only in list: {listed - indexed}; only in index: {indexed - listed}"


def test_excluded_directories(populated_vault: Path) -> None:
    for excluded in ("archive", "_setup", ".index"):
        memory(populated_vault / excluded / "x.md", "x", "project", "Body.")
    listed = {m.path.name for m in vault.list_memories()}
    indexed = {p.name for p in vault.iter_indexable_md(populated_vault)}
    assert "x.md" not in listed
    assert "x.md" not in indexed


def test_predicate_rejects_paths_outside_the_vault(populated_vault: Path) -> None:
    assert vault.is_memory_path(Path("C:/elsewhere/note.md"), populated_vault) is False


def test_doctor_uses_the_same_list(populated_vault: Path) -> None:
    """doctor must not carry its own copy of the bookkeeping filenames."""
    assert doctor._non_memory_names() is vault.EXCLUDE_FILES


def test_no_module_redeclares_the_exclusion_list() -> None:
    """A second literal list is how these drifted apart in the first place."""
    package = Path(vault.__file__).resolve().parent
    offenders = []
    for source in sorted(package.glob("*.py")):
        if source.name == "vault.py":
            continue
        text = source.read_text(encoding="utf-8")
        if "activity.md" in text and "EXCLUDE_FILES" not in text:
            offenders.append(source.name)
    assert not offenders, (
        f"{offenders} name bookkeeping files literally instead of using "
        f"vault.EXCLUDE_FILES / vault.is_memory_path"
    )


def test_render_list_does_not_surface_bookkeeping(populated_vault: Path) -> None:
    (populated_vault / "activity.md").write_text("| turn | [sig=Y]\n", encoding="utf-8")
    out = render.render_list(render.list_payload())
    assert "activity.md" not in out
    assert "unknown" not in out, "bookkeeping files were the source of the 'unknown' type bucket"
