"""`brain_session_start` is sized by its own knob, and a model can only shrink it.

The MCP server's readers are local models (LM Studio and the like) whose windows are
a fraction of Claude Code's. Until 2026-10-01 the only way to size their bundle was
`BRAIN_BUNDLE_BUDGET_KB`, which also sizes every Claude Code session: a 48 KB value
set in a Claude Code `settings.json` to protect local models skipped ten memories
from Opus sessions and never reached LM Studio, whose server kept loading 72 KB.

So `BRAIN_MCP_BUDGET_KB` is the MCP ceiling (falling back to the shared name), and
the tool's `budget_kb` argument can lower it but never raise it: the operator sized
the config for the model's window, and a model that asks for more is asking to
overflow it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from brain_mcp import server


def _start(**args) -> dict:
    out = asyncio.run(server.call_tool("brain_session_start", args))
    return json.loads(out[0].text)


def _labels(bundle: dict) -> list[str]:
    return [s["label"] for s in bundle["sections"]]


def test_mcp_knob_sizes_the_mcp_bundle(populated_vault: Path, monkeypatch) -> None:
    monkeypatch.setenv("BRAIN_BUNDLE_BUDGET_KB", "72")
    monkeypatch.setenv("BRAIN_MCP_BUDGET_KB", "16")
    assert _start(project="Widget")["budget_limit_kb"] == 16.0


def test_shared_knob_still_applies_when_mcp_knob_unset(populated_vault: Path,
                                                        monkeypatch) -> None:
    """An MCP config written before the new name keeps its meaning."""
    monkeypatch.setenv("BRAIN_BUNDLE_BUDGET_KB", "20")
    assert _start(project="Widget")["budget_limit_kb"] == 20.0


def test_argument_lowers_but_never_raises_the_ceiling(populated_vault: Path,
                                                      monkeypatch) -> None:
    monkeypatch.setenv("BRAIN_MCP_BUDGET_KB", "16")
    assert _start(budget_kb=8)["budget_limit_kb"] == 8.0
    assert _start(budget_kb=500)["budget_limit_kb"] == 16.0


@pytest.mark.parametrize("bad", ["lots", 0, -4, "nan", "inf", [1]])
def test_bad_budget_is_an_error_not_a_default(populated_vault: Path, bad) -> None:
    out = asyncio.run(server.call_tool("brain_session_start", {"budget_kb": bad}))
    assert "error" in json.loads(out[0].text)


def test_slim_drops_overview_and_checkpoint(populated_vault: Path) -> None:
    full = _labels(_start(project="Widget"))
    slim = _labels(_start(project="Widget", slim=True))
    assert any("overview" in label for label in full)
    assert not any("overview" in label or "session" in label for label in slim)
    assert any(label == "feedback" for label in slim)


def test_schema_advertises_the_new_arguments() -> None:
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    props = tools["brain_session_start"].inputSchema["properties"]
    assert props["budget_kb"]["type"] == "number"
    assert props["slim"]["type"] == "boolean"
