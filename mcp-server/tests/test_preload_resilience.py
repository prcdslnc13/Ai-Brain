"""The preload must survive its own inputs.

Three ways the SessionStart preload was lost wholesale, all found 2026-09-01:

- F5: pinned items (overview, latest checkpoint) never checked the budget, and the
  never-return-an-empty-bundle guard counted them — so one 80 KB checkpoint consumed
  the budget and refused every user and feedback entry while MEMORY_SIZES_OK reported
  clean (that check scans user/feedback only).
- F24: `BRAIN_BUNDLE_BUDGET_KB=inf` raised OverflowError at `int()`, `nan` raised
  ValueError outside the guarded parse; a file vanishing between glob and stat raised
  out of a sort key.
- F4: `doctor.check()` ran eighteen checks with no isolation, and the hook treated any
  exception as a fatal vault error and exited before building the bundle.

The tests here assert the *class* in each case: a pinned item can never starve the
elastic sections, a budget knob can never raise, a vanished file can never raise, and
a raising check can never cost the preload.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from brain_mcp import doctor, vault

from conftest import memory


# ------------------------------------------------------------------ F5: pinned items

def _big_checkpoint(brain: Path, project: str, size: int) -> Path:
    sessions = brain / "projects" / project / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    p = sessions / "2026-09-01-100000-test-host.md"
    p.write_text("---\nproject: " + project + "\n---\n\n" + ("checkpoint line\n" * (size // 16)),
                 encoding="utf-8")
    # Newest by a margin no filesystem timestamp granularity can blur.
    future = p.stat().st_mtime + 3600
    os.utime(p, (future, future))
    return p


def test_an_oversized_checkpoint_cannot_starve_the_elastic_sections(populated_vault: Path):
    """The reproduced failure: 81.98/72 KB consumed, 5 user + 5 feedback skipped, 0 loaded."""
    _big_checkpoint(populated_vault, "Widget", 80_000)
    for i in range(5):
        memory(populated_vault / "user" / f"u{i}.md", f"u{i}", "user", "fact " * 50)
        memory(populated_vault / "feedback" / f"f{i}.md", f"f{i}", "feedback", "rule " * 50)

    bundle = vault.session_start_bundle("Widget")
    labels = {s["label"]: len(s["items"]) for s in bundle["sections"]}
    assert labels.get("user", 0) == 6 and labels.get("feedback", 0) == 6, labels
    assert bundle["skipped_sections"] == {}
    assert bundle["budget_consumed_kb"] < bundle["budget_limit_kb"]


def test_pinned_items_are_clipped_to_their_cap_with_a_marker(populated_vault: Path):
    cp = _big_checkpoint(populated_vault, "Widget", 80_000)
    overview = populated_vault / "projects" / "Widget" / "overview.md"
    overview.write_text("---\nname: overview\ntype: project\n---\n\n" + ("x" * 20_000), encoding="utf-8")

    bundle = vault.session_start_bundle("Widget")
    by_label = {s["label"]: s["items"][0]["content"] for s in bundle["sections"]}
    latest = by_label["project:Widget:latest-session"]
    ov = by_label["project:Widget:overview"]
    assert len(latest) <= vault.pinned_max_chars("checkpoint")
    assert len(ov) <= vault.pinned_max_chars("overview")
    assert "preload clipped" in latest and cp.name in latest, "the marker says where the rest is"
    assert "preload clipped" in ov and "brain recall overview" in ov
    assert sorted(bundle["pinned_clipped"]) == sorted(
        [str(cp.relative_to(populated_vault.parent)), str(overview.relative_to(populated_vault.parent))]
    )
    assert bundle["pinned_kb"] > 0


def test_pinned_cap_knob(populated_vault: Path, monkeypatch: pytest.MonkeyPatch):
    _big_checkpoint(populated_vault, "Widget", 20_000)
    monkeypatch.setenv("BRAIN_PRELOAD_PINNED_MAX_CHARS", "500")
    bundle = vault.session_start_bundle("Widget")
    latest = next(s for s in bundle["sections"] if s["label"].endswith("latest-session"))
    assert len(latest["items"][0]["content"]) <= 500


def test_small_pinned_items_are_not_clipped(populated_vault: Path):
    bundle = vault.session_start_bundle("Widget")
    assert bundle["pinned_clipped"] == []
    latest = next(s for s in bundle["sections"] if s["label"].endswith("latest-session"))
    assert "preload clipped" not in latest["items"][0]["content"]


def test_the_tiny_budget_guard_still_ships_one_elastic_item_after_a_pinned_one(
    populated_vault: Path,
):
    """The guard counts elastic items now; a pinned item must not satisfy it."""
    memory(populated_vault / "user" / "big.md", "big", "user", "x" * 4000)
    bundle = vault.session_start_bundle("Widget", budget_kb=0.5)
    labels = {s["label"] for s in bundle["sections"]}
    assert "user" in labels or "feedback" in labels, (
        "a tight budget with pinned items present must still ship one elastic entry"
    )


def test_doctor_flags_oversized_pinned_items(populated_vault: Path):
    cp = _big_checkpoint(populated_vault, "Widget", 80_000)
    findings = doctor._check_pinned_sizes(populated_vault)
    assert [f.code for f in findings] == ["OVERSIZED_PINNED"]
    assert cp.name in findings[0].message


def test_doctor_pinned_check_is_clean_on_small_items(populated_vault: Path):
    assert [f.code for f in doctor._check_pinned_sizes(populated_vault)] == ["PINNED_SIZES_OK"]


# ---------------------------------------------------------------- F24: knobs and files

@pytest.mark.parametrize("value", ["inf", "-inf", "nan", "0", "-5", "abc", "", "  "])
def test_budget_knob_never_raises(vault_dir: Path, monkeypatch: pytest.MonkeyPatch, value: str):
    memory(vault_dir / "user" / "u.md", "u", "user", "fact")
    monkeypatch.setenv("BRAIN_BUNDLE_BUDGET_KB", value)
    monkeypatch.setenv("BRAIN_SUBAGENT_BUDGET_KB", value)
    bundle = vault.session_start_bundle()
    assert bundle["budget_limit_kb"] == vault.BUNDLE_BUDGET_DEFAULT_KB
    assert vault.subagent_budget_kb() == vault.SUBAGENT_BUDGET_DEFAULT_KB
    slim = vault.session_start_bundle(budget_kb=vault.subagent_budget_kb(), slim=True)
    assert sum(len(s["items"]) for s in slim["sections"]) >= 1


def test_budget_knob_is_clamped_to_a_finite_ceiling(vault_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BRAIN_BUNDLE_BUDGET_KB", "1e300")
    assert vault.bundle_budget_kb() == vault.BUDGET_MAX_KB
    assert vault.session_start_bundle()["budget_limit_kb"] == vault.BUDGET_MAX_KB


def test_explicit_nonfinite_budget_argument_falls_back(vault_dir: Path):
    memory(vault_dir / "user" / "u.md", "u", "user", "fact")
    for bad in (float("inf"), float("nan"), 0.0, -1.0):
        assert vault.session_start_bundle(budget_kb=bad)["budget_limit_kb"] == (
            vault.BUNDLE_BUDGET_DEFAULT_KB
        )


def test_a_file_that_vanishes_mid_sort_does_not_raise(vault_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Sort keys go through `safe_mtime`; a raising stat must degrade, not raise."""
    for i in range(3):
        memory(vault_dir / "feedback" / f"f{i}.md", f"f{i}", "feedback", "rule")
    real_stat = Path.stat
    doomed = (vault_dir / "feedback" / "f1.md").resolve()

    def flaky_stat(self, *a, **kw):
        if self.resolve() == doomed:
            raise FileNotFoundError(str(self))
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    bundle = vault.session_start_bundle()
    names = [i["path"] for s in bundle["sections"] for i in s["items"]]
    assert any(n.endswith("f0.md") for n in names) and any(n.endswith("f2.md") for n in names)


