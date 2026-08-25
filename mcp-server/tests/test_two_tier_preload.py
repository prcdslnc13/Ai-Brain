"""The preload carries the rule; the rationale stays one recall away.

Every feedback memory is a rule lead, a `**Why:**` recounting the incident that produced
it, and a `**How to apply:**`. The lead and the how-to-apply are directives; the Why is
evidence for judging edge cases, and it is 37% of the feedback corpus by bytes.

The property under test is that deferring it is **lossless** — nothing on disk changes
and `brain recall` still returns the whole body. That is the whole reason this is
preferable to summarizing: these files are the record of corrections the user has given,
and a lossy rewrite of that record is the failure the Brain exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brain_mcp import render, vault

from conftest import memory

RULED = """\
---
name: no force push
description: Never force-push to a shared branch.
type: feedback
---

Never force-push to a shared branch.

**Why:** on 2026-01-02 a force-push dropped three commits another session had pushed,
and the reflog on that machine had already expired. The cost is unbounded and the
benefit is tidiness.

**How to apply:** use `--force-with-lease`, and only on a branch you alone own.
"""


def test_preload_text_drops_only_the_why():
    out = vault.preload_text(RULED)
    assert "Never force-push to a shared branch." in out, "the rule must survive"
    assert "**How to apply:**" in out, "the directive must survive"
    assert "--force-with-lease" in out
    assert "reflog on that machine" not in out, "the rationale should be deferred"
    assert vault._WHY_DEFERRED_MARKER in out
    assert len(out) < len(RULED)


def test_preload_text_preserves_frontmatter():
    out = vault.preload_text(RULED)
    assert out.startswith("---\nname: no force push\n")
    assert "type: feedback" in out
    parsed = vault.Memory.from_text(Path("x.md"), out)
    assert parsed.type == "feedback", "frontmatter must still parse after trimming"
    assert parsed.name == "no force push"


def test_memory_without_a_why_is_untouched():
    plain = "---\nname: x\ntype: user\n---\n\nI prefer Rust.\n"
    assert vault.preload_text(plain) == plain


def test_memory_whose_only_substance_is_the_why_is_untouched():
    """A Why with no How runs to the end of the body — cutting it guts the memory.

    No such memory exists in the vault today; this is the guard for the one that will.
    """
    why_only = (
        "---\nname: x\ntype: feedback\n---\n\n"
        "Short rule.\n\n**Why:** " + ("a long and load-bearing explanation. " * 20) + "\n"
    )
    assert vault.preload_text(why_only) == why_only


def test_trimming_never_grows_a_memory():
    """A Why shorter than the marker must be left alone, not 'saved' into being bigger."""
    tiny = (
        "---\nname: x\ntype: feedback\n---\n\nRule.\n\n"
        "**Why:** no.\n\n**How to apply:** do it.\n"
    )
    assert len(vault.preload_text(tiny)) <= len(tiny)


def test_bundle_defers_by_default(populated_vault: Path):
    memory(populated_vault / "feedback" / "ruled.md", "ruled", "feedback", "x")
    (populated_vault / "feedback" / "ruled.md").write_text(RULED, encoding="utf-8")

    bundle = vault.session_start_bundle("Widget")
    body = _feedback_body(bundle, "ruled.md")
    assert vault._WHY_DEFERRED_MARKER in body
    assert bundle["deferred_why_kb"] > 0


def test_knob_restores_full_bodies(populated_vault: Path, monkeypatch: pytest.MonkeyPatch):
    (populated_vault / "feedback" / "ruled.md").write_text(RULED, encoding="utf-8")

    monkeypatch.setenv("BRAIN_PRELOAD_DEFER_WHY", "0")
    bundle = vault.session_start_bundle("Widget")
    body = _feedback_body(bundle, "ruled.md")
    assert "reflog on that machine" in body
    assert vault._WHY_DEFERRED_MARKER not in body
    assert bundle["deferred_why_kb"] == 0


def test_deferring_is_lossless_on_disk(populated_vault: Path):
    """The preload must never mutate the corpus it reads."""
    path = populated_vault / "feedback" / "ruled.md"
    path.write_text(RULED, encoding="utf-8")
    before = path.read_bytes()

    vault.session_start_bundle("Widget")
    vault.session_start_bundle("Widget", budget_kb=8.0, slim=True)

    assert path.read_bytes() == before, "session_start_bundle rewrote a memory file"


def test_recall_still_returns_the_why(populated_vault: Path):
    """The rationale has to be genuinely reachable, or this is just truncation."""
    (populated_vault / "feedback" / "ruled.md").write_text(RULED, encoding="utf-8")

    payload = render.recall_payload("force-push", full_body=True)
    bodies = " ".join(r["body"] for r in payload["results"])
    assert "reflog on that machine" in bodies, (
        "the deferred Why must still come back through recall — otherwise deferring it "
        "loses the user's correction history rather than relocating it"
    )


def test_pinned_sections_are_not_trimmed(populated_vault: Path):
    """Overview and checkpoints are narrative, not rules — and they are pinned anyway."""
    overview = populated_vault / "projects" / "Widget" / "overview.md"
    overview.write_text(
        "---\nname: overview\ntype: project\n---\n\nWidget does things.\n\n"
        "**Why:** because someone needed them done.\n\n**How to apply:** carefully.\n",
        encoding="utf-8",
    )
    bundle = vault.session_start_bundle("Widget")
    section = next(s for s in bundle["sections"] if s["label"].endswith(":overview"))
    assert "because someone needed them done" in section["items"][0]["content"]


def test_deferring_frees_real_budget(vault_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """The point is headroom: the same corpus must fit in a smaller bundle."""
    for i in range(20):
        (vault_dir / "feedback" / f"r-{i:02d}.md").write_text(
            RULED.replace("no force push", f"rule {i}"), encoding="utf-8"
        )

    monkeypatch.setenv("BRAIN_PRELOAD_DEFER_WHY", "0")
    full = vault.session_start_bundle(None)["budget_consumed_kb"]
    monkeypatch.setenv("BRAIN_PRELOAD_DEFER_WHY", "1")
    deferred = vault.session_start_bundle(None)["budget_consumed_kb"]

    assert deferred < full
    assert (full - deferred) / full > 0.25, (
        f"expected a meaningful saving on a Why-heavy corpus, got {full}->{deferred} KB"
    )


def test_deferring_lets_more_entries_survive_a_tight_budget(vault_dir: Path,
                                                            monkeypatch: pytest.MonkeyPatch):
    """The failure this exists to prevent: rules saved but silently never loaded."""
    for i in range(20):
        (vault_dir / "feedback" / f"r-{i:02d}.md").write_text(
            RULED.replace("no force push", f"rule {i}"), encoding="utf-8"
        )

    def loaded(defer: str) -> int:
        monkeypatch.setenv("BRAIN_PRELOAD_DEFER_WHY", defer)
        b = vault.session_start_bundle(None, budget_kb=4.0)
        return sum(len(s["items"]) for s in b["sections"] if s["label"] == "feedback")

    assert loaded("1") > loaded("0"), (
        "deferring the Why must let more rules fit under the same budget"
    )


def _feedback_body(bundle: dict, filename: str) -> str:
    section = next(s for s in bundle["sections"] if s["label"] == "feedback")
    return next(i["content"] for i in section["items"] if i["path"].endswith(filename))


def test_malformed_hook_payload_is_announced(capsys):
    """A silently project-less preload looks complete and isn't.

    Found 2026-08-25 while measuring this change: a payload whose JSON failed to parse
    produced a bundle with no project overview and no latest checkpoint, and nothing
    anywhere said so — the numbers just looked smaller.
    """
    import io
    import sys as _sys

    from _common import read_payload

    real_stdin = _sys.stdin
    _sys.stdin = io.StringIO(r'{"cwd": "D:\src\broken"}')  # \s is not a valid JSON escape
    try:
        assert read_payload() == {}
    finally:
        _sys.stdin = real_stdin
    assert "unparseable payload" in capsys.readouterr().err
