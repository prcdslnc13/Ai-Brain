---
paths:
  - "mcp-server/brain_mcp/vault.py"
  - "mcp-server/brain_mcp/cli.py"
  - "mcp-server/brain_mcp/server.py"
  - "mcp-server/brain_mcp/compact.py"
  - "mcp-server/brain_mcp/transcript.py"
  - "mcp-server/brain_mcp/render.py"
  - "hooks/*.py"
  - "mcp-server/tests/test_save_*.py"
  - "mcp-server/tests/test_checkpoint_*.py"
  - "mcp-server/tests/test_compact_rollups.py"
  - "mcp-server/tests/test_project_path_safety.py"
  - "mcp-server/tests/test_memory_path_predicate.py"
  - "mcp-server/tests/test_payload_caps.py"
  - "mcp-server/tests/test_vault_line_endings.py"
---

# Rules: writing to the vault

Each rule has a case history in `docs/GOTCHAS.md` under the same lead phrase: what
happened, what was measured, and why the fix has the shape it has. Read that entry before
changing anything a rule guards.

- **A `project` value is a directory basename and nothing else.** `vault.validate_project_name()`
  is the predicate (a blacklist, not a whitelist), `vault.project_dir()` the only path builder,
  `vault.projects_root()` the only enumerator. Hooks take the directory from `_common.project_dir()`
  (`CLAUDE_PROJECT_DIR` first; the payload's `cwd` follows `cd`). `project_basename()` returns None and never raises;
  doctor downgrades to `PROJECT_NAME_INVALID`; the CLI exits 2; the MCP server returns an error
  result. A test fails on any direct join under `"projects"`.
- **Checkpoint filenames are claimed with `O_EXCL`** (`_reserve_checkpoint_path`), to the second,
  with a zero-padded `_02` disambiguator (underscore so it sorts after `.`). The reservation is a
  real empty `.md`, unlinked on a failed write. `_atomic_write` temp names carry pid + counter and
  never end in `.md`.
- **Frontmatter is built with `vault._frontmatter()` and written with `vault._atomic_write()`**,
  never f-strings + `write_text`. Every text write into the vault pins `newline="\n"`; text mode
  on Windows writes CRLF otherwise (`test_vault_line_endings.py`). The CLI forces UTF-8 on stdio; `--project` filtering uses
  `vault.path_in_project()`. Doctor flags `MALFORMED_FRONTMATTER`.
- **`vault.is_memory_path()` is the only answer to "is this a memory".** Route every enumeration
  through it; a test fails on literal bookkeeping filenames elsewhere.
- **`brain list` is capped, round-robin across types.** Don't simplify it to a slice — path order
  dropped whole categories. Omissions are reported with the filters that would narrow them.
- **Recall/list output is capped; don't remove the caps.** `top_k` defaults to 3 and clamps at
  `BRAIN_RECALL_MAX_K` (10); `BRAIN_RECALL_PREVIEW_CHARS`, `BRAIN_RECALL_MAX_BODY_CHARS` (6000),
  `BRAIN_RECALL_MAX_TOTAL_CHARS` (20000); sessions excluded unless asked. Both frontends route
  through `render.py`.
- **`Path.resolve()` is not prefix-stable on Windows.** Compare resolved paths through
  `vault._comparable()`, always.
- **A save that replaces a memory archives the previous version** under
  `Brain/archive/versions/` (`VERSION_KEEP`=5, monotonic names) and reports it; a byte-identical
  re-save writes nothing; `slugify` transliterates via NFKD and hashes when no ASCII survives;
  caller frontmatter is honoured only as a mapping; `forget_memory` requires a `.md` that
  `is_memory_path` accepts, and archives it to `archive/versions/` the same way before deleting.
- **brain-compact buckets and ages by the date in the filename, never mtime**, merges by
  `<!-- brain-compact source: … -->` sections, reclaims fully-absorbed sources, and merges into
  existing archives. It never rolls up a project's newest checkpoint (by name). Test fixtures use
  today-relative stamps.
