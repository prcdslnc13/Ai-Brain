# Ai-Brain Code Review Remediation Plan

Review date: 2026-08-25 · **Remediated 2026-08-25** — all five items implemented; suite 96 → 345 passing, plus a new `npm run typecheck` for the pi extension. `[~]` marks a task completed with a stated limitation rather than in full.

Scope: the shared Python memory core, Claude Code hooks and installers, MCP interface, and pi integration.

Baseline validation: the existing test suite passes (`96 passed`). The review also reproduced project-path traversal outside `Brain/` and same-minute checkpoint overwrites using an isolated temporary vault.

## Priority order

1. Prevent project-path traversal.
2. Preserve unrelated Claude hooks during installation.
3. Prevent checkpoint filename collisions.
4. Preserve malformed `settings.json` files instead of replacing them.
5. Restrict the preapproved Brain CLI surface.

## 1. Prevent project-path traversal

Severity: High

Affected areas:

- `mcp-server/brain_mcp/vault.py::write_memory`
- `mcp-server/brain_mcp/vault.py::list_memories`
- `mcp-server/brain_mcp/vault.py::session_start_bundle`
- `mcp-server/brain_mcp/vault.py::ensure_project_overview_stub`
- `mcp-server/brain_mcp/vault.py::write_checkpoint`
- Project arguments exposed by the CLI, MCP server, doctor, compactor, and pi extension

Problem: project values are joined directly into filesystem paths. Values containing `..`, path separators, or absolute paths can escape `Brain/projects/`. This permits writes outside the vault and can make read operations enumerate or preload unrelated Markdown files.

### Implementation tasks