def test_zero_byte_checkpoints_are_skipped_when_picking_the_latest(populated_vault: Path):
    """A reservation left behind by a crashed writer must not become 'the latest session'."""
    sessions = populated_vault / "projects" / "Widget" / "sessions"
    real = sessions / "2026-01-01-1200-test-host.md"
    empty = sessions / "2026-09-01-120000-test-host.md"
    empty.write_text("", encoding="utf-8")
    assert vault.latest_checkpoint(sessions) == real
    bundle = vault.session_start_bundle("Widget")
    latest = next(s for s in bundle["sections"] if s["label"].endswith("latest-session"))
    assert latest["items"][0]["path"].endswith(real.name)


PRELOAD_PATH_MODULES = ("vault.py", "doctor.py", "brain_prep.py")
PRELOAD_PATH_HOOKS = ("session_start.py", "subagent_start.py")


def test_no_sort_key_on_the_preload_path_stats_unguarded():
    """The class: `key=lambda p: p.stat()...` on the preload path is the bug coming back.

    Scoped to the modules a SessionStart runs through — one raise there costs the
    session every memory. (`compact.py` and `transcript.py` carry the same pattern
    off this path; they degrade one operation, not the preload.)
    """
    import re
    pkg = Path(vault.__file__).parent
    files = [pkg / m for m in PRELOAD_PATH_MODULES]
    files += [pkg.parent.parent / "hooks" / h for h in PRELOAD_PATH_HOOKS]
    offenders = []
    for py in files:
        text = py.read_text(encoding="utf-8")
        for m in re.finditer(r"key=lambda\s+(\w+):\s*\1\.stat\(\)", text):
            offenders.append(f"{py.name}: {m.group(0)}")
        for m in re.finditer(r"\b(\w+)\.stat\(\)\.st_mtime\s+for\s+\1\s+in\b", text):
            offenders.append(f"{py.name}: {m.group(0)}")
    assert not offenders, "unguarded stat() in a sort key: " + "; ".join(offenders)


# ------------------------------------------------------------- F4: doctor isolation

