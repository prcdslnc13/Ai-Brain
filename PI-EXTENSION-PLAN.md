# Plan: a Brain extension for pi

_Written 2026-08-23. **M0–M3 built and verified the same day** (see "Status" at the bottom);
M4 (packaging/tag) and M5 (say=do gate) remain._

## Why

`PI-SETUP.md` wires the Brain into pi the cheap way: an `AGENTS.md` snippet telling
the model the `brain` CLI exists, and a shell tool to run it. That works, and it
should stay. But it leaves the same gap `LOCAL-HARNESS-SETUP.md` opens with —
**nothing happens unless the model decides it should.** No preload, no
checkpoint-before-overflow, no session-end write. Those are exactly the three
things Claude Code gets from hooks and pi does not.

pi has an extension API that closes the gap. Two things make this worth building
now rather than later:

1. **pi is becoming the substrate.** PI WEB bundles a pi runtime; Paseo drives
   the pi CLI as one of its native providers. An extension installed once loads
   in the pi CLI, in PI WEB, and in pi-under-Paseo alike — see
   `super-duper-system/docs/harness-eval.md` for why those two matter.
2. **pi hands us better triggers than cherryd had.** cherryd's autosave guesses
   at overflow from a token count because claw-code gave it nothing better. pi
   fires `session_before_compact` with `reason: "threshold" | "overflow" |
   "manual"` — that is PreCompact parity, the real signal, not a proxy for it.

**pi has no MCP support and that is deliberate** (MCP tool schemas are too
token-heavy for a minimal agent) — verified again 2026-08-23: zero MCP
references anywhere in `badlogic/pi-mono`. So this is a TypeScript extension
shelling out to the `brain` CLI, not an MCP server registration. The CLI stays
the single interface; this extension is a third frontend to it alongside
`brain-mcp` and the Claude Code hooks.

## Where it lives

Here, in this repo, next to `mcp-server/`. The extension writes checkpoints in
the same format `brain-mcp` and the hooks write, and history says a second
writer in a different repo drifts: cherryd needed a commit
(`super-duper-system@14aa7fd`, "exact checkpoint parity with brain-mcp (trailing
newline)") to fix exactly that. Keeping every writer in one repo makes a format
change one commit, not three.

Layout:

```
package.json              # root manifest, so `pi install git:…` works
pi/extensions/brain.ts    # the extension (pi loads .ts directly, no build step)
```

```json
{
  "name": "ai-brain",
  "keywords": ["pi-package"],
  "pi": { "extensions": ["./pi/extensions"] }
}
```

pi package sources are npm, a git repo, or a local path — a *subdirectory* of a
git repo is not addressable, which is why the manifest goes at the repo root and
points inward. Install:

```bash
pi install git:github.com/prcdslnc13/Ai-Brain@<tag>   # pinned, any machine
pi install ~/src/Ai-Brain                             # local checkout
pi -e ~/src/Ai-Brain                                  # try it for one run
```

## Design

### Resolving the CLI

No hardcoded paths. In order: `$BRAIN_CMD`, then `<package root>/mcp-server/.venv/bin/brain`
(and `Scripts/brain.exe` on Windows), then `brain` on `PATH`. `BRAIN_VAULT` must
be set or resolvable the same way the CLI already resolves it; if neither the
command nor the vault resolves, the extension should **disable itself with one
notice and no further noise** — a broken Brain must not break the session.

Every invocation goes through one `pi.exec(cmd, args, { signal, timeout })`
wrapper that injects `BRAIN_VAULT`, enforces a timeout, and never throws into
the agent loop.

### 1. Preload — getting memory in

Hook: `before_agent_start`, first turn of the session only (track a module-level
flag; re-arm it in `session_start`, which fires again on `new`/`resume`/`fork`).

Do **not** reimplement bundle assembly. Run the existing tool:

```
brain-prep --project <basename(ctx.cwd)> --slim --budget-kb <N>
```

and return it as an injected message:

```ts
return { message: { customType: "brain-bundle", content: bundle, display: false } };
```

`--slim --budget-kb` sizing is already documented in `LOCAL-HARNESS-SETUP.md`
(≈1.5k tokens at 6 KB, ≈4k at 16 KB). Default to something a 32k-window local
model can afford — start at `--slim --budget-kb 12` and make it configurable.
Read the budget from the extension's own settings, and let `$BRAIN_BUNDLE_BUDGET_KB`
override, so the same package suits a 32k local model and a 200k frontier one.

