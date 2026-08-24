# Using the Brain with llama.cpp and other hook-less harnesses

Claude Code gets its Brain wiring from hooks: SessionStart preloads the bundle, PreCompact and
SessionEnd write checkpoints, Stop gates unfulfilled save-promises. **No other harness has any of
that.** A llama.cpp session driven by cherryd, pi, or a hand-rolled client only writes to memory
when the model itself decides to — and a local model on an 8-32k context window is exactly the
one that will hit its ceiling and lose the session before it gets around to it.

This document covers the two halves of the fix:

1. **Getting memory in** — the bundle, sized for a small context window.
2. **Getting work out** — checkpoints that do not depend on the model remembering to write them.

See `PI-SETUP.md` for pi specifically, and `LMSTUDIO-SETUP.md` for the MCP route.

---

## 1. Getting memory in

### If the harness has native brain support (cherryd)

cherryd implements the Brain directly against the vault — bundle injection into the system prompt
plus five callable tools (`brain_save`, `brain_recall`, `brain_checkpoint`, `brain_list`,
`brain_forget`), in the same file format this repo writes. Nothing here needs installing for that
path; point it at the vault and it reads what Claude Code curates:

```toml
# cherryd.toml
[brain]
vault = "/home/you/Vaults/Ai-Brain"     # the Obsidian root, not the Brain/ subdir
```

Equivalent env vars: `CHERRYD_BRAIN_VAULT`, `CHERRYD_BRAIN_BUDGET_KB` (default 32),
`CHERRYD_BRAIN_SESSIONS_RECENT` (default 1), `CHERRYD_BRAIN_PROJECT`.

### If the harness has a shell tool but no brain support

Use the `brain` CLI, exactly as pi does — see `PI-SETUP.md` and `templates/AGENTS-brain.md`.

### If the model cannot call tools at all

Pipe the bundle in as a system prompt:

```bash
brain-prep --project MyProject --slim --budget-kb 12 | llama-cli -m model.gguf --system-prompt-file /dev/stdin
```

**Size the bundle to the context window.** `brain-prep` defaults to `BRAIN_BUNDLE_BUDGET_KB`
(72 KB), which is sized for a 200k-token Claude context and is roughly 18k tokens — more than
half of a 32k window before the conversation starts. Two flags cut it down:

| Flag | Effect |
|---|---|
| `--budget-kb N` | Hard cap in kilobytes. Sections fill in priority order (project feedback → user → global feedback), so what is dropped is the least specific material. |
| `--slim` | Index + user + feedback only; drops the project overview and session checkpoints. The same shape the SubagentStart hook injects. |

Rough sizing: `--slim --budget-kb 6` ≈ 1.5k tokens, `--slim --budget-kb 16` ≈ 4k tokens. The
budget line at the top of the output reports what was skipped, so you can see when the cap is
biting:

```
> budget: 5.21/6.0 KB · skipped 4 feedback
```

---

## 2. Getting work out — checkpoints without hooks

This is the half that actually stops work getting lost, and the important property is that **it
does not depend on the model.** A harness that persists its own session log can be checkpointed
from the outside, after the fact, by reading that log.

### cherryd

cherryd writes every session to a SQLite event log at `$XDG_STATE_HOME/cherryd/cherryd.db`
(default `~/.local/state/cherryd/cherryd.db`). Point `brain checkpoint` at it:

```bash
# Checkpoint the most recently active session
brain checkpoint --from-cherryd ~/.local/state/cherryd/cherryd.db

# Every session that has moved since the last run — what a timer should call
brain checkpoint --from-cherryd ~/.local/state/cherryd/cherryd.db --all-sessions

# What is in the log?
brain checkpoint --from-cherryd ~/.local/state/cherryd/cherryd.db --list-sessions
```

The checkpoint is the same structural extract the Claude Code hooks produce — user turns, a
tool-call histogram, the final assistant message — filed under
`Brain/projects/<project>/sessions/`. The project name comes from the session's own cwd; pass a
positional project name to override it.