- [x] Add one shared `validate_project_name()` or `project_path()` helper in `vault.py`.
- [x] Require a non-empty basename rather than accepting a path.
- [x] Reject `.`, `..`, absolute paths, `/`, `\`, drive-qualified paths, and platform-specific alternate separators.
- [x] Decide and document the allowed character set and maximum length for project names.
- [x] Resolve the constructed path and verify it remains below the resolved `Brain/projects/` directory.
- [x] Route every project-derived read and write through the shared helper.
- [x] Return a concise validation error through both CLI and MCP interfaces.
- [x] Review `brain-compact --project` and doctor project checks for the same containment requirement.

### Verification tasks

- [x] Test valid project names on Windows and POSIX path semantics.
- [x] Test `..`, `../x`, `..\x`, absolute POSIX paths, Windows drive paths, UNC paths, and mixed separators.
- [x] Assert rejected names create no files or directories outside `Brain/projects/`.
- [x] Assert list, recall, preload, doctor, and compact cannot read outside the project root.
- [x] Add CLI and MCP tests confirming validation failures are reported without server termination.

## 2. Preserve unrelated Claude hooks during installation

Severity: High

Affected areas:

- `brain-setup.py::merge_settings_json`
- `setup-linux.sh`
- `setup-mac.sh`
- `setup-windows.ps1`

Problem: each installer prunes existing Brain-owned hooks, but then assigns the template definition to the whole event. Existing third-party hooks for events such as `SessionStart`, `Stop`, and `PreCompact` are discarded.

### Implementation tasks

- [x] Keep the current Brain-owned hook detection and pruning behavior.
- [x] Append current Brain hook groups to each event's surviving groups instead of replacing the event.
- [x] Preserve unknown and malformed non-Brain entries wherever possible.
- [x] Ensure rerunning setup does not duplicate Brain hooks.
- [x] Use the same merge algorithm in the Python, Linux, macOS, and Windows installers.
- [x] Consider moving the merge algorithm into a small shared stdlib-only Python utility invoked by all installers to prevent parity drift.
- [x] Update installer comments that currently claim unrelated hooks are preserved.

### Verification tasks

- [x] Seed every supported hook event with a third-party hook, run setup twice, and assert it remains exactly once.
- [x] Assert every expected Brain hook is present exactly once after repeated installation.
- [x] Run uninstall and assert only Brain-owned hooks are removed.
- [x] Test events containing multiple groups, matchers, and multiple inner hooks.
- [x] Extend `test_installer_parity.py` to test behavior, not only template/string parity.

## 3. Prevent checkpoint filename collisions

Severity: Medium

Affected area: `mcp-server/brain_mcp/vault.py::write_checkpoint`

Problem: checkpoint filenames contain only minute precision and the machine name. Multiple checkpoints for the same project and machine during one minute resolve to the same path, causing the later checkpoint to overwrite the earlier one.

### Implementation tasks

- [x] Choose a collision-resistant naming scheme, such as seconds plus microseconds or a stable session ID plus timestamp.
- [x] Preserve the machine suffix because it is useful for cross-machine diagnosis.
- [x] Use exclusive creation or a retrying numeric suffix so uniqueness does not depend solely on clock resolution.
- [x] Keep filenames sortable in chronological order.
- [x] Confirm compact, preload, stats, and archive logic do not depend on the current minute-only filename shape.
- [~] Considered and deliberately declined for the Claude Code hook path: PreCompact and SessionEnd legitimately capture different transcript states, and two distinct files is the right outcome now that they no longer overwrite. pi/cherryd already dedupe via `Brain/.state/harness-checkpoints.json`.

### Verification tasks

- [x] Write multiple checkpoints in the same second and assert every body remains available.
- [~] Exercise concurrent writers from separate processes. **Threads, not processes** — 32-40 concurrent writers in-process; `O_EXCL` makes the guarantee cross-process, but that is argued, not measured here.
- [x] Verify filenames remain valid on Windows, macOS, and Linux.
- [x] Verify latest-session selection still chooses the newest checkpoint.
- [x] Verify compaction handles the new names without special cases.

## 4. Preserve malformed `settings.json`

Severity: Medium

Affected areas:

- `brain-setup.py::merge_settings_json`
- Embedded merge scripts in `setup-linux.sh`, `setup-mac.sh`, and `setup-windows.ps1`

Problem: invalid JSON is treated as `{}` and then written back, silently erasing the original Claude configuration. The uninstallers already follow the safer policy of leaving an unparseable file untouched.

### Implementation tasks

- [x] Abort the settings merge when existing JSON cannot be parsed.
- [x] Print the filename, parser error, and a clear repair instruction.
- [x] Leave the original file byte-for-byte unchanged on failure.
- [x] Create a timestamped backup before mutating a valid existing settings file.
- [x] Write updated settings atomically through a same-directory temporary file and replace.
- [x] Apply identical failure behavior across every installer.
- [x] Ensure setup exits nonzero or clearly reports a partial installation when settings integration fails.

### Verification tasks

- [x] Test malformed JSON, empty files, wrong top-level JSON types, and interrupted/partial JSON.
- [x] Assert malformed inputs remain byte-identical after setup fails.
- [x] Assert a valid backup exists before a successful mutation.
- [x] Simulate a write/replace failure and assert the original settings survive.
- [x] Verify non-settings installation steps are reported accurately when the merge fails partway through setup.

## 5. Restrict the preapproved Brain CLI surface

Severity: Medium

Affected areas:

- Installer-generated Claude permission rules
- `mcp-server/brain_mcp/cli.py::_read_body`
- `brain save --file`
- `brain checkpoint --from-pi` and `--from-cherryd`

Problem: setup preapproves the Brain command with arbitrary trailing arguments. The CLI includes file-import operations that can read any file accessible to the process. A prompt-injected agent could copy sensitive local content into the vault and retrieve it through normal memory operations.

### Implementation tasks

- [x] Define which commands are safe for unattended model invocation.
- [x] Separate agent-facing memory operations from administrative/import operations, either through separate executables or enforceable command prefixes.
- [x] Remove `--file`, `--from-pi`, and `--from-cherryd` from the preapproved agent-facing surface.
- [x] Keep explicit operator workflows available without silently approving them.
- [x] Narrow generated permission rules to the smallest supported command shapes.
- [x] Document the security boundary and the effect of enabling proactive saves.
- [~] Reviewed: yes, it can. Deferred deliberately — tracked as ROADMAP 3F, because a real fix spans the bundle, both hooks, brain_prep and the pi preload.

### Verification tasks

- [x] Confirm ordinary recall, inline save, list, stats, and checkpoint workflows still operate as intended.
- [x] Confirm arbitrary file import requires explicit approval.
- [x] Test attempts to pass `--file`, `--from-pi`, or `--from-cherryd` through the unattended command path.
- [~] Inspected on **Windows** (generated brain.cmd + settings.json verified end to end); the POSIX shapes are covered by unit tests, not a real install.
- [x] Add regression tests for argument smuggling and alternate option ordering.

## Cross-cutting completion tasks

- [x] Add regression tests before or alongside each fix.
- [~] Run the complete Python test suite on Windows and at least one POSIX platform. **Windows only** — no POSIX host available here; the shell installers were checked with `bash -n` and the PowerShell ones with an AST parse.
- [x] Add an automated TypeScript typecheck/test command for the pi extension if one is not already available.
- [x] Exercise CLI, MCP, Claude hooks, and pi against the same temporary vault fixtures to verify semantic parity.
- [x] Run install/reinstall/uninstall scenarios with pre-existing Claude settings and third-party hooks.
- [x] Update setup documentation for any validation, permission, or filename behavior changes.
- [x] Run `brain doctor` against a representative synchronized vault after the changes.

## Definition of done

- No user-controlled project value can read or write outside `Brain/projects/`.
- Installation and uninstallation preserve every unrelated Claude setting and hook.
- Distinct checkpoint events cannot overwrite one another.
- Invalid configuration files are never silently replaced.
- Preapproved model operations cannot import arbitrary local files.
- All existing tests and the new regression tests pass on supported platforms.