`before_agent_start` also exposes `systemPromptOptions` (loaded `AGENTS.md`
files, skills, tool snippets). Use it to **detect an already-present
`AGENTS-brain.md` snippet and skip re-injecting the usage instructions** — the
two mechanisms should compose, not duplicate.

### 2. Tools — five of them

`pi.registerTool()` × 5: `brain_recall`, `brain_save`, `brain_checkpoint`,
`brain_forget`, `brain_list`, each a thin shell over the matching CLI
subcommand. Recall output is already capped upstream (3 preview hits, ~300-char
previews, hard total cap), so a local model cannot blow its window on one call —
no extra clamping needed here.

Two pi-specific details:

- Give each tool a `promptSnippet` so it earns a line in `Available tools`.
- `promptGuidelines` bullets are appended **flat**, with no tool-name prefix.
  Write "Use `brain_recall` when…", never "Use this tool when…" — the model
  cannot tell which tool "this" refers to.

The guidance text should be derived from `templates/AGENTS-brain.md` rather than
written fresh, so the CLI route and the extension route say the same thing.

### 3. Checkpoints — getting work out

This is the half that matters, and pi supports doing it properly. Three
triggers, all writing through `brain checkpoint`:

| Trigger | Event | Mirrors |
|---|---|---|
| About to compact | `session_before_compact` (`reason` = `threshold` \| `overflow` \| `manual`) | Claude Code's PreCompact hook |
| Cadence floor | `agent_settled` + turn counter + cooldown | cherryd's `autosave_every_turns` / `autosave_cooldown_turns` |
| Session ending | `session_shutdown` | Claude Code's SessionEnd hook |

Notes that matter:

- **Use `agent_settled`, not `agent_end`.** `agent_end` fires per low-level run;
  pi may still auto-retry, auto-compact, or drain queued follow-ups afterward.
  `agent_settled` means pi will not continue on its own.
- **`session_before_compact` replaces cherryd's token threshold.** Keep
  `ctx.getContextUsage()` for the cadence decision and for the checkpoint body,
  but the primary trigger is now the real event. This is the one place the pi
  design is strictly better than cherryd's — do not port the guesswork.
- **`session_shutdown` gives us the session-end checkpoint cherryd could never
  have** (its executor loop never exits). Verify it actually fires on process
  exit and not only on session switch — pi documents it for `/new`, `/resume`
  and `/fork`; confirm the exit path before relying on it.
- **No approval prompt.** Same reasoning as cherryd: `brain_checkpoint` is
  ask-gated as a *tool*, but autosave is the operator's own policy executing,
  not the model asking. It must not route through tool dispatch.
- **Idempotence.** Consecutive triggers must not write near-duplicate
  checkpoints. Track the last checkpoint's entry id in module state; consider
  whether to reuse `Brain/.state/harness-checkpoints.json`, which the
  `--from-cherryd` path already uses for exactly this, or keep extension state
  in `pi.appendEntry()` (durable in the session file, travels with fork/resume).
  Pick one and write down why.

The checkpoint body should be the same structural extract the hooks produce —
user turns, a tool-call histogram, the final assistant message — built from
`ctx.sessionManager.getEntries()`.

### 4. Say = do (optional, later)

The Claude Code Stop gate blocks turn-end when the model promised a save and
never made one. The pi analogue is `agent_settled`: scan the final assistant
message for the same promise phrasings, and either notify via `ctx.ui` or write
the checkpoint outright. Worth doing, but only after 1–3 are solid — the
incident this guards against (2026-04-22, MM-ToolDecoder) is already mostly
covered once compaction and shutdown both checkpoint.

## Alternative considered — `brain checkpoint --from-pi`

`--from-cherryd` reads cherryd's SQLite log from outside the harness, on a
timer, with no cooperation from the agent. pi persists sessions as files
(`sessionDir`, documented format), so a `--from-pi` reader is clearly possible
and would be harness-agnostic and immune to a wedged session.

It is a **complement, not a substitute**: post-hoc only, no preload, no tools,
and it cannot fire *before* a compaction discards the context. Build the
extension first. If unattended pi sessions become a real pattern, add `--from-pi`
afterward and run it on the same timer as the cherryd one.

## Milestones

- [x] **M0 — skeleton.** Root `package.json` manifest, `pi/extensions/brain.ts`,
      CLI resolution + `exec` wrapper, self-disable path.
- [x] **M1 — tools.** Five tools with prompt snippets and guidelines. Verified with
      qwen3.6-27b on LMStudio: `brain_recall` and `brain_save` both called and
      landed correctly (a project-scoped feedback memory, right frontmatter).