def test_a_raising_check_becomes_a_finding_not_an_exception(populated_vault: Path,
                                                            monkeypatch: pytest.MonkeyPatch):
    def boom(brain):
        raise RuntimeError("synthetic check failure")

    monkeypatch.setattr(doctor, "_check_frontmatter", boom)
    findings = doctor.check("Widget", None)
    failed = [f for f in findings if f["code"] == "CHECK_FAILED"]
    assert len(failed) == 1 and "frontmatter" in failed[0]["message"]
    assert failed[0]["severity"] == "info"
    # Every other check still ran.
    assert "BUNDLE_BUDGET_OK" in {f["code"] for f in findings}


def test_check_stashes_the_bundles_it_built(populated_vault: Path):
    cache: dict = {}
    doctor.check("Widget", None, bundle_cache=cache)
    assert set(cache) == {"session", "subagent"}
    assert cache["session"]["budget_limit_kb"] == vault.BUNDLE_BUDGET_DEFAULT_KB
    assert cache["subagent"]["budget_limit_kb"] == vault.SUBAGENT_BUDGET_DEFAULT_KB
    assert not any(s["label"].endswith("latest-session") for s in cache["subagent"]["sections"])


@pytest.mark.parametrize("fm, reason", [
    ("---\nname: x\ntype: [user]\n---\n\nbody\n", "type is ['user']"),
    ("---\n- just\n- a list\n---\n\nbody\n", "not a mapping"),
])
def test_frontmatter_check_reports_odd_yaml_instead_of_raising(vault_dir: Path, fm: str, reason: str):
    (vault_dir / "user" / "odd.md").write_text(fm, encoding="utf-8")
    findings = doctor._check_frontmatter(vault_dir)
    assert findings[0].code == "MALFORMED_FRONTMATTER"
    assert reason in findings[0].message


def test_frontmatter_check_reports_a_cp1252_note_instead_of_raising(vault_dir: Path):
    (vault_dir / "user" / "latin.md").write_bytes(b"---\nname: caf\xe9\ntype: user\n---\n\nbody\n")
    findings = doctor._check_frontmatter(vault_dir)
    assert findings[0].code == "MALFORMED_FRONTMATTER"
    assert "not valid UTF-8" in findings[0].message


def test_frontmatter_readers_tolerate_non_mapping_yaml_and_bad_bytes(vault_dir: Path):
    listy = vault_dir / "user" / "listy.md"
    listy.write_text("---\n- a\n- b\n---\n\nbody\n", encoding="utf-8")
    latin = vault_dir / "user" / "latin.md"
    latin.write_bytes(b"---\nname: caf\xe9\ntype: user\nstub: true\n---\n\nbody\n")
    for p in (listy, latin):
        assert vault.read_frontmatter_type(p) is None
        assert vault.is_overview_stub(p) is False


def test_stale_checks_survive_a_checkpoint_vanishing(populated_vault: Path,
                                                    monkeypatch: pytest.MonkeyPatch):
    real_stat = Path.stat
    doomed = (populated_vault / "projects" / "Widget" / "sessions").resolve()

    def flaky_stat(self, *a, **kw):
        if self.parent.resolve() == doomed:
            raise FileNotFoundError(str(self))
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    assert doctor._check_stale_checkpoint(populated_vault, "Widget") == []
    assert doctor._check_stale_uncommitted(populated_vault, "Widget", populated_vault.parent) == []


# ---------------------------------------------------- the hook keeps the bundle

def _run_session_start(monkeypatch: pytest.MonkeyPatch, capsys, argv: list[str], cwd: str) -> dict | None:
    import session_start

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": cwd, "hook_event_name": "SessionStart"})))
    monkeypatch.setenv("BRAIN_AUTO_REINDEX", "0")
    try:
        session_start.main(argv)
    except SystemExit as exc:  # early exits are exit 0; a nonzero one is a bug
        assert exc.code in (0, None)
    out = capsys.readouterr().out
    return json.loads(out) if out.strip() else None


def test_hook_still_emits_the_bundle_when_a_check_raises(populated_vault: Path, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch, capsys):
    def boom(brain):
        raise RuntimeError("synthetic check failure")

    monkeypatch.setattr(doctor, "_check_frontmatter", boom)
    out = _run_session_start(monkeypatch, capsys, [], str(tmp_path / "Widget"))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert vault.MEMORY_FENCE_BEGIN in ctx, "the preload was lost to a check's own bug"
    assert "prefers-rust" in ctx


def test_hook_still_emits_the_bundle_when_the_whole_doctor_raises(populated_vault: Path, tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch, capsys):
    def boom(*a, **kw):
        raise RuntimeError("doctor exploded")

    monkeypatch.setattr(doctor, "check", boom)
    out = _run_session_start(monkeypatch, capsys, [], str(tmp_path / "Widget"))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "DOCTOR_FAILED" in ctx and vault.MEMORY_FENCE_BEGIN in ctx
    assert "prefers-rust" in ctx


def test_hook_banner_names_the_installer_that_exists():
    import session_start

    text = session_start._import_failure_banner("x", RuntimeError("y"))
    assert "brain-setup.py" in text
    assert "setup-mac" not in text and "setup-windows" not in text
