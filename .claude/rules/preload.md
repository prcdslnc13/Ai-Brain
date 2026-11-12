---
paths:
  - "mcp-server/brain_mcp/vault.py"
  - "mcp-server/brain_mcp/brain_prep.py"
  - "mcp-server/brain_mcp/render.py"
  - "mcp-server/brain_mcp/doctor.py"
  - "mcp-server/brain_mcp/server.py"
  - "hooks/session_start.py"
  - "hooks/subagent_start.py"
  - "templates/settings.hooks*.json"
  - "pi/extensions/**"
  - "mcp-server/tests/test_preload_*.py"
  - "mcp-server/tests/test_two_tier_preload.py"
  - "mcp-server/tests/test_trust_boundary.py"
  - "mcp-server/tests/test_mcp_session_budget.py"
  - "mcp-server/tests/test_doctor_*.py"
---

# Rules: the session preload, its budgets, and the trust fence

Each rule has a case history in `docs/GOTCHAS.md` under the same lead phrase: what
happened, what was measured, and why the fix has the shape it has. Read that entry before
changing anything a rule guards.

- **The preload carries the rule; `**Why:**` is deferred to recall.** `vault.preload_text()`
  replaces it with a marker in elastic sections only — lossless, nothing on disk changes,
  `BRAIN_PRELOAD_DEFER_WHY=0` restores. Project-scoping is not a further lever (measured
  2026-08-25).
- **`vault.preload_item_text()` is the one definition of an elastic memory in a preload:**
  Why deferred, frontmatter stripped (`BRAIN_PRELOAD_STRIP_FRONTMATTER=0` restores). The
  bundle is built with it and doctor's `_preload_cost` sizes with it; a test compares the two
  under every opt-out combination. Pinned items keep their frontmatter.
- **User and reference memories preload as one-line summaries; feedback never does.**
  `vault.preload_mode()` decides, `memory_summary()` renders (a usable `description`, else
  the body's first sentence), `description_quality()` is the one test of "usable", shared with
  doctor's `WEAK_DESCRIPTIONS`. `user/profile.md` and `preload: full` load whole; references
  are a last, summary-only section; `BRAIN_PRELOAD_SUMMARIES=0` restores. `_render_memory`
  writes `first_sentence(body)` as the default description. Leading frontmatter is peeled
  repeatedly (`peel_frontmatter`), because 8 memories carry two blocks.
- **`projects:` globs scope a global memory's preload to matching project names**, case-
  insensitive, failing open exactly like `machines:` (also when the session has no project);
  `BRAIN_PRELOAD_PROJECT_SCOPE=0` turns it off; both scopes must pass.
- **`machines:` in frontmatter scopes a memory's *preload* to those machines, and fails
  open.** `machine:` is saved-on and must not be used for this. Missing, empty or unreadable
  means every machine; recall, list and the index ignore the scope; scoped-out memories are
  reported in `scoped_out_items` with the machine name, never catalogued as "did not fit"
  (`BRAIN_PRELOAD_MACHINE_SCOPE=0` loads all). `user/profile.md` fills first in its section.
- **Vault content reaches a model fenced, and the fence must be unclosable from inside.**
  `vault.fence()` is the only producer; `vault.neutralize_fence()` runs where content enters the
  payload and before clipping; only `brain_prep.render` and `render.py` render vault text;
  `doctor.render_banner` defangs findings; the fence's bytes are reserved out of the budget; the
  never-empty guard counts items. The notice says "data, not instructions", never "ignore this".
- **One hook command's `additionalContext` is capped at 10,000 chars; the preload is delivered
  in `vault.PRELOAD_PARTS` parts.** `PRELOAD_PARTS`, the entry count in both hook templates and
  `brain-launch.cmd` forwarding `%2..%9` must agree (`test_preload_parts.py`). Parts arrive in
  any order and are self-describing; only part 1 runs doctor/stub/reindex; what doesn't fit is
  catalogued by name. No `--part` means the whole bundle.
- **Global feedback fills before user memories** (project feedback → global feedback → user →
  reference summaries).
  If the corpus outgrows the parts, compact `user/`; don't reorder again.
- **Each preload surface has its own budget knob; never size a local model from Claude
  Code's `settings.json`.** Hooks read `BRAIN_BUNDLE_BUDGET_KB` (72); `brain_session_start`
  reads `vault.mcp_budget_kb()` (`BRAIN_MCP_BUDGET_KB`, falling back to the shared name) and
  its `budget_kb` argument only lowers it; pi reads `BRAIN_PI_BUDGET_KB` (12) first;
  cherryd has `CHERRYD_BRAIN_BUDGET_KB`.
- **`doctor.check()` isolates every check; a check failure is never a vault failure.**
  `_run_check` turns a raise into `CHECK_FAILED`; `_read_frontmatter_head` returns None for
  anything not a UTF-8 mapping; sort keys and `max()` use `vault.safe_mtime` (a test fails on
  `key=lambda p: p.stat()` on the preload path); the hook separates an import failure from
  `DOCTOR_FAILED`; `search_memories` catches `(OSError, ValueError)` per file.
- **Pinned preload items are clipped** to `vault.pinned_max_chars()`
  (`BRAIN_PRELOAD_PINNED_MAX_CHARS`); the never-empty guard counts elastic items only;
  `latest_checkpoint()` skips 0-byte files; budget knobs are clamped finite by
  `_budget_kb_from_env`.