- [x] **M2 — preload.** `brain-prep --slim --budget-kb` injected as a hidden
      `brain-bundle` custom message on the first turn; guidance read from
      `templates/AGENTS-brain.md` and skipped when that snippet is already a
      loaded context file.
- [x] **M3 — checkpoints.** All three triggers wired, dedup via the shared
      `harness-checkpoints.json`, no approval prompt. Cadence and shutdown observed
      firing (and deduping against each other); **`session_before_compact` is wired
      but has not yet been seen firing** — see the note below.
- [ ] **M4 — packaging.** Tag, `pi install git:…@<tag>` on a second machine,
      confirm it loads under PI WEB and under Paseo-driven pi.
- [ ] **M5 — say=do gate.** Optional.

## Verification

- **Format parity is the thing that breaks.** Diff a checkpoint written by this
  extension against one written by `brain-mcp` for the same session — including
  the trailing newline. That exact byte cost cherryd a commit.
- Run against a local model on the geekom box, not just a frontier model: the
  whole point is the 8–32k window case where the session dies before the model
  volunteers a save.
- Force each trigger deliberately: `/compact` for manual, a long session for
  threshold, and an exit for shutdown.
- Confirm the extension is inert when `BRAIN_VAULT` is unset — a machine without
  the vault synced must still run pi normally.

## Open questions

- Does `session_shutdown` fire on process exit, or only on session switch? The
  session-end checkpoint depends on it.
- Extension settings: pi settings file, env vars, or both? The budget and the
  cadence numbers both need to be tunable per machine, the way `[brain]` is in
  `cherryd.toml`.
- Should the extension refuse to load when `AGENTS-brain.md` is present, or
  detect and complement it? Complementing is proposed above; confirm it doesn't
  produce two sets of instructions in the prompt.
- Is there a reason to keep the pi extension in step with `brain-mcp`'s tool
  descriptions automatically, rather than by hand?

## Status (2026-08-23)

Built: `pi/extensions/brain.ts`, the root `package.json` manifest, and
`brain checkpoint --from-pi` (`transcript.parse_pi_session` /
`transcript.checkpoint_pi`).

Answers to the open questions above, as resolved by building it:

- **`session_shutdown` fires on process exit** — verified in print mode, where the
  checkpoint lands with `reason: "quit"`. The TUI exit path (Ctrl+C/Ctrl+D) is the
  same event and pi documents it, but it has not been exercised by hand yet.
- **Settings are environment variables only.** pi has no per-extension settings
  block, and every other component here reads its configuration from the env.
  Table in `PI-SETUP.md`. One trap: `BRAIN_CMD` is a *shell string* in the Claude
  Code templates and `pi.exec` spawns without a shell, so it is honoured only when
  it has no whitespace; `BRAIN_PI_CMD` is the unambiguous knob.
- **Complement the `AGENTS.md` snippet, don't refuse to load.** The extension
  reads its guidance from `templates/AGENTS-brain.md` and skips injecting it when
  the same file is already loaded as a context file (matched on the
  `managed-by: ai-brain` marker), so the two never produce two copies.
- **Tool descriptions stay in step by construction**, not by hand: the extension
  is a frontend over the same CLI, and the parts that must not drift — the
  checkpoint format and the behavioural guidance — are read from Python and from
  the template rather than restated in TypeScript.

Also learned while verifying:

- The **first** `brain_recall` on a machine builds the embedding index for the whole
  vault and blew a 20s timeout, which surfaced to the model as an empty
  `brain error:`. The timeout now defaults to 60s and a killed command reports
  "timed out", not an empty string.
- **Forcing an auto-compaction from configuration did not work** and is a dead end
  worth not repeating. Setting `compaction.reserveTokens` just under the model's
  context window (project settings with `-a`, then user settings) never produced a
  compaction entry in print mode, even with the session context at ~11.7k against a
  ~5.1k threshold; a helper extension calling `ctx.compact()` from `agent_settled`
  hung rather than completing. The cheap verification is `/compact` in an
  interactive session — one command, real event, `reason: "manual"` — and that is
  what to do before calling this trigger verified.

The alternative reader described above turned out to be the *implementation*
rather than an alternative: `--from-pi` parses pi's own session JSONL, so the same
flag serves the extension (which supplies the trigger) and any future timer
(which would supply its own). Running it on a timer over `~/.pi/agent/sessions`
still needs nothing more than a cron entry.
