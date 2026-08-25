"""Every recall/list payload must be bounded.

`render.py` exists because an uncapped recall once handed a local model 200k+ tokens
in a single call. `list` was then written without any cap, which reintroduced the same
failure through a different door: measured 2026-08-24 on a 917-file vault, a default
`brain list` rendered 57 KB and `--include-sessions` rendered 140 KB (~35k tokens),
growing by one checkpoint per session forever.

These tests assert the *property* — bounded output — rather than any particular
rendering, so a future frontend cannot reintroduce an unbounded path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brain_mcp import render, vault

from conftest import memory


@pytest.fixture
def big_vault(vault_dir: Path) -> Path:
    """A vault large enough that an uncapped payload would be obviously too big."""
    for i in range(400):
        memory(
            vault_dir / "projects" / "Widget" / f"note-{i:03d}.md",
            f"note {i}", "project",
            f"Body for note {i}. " + ("filler " * 40),
            description="A fairly long description that eats budget. " * 3,
        )
    for i in range(300):
        (vault_dir / "projects" / "Widget" / "sessions").mkdir(parents=True, exist_ok=True)
        (vault_dir / "projects" / "Widget" / "sessions" / f"2026-01-{i % 28 + 1:02d}-{i:04d}.md").write_text(
            "---\nproject: Widget\n---\n\nSession work.\n", encoding="utf-8"
        )
    return vault_dir


def test_list_is_capped(big_vault: Path) -> None:
    payload = render.list_payload()
    assert payload["count"] <= render.max_list_items()
    assert payload["omitted"] == payload["total_matches"] - payload["count"]
    assert payload["omitted"] > 0, "fixture should exceed the cap or it proves nothing"


def test_list_rendering_stays_bounded(big_vault: Path) -> None:
    """The rendered markdown is what actually reaches a model's context."""
    out = render.render_list(render.list_payload())
    ceiling = render.max_list_total_chars() * 2  # paths + markup on top of the budget
    assert len(out) < ceiling, f"rendered list was {len(out)} chars"


def test_truncation_never_drops_a_whole_type(vault_dir: Path) -> None:
    """Truncation must take from every type, not wipe the ones that sort last.

    `list_memories` returns path order — feedback, projects, references, user — so
    capping it in place emptied the entire user and reference buckets while the huge
    project bucket was still being emitted. Losing a category is far worse than
    losing a slice of each, and user/feedback are the two the model's behaviour
    actually depends on.
    """
    for i in range(400):
        memory(vault_dir / "projects" / "Widget" / f"p-{i:03d}.md", f"p{i}", "project",
               "Project note. " + ("filler " * 40))
    for i in range(12):
        memory(vault_dir / "user" / f"u-{i:02d}.md", f"u{i}", "user", "A user fact.")
    for i in range(9):
        memory(vault_dir / "references" / f"r-{i:02d}.md", f"r{i}", "reference", "A pointer.")

    payload = render.list_payload()
    assert payload["omitted"] > 0, "fixture must exceed the cap or this proves nothing"

    shown = {}
    for entry in payload["memories"]:
        shown[entry["type"]] = shown.get(entry["type"], 0) + 1
    assert shown.get("user") == 12, f"user memories were truncated away: {shown}"
    assert shown.get("reference") == 9, f"reference memories were truncated away: {shown}"
    assert shown.get("project", 0) > 0
    assert shown["project"] < 400, "the large bucket should absorb the truncation"


def test_list_with_sessions_is_capped(big_vault: Path) -> None:
    """`include_sessions` is the unbounded-growth path: checkpoints accrue forever."""
    payload = render.list_payload(include_sessions=True)
    assert payload["count"] <= render.max_list_items()
    out = render.render_list(render.list_payload(include_sessions=True))
    assert len(out) < render.max_total_chars() * 2


def test_omission_is_reported_not_silent(big_vault: Path) -> None:
    """A truncated enumeration that looks complete is worse than a short one."""
    out = render.render_list(render.list_payload())
    assert "omitted" in out
    assert "--type" in out or "BRAIN_LIST_MAX_ITEMS" in out


def test_list_cap_is_tunable(big_vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_LIST_MAX_ITEMS", "25")
    assert render.list_payload()["count"] == 25


def test_small_vault_is_not_truncated(populated_vault: Path) -> None:
    """The cap must not fire on an ordinary vault."""
    payload = render.list_payload()
    assert payload["omitted"] == 0
    assert payload["count"] == payload["total_matches"]
    assert "omitted" not in render.render_list(payload)


def test_recall_stays_capped(big_vault: Path) -> None:
    payload = render.recall_payload("note", full_body=True, top_k=999)
    assert payload["shown"] <= render.max_top_k()
    out = render.render_recall(payload)
    assert len(out) < render.max_total_chars() * 2


def test_every_render_entry_point_is_bounded() -> None:
    """No public payload builder may return an unbounded collection.

    `list_payload` was added after `recall_payload` and simply never got a cap, in a
    module whose docstring promises every client sees bounded payloads. This asserts
    the promise across the module rather than per-function.
    """
    import inspect

    builders = [
        name for name, obj in vars(render).items()
        if name.endswith("_payload") and inspect.isfunction(obj)
    ]
    assert set(builders) == {"recall_payload", "list_payload"}, (
        f"new payload builder(s) {builders} — add a cap and a test here"
    )