Properties that matter for unattended use:

- **The database is opened read-only.** A checkpoint run cannot damage a live session's history.
- **Repeat runs are no-ops.** The last event id per session is recorded in
  `<vault>/Brain/.state/harness-checkpoints.json`; a session with nothing new is skipped with a
  reason rather than writing a duplicate. `--force` overrides.
- **Empty sessions are skipped**, and a bare `--from-cherryd` picks the newest session that has
  an actual exchange in it — opening a fresh session in the TUI does not shadow the long-running
  one holding the work.

### pi

pi persists each session as a JSONL file under `~/.pi/agent/sessions/`, so the same reader
works there:

```bash
# Newest session under a directory (a session file also works)
brain checkpoint --from-pi ~/.pi/agent/sessions
```

The same dedup state file applies, so this is safe to put on the timer alongside the cherryd
line. **For an attended pi session, install the extension instead** — it checkpoints on the
real compaction event and at shutdown rather than up to one timer interval late. See
`PI-SETUP.md`. The two do not conflict: both write through the same dedup key.

### Run it on a timer

The whole point is that this happens without anyone remembering. A user-level systemd timer,
every 10 minutes:

```ini
# ~/.config/systemd/user/brain-checkpoint.service
[Unit]
Description=Checkpoint cherryd sessions into the Brain vault

[Service]
Type=oneshot
Environment=BRAIN_VAULT=%h/Vaults/Ai-Brain
ExecStart=%h/src/Ai-Brain/mcp-server/.venv/bin/brain checkpoint \
    --from-cherryd %h/.local/state/cherryd/cherryd.db --all-sessions
```

```ini
# ~/.config/systemd/user/brain-checkpoint.timer
[Unit]
Description=Checkpoint cherryd sessions every 10 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now brain-checkpoint.timer
systemctl --user list-timers brain-checkpoint.timer
```

macOS launchd equivalent (`~/Library/LaunchAgents/dev.brain.checkpoint.plist`) uses
`StartInterval` of `600` with the same `ExecStart` split into `ProgramArguments`; a plain
`*/10 * * * *` crontab entry works too, as long as `BRAIN_VAULT` is set in the entry itself —
cron does not inherit your shell profile.

Ten minutes is a starting point, not a rule. The cost of a run with no new events is one SQLite
read; the cost of a missed one is however much context died with the session.

### Checkpoint volume

Every run with new activity writes a new timestamped file, so a long session produces a series of
checkpoints, each superseding the last (the preload only ever loads the newest). `brain-compact`
rolls old ones into daily/weekly/archive buckets:

```bash
brain-compact --dry-run    # see what it would fold up
brain-compact
```

Worth its own timer, weekly, if you leave the checkpoint timer running.

---

## 3. What this does not fix

The timer captures **what happened** — turns, tools, the last message. It does not capture **what
was decided and why**, which is what a good checkpoint carries and what only the model can write.
The structural extract is a floor, not a replacement: it guarantees a session is never a total
loss, while a model that calls `brain_checkpoint` itself still produces the better record.

Two things still need doing on the harness side, and neither can be done from this repo:

- **Tell the model the brain tools exist and when to use them.** cherryd advertises the five brain
  tools in its tool schema but its system prompt describes only the filesystem tools — there is no
  equivalent of `templates/global-CLAUDE.md`'s proactive-save triggers, so the model has the
  capability and no instruction to use it. The trigger list in that template is the content to
  port; keep it short for a small model.
- **Checkpoint on context pressure.** cherryd knows the real input-token count after every turn
  (`summary.usage.input_tokens`) and claw-code already exports `should_compact` /
  `estimate_session_tokens`, but nothing in the daemon calls them. A threshold check after each
  turn that fires a checkpoint before the window overflows is the harness-side version of Claude
  Code's PreCompact hook, and it is the only mechanism that catches the failure *as it happens*
  rather than up to one timer interval late.
