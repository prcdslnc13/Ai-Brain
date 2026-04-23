<!-- managed-by: ai-brain -->
# Global memory directives

You have a persistent, cross-machine, cross-account memory system called the **Brain**, backed by an
Obsidian vault at `__BRAIN_VAULT__` (synced via Obsidian Sync). The Brain is exposed as MCP tools
named `brain_*`. Use them. Do **not** write to memory by editing files directly, and do **not** use
the per-project `~/.claude-*/projects/*/memory/` directories — those are obsolete and ignored.

The SessionStart hook automatically preloads the Brain bundle (index, user profile, all feedback,
project overview, latest session checkpoint) into your context at the top of every session. You do
not need to call `brain_session_start` yourself unless that preload is missing.

## Session bootstrap: upgrading an overview stub

The first time a project is used, the SessionStart hook writes a *stub* `overview.md` so the bundle
has something to show. You can recognize a stub by `stub: true` in its YAML frontmatter and the
header `# <project> — overview (STUB)`. When the preloaded overview for the current project is a
stub, treat that as a first-turn task:

1. Read the "Source material" paths listed in the stub (typically the project's `CLAUDE.md`,
   `plan.md`, `ROADMAP.md`, `README.md` — whichever exist).
2. Synthesize a concise overview covering **purpose**, **architecture** (moving parts and how they
   fit together), and **non-obvious gotchas** (things a future session won't figure out by reading
   the code).
3. Call `brain_save(type="project", project="<project>", name="overview", content=...)` with your
   summary. This overwrites the stub — future sessions will see your real overview instead of the
   placeholder.

Do this early in the turn, before the user's actual request if possible, so the rest of the session
has full project context. One redundant `brain_save` if the stub was already upgraded costs nothing;
leaving a stub in place costs every future session the context it needs.

## Say = do: stated intent must be fulfilled in the same turn

When you tell the user you will perform an action — save to brain, checkpoint, run a command, write
a file — that action **must happen in the same turn**. Not the next turn, not "after we finish the
next step", not "once you confirm". A stated commitment is not a plan; it's a promise the user
treats as already-done.

A Stop-hook gate (`BRAIN_STOP_GATE`, default on) watches your final message for save-promise
phrasings: *"I'll save this"*, *"let me checkpoint"*, *"saving to brain"*, *"recording this"*,
*"checkpointing now"*, *"I'll save as feedback"*, etc. If the gate sees one and no matching
`brain_save` / `brain_checkpoint` tool call happened in the turn, it blocks turn-end and feeds the
block reason back to you — you then have to either call the tool or explicitly recant before the
turn can close.

Three ways to stay on the right side of the gate:

1. **Fulfill it** — call `brain_save` or `brain_checkpoint` in the same turn, *before* your final
   text. Then describe what you just did in past tense. This is the preferred path.
2. **Don't promise until you're ready** — skip "I'll save this" entirely, just save, then mention
   it: *"Saved as feedback."* No promise, no gate risk.
3. **Explicitly defer** — if you really do mean to save later (e.g. after the user confirms a
   choice), phrase it with a conditional the gate won't trip on, and actually follow through when
   the condition is met. Gate false-positives here are a minor annoyance; silent drops are not.

The triggering incident (2026-04-22, MM-ToolDecoder): the model said *"recording verification steps
to brain so they survive a restart"* and never called `brain_save`. The window died before the
safety-net checkpoint fired and ~70 minutes of migration work plus the user's verification plan
were lost. The user: *"It's unacceptable that the model says it's doing something and it just
doesn't."* The gate exists so this cannot happen again silently.

## Session-start health banner: act on it

The SessionStart bundle you're reading *right now* may include a `## Brain Health` banner at the
top listing warn/error findings from `brain_doctor`. Read it before you answer the user. In
particular:

- **`STALE_UNCOMMITTED`** — the project has on-disk changes (commits or uncommitted edits) that
  postdate the last Brain checkpoint. A prior session likely died before checkpointing. Before
  starting new work, reconstruct what changed from `git log` / `git diff` and call
  `brain_checkpoint` to close the gap. Then proceed with the user's actual request.
- **`PROMISE_GAP`** — recent turns promised saves and didn't fulfill them. The gate may be off,
  the regex may have missed a phrasing, or there's a re-entry path bypassing enforcement. Mention
  this to the user if they're about to rely on saved state.
- **`SAVE_GAP`** — signals in user messages aren't turning into saves often enough. Tighten your
  own proactive-save behavior this session.

## Memory taxonomy

There are exactly four types of memories. Save things if and only if they fit one of these:

### user
Facts about the user that should shape future behavior: role, expertise, preferences, working style,
tools they use. Avoid judgmental notes. Aim to be useful, not to label.

### feedback
Behavior corrections AND validated approaches. Save BOTH:
- Corrections: the user told you to stop doing X, or to start doing Y.
- Validated approaches: the user accepted a non-obvious choice you made without pushback (a quiet "yes, that was right").

Body structure: lead with the rule. Then a `**Why:**` line (the reason — often an incident or strong
preference). Then a `**How to apply:**` line (when this rule kicks in). Knowing *why* lets you judge
edge cases instead of blindly following the rule.

### project
Things about an ongoing initiative, deadline, incident, or stakeholder ask that you cannot derive
from reading the code or git log. Convert relative dates to absolute dates ("Thursday" → "2026-03-05")
so the memory remains interpretable later. Body structure: lead with the fact, then `**Why:**` and
`**How to apply:**` lines.

### reference
Pointers to where information lives in external systems: "bugs are tracked in Linear project FOO",
"the oncall dashboard is at grafana.internal/d/api-latency".

## Do NOT save

These exclusions apply even if the user explicitly asks you to save:

- Code patterns, conventions, file paths, project structure — derivable by reading the code.
- Git history, recent changes, who-changed-what — `git log` / `git blame` are authoritative.
- Debugging recipes or fix descriptions — the fix is in the code; the commit message has the context.
- Anything already in CLAUDE.md files.
- Ephemeral state: in-progress task details, current conversation context, today's todo list.

If the user asks you to save something in this list, ask them what was *surprising* or *non-obvious*
about it — that's the part worth keeping.

## When to save (proactive triggers)

You must call `brain_save` immediately, **without waiting for the user to say "remember this"**, when
any of the following happens. The user expects the Brain to be fully automatic and transparent — they
should never have to ask you to save. If they have to tell you to remember something, that's a
failure of these triggers.

### User-initiated signals (things the user says)

- The user states a preference: *"I prefer X"*, *"I always do Y"*, *"I hate Z"*, *"my default is X"*.
- The user corrects your approach: *"don't do that"*, *"stop"*, *"that's wrong because…"*, *"no, use X instead"*.
- The user validates a non-obvious choice you made: *"yes exactly"*, *"perfect"*, *"that was the right call"*,
  or simply accepts an unusual approach without pushback.
- The user gives a durable rule: *"from now on…"*, *"next time…"*, *"never…"*, *"always…"*, *"going forward…"*,
  *"the right cadence is…"*, *"I want…"*, *"I'm looking for…"*.
- The user mentions a deadline, stakeholder, incident, or constraint that won't be in the code.
- The user mentions an external system as the source of truth for some kind of information.
- The user explains *why* something is done a certain way and the reason isn't obvious from the code.

### Model-initiated signals (things you decide or discover)

- **You make a non-obvious design or architecture decision during implementation.** Save it as
  `project` context with the reasoning — a future session seeing only the code won't know *why*
  you chose approach A over approach B.
- **You discover a constraint, gotcha, or non-obvious interaction** while working that would bite
  a future session. Save it as `project` or `feedback` depending on scope.
- **You rule out an approach** after investigating it. The dead end is valuable context — save why
  it was rejected so a future session doesn't retry it.
- **The user and you agree on a plan or direction** (explicitly or implicitly). Save the decision
  and its rationale as `project` context.

The user said: *"I am awful at remembering to do things like this and you are here to save me from
myself."* Take that seriously. The cost of a missed save is high; the cost of saving something
slightly redundant is low. Lean toward saving when you're on the fence — but never save things in
the "do NOT save" list above.

## When to recall (proactive triggers)

Recall is cheap. Call it **before** acting, not after. The user expects the Brain to surface relevant
context automatically — they should never have to say "check the brain for…". If prior context
existed and you didn't use it, that's a failure.

You must call `brain_recall` immediately when:

### Explicit mentions

- The user mentions a project, repo, codebase, or file by name → `brain_recall(query=<name>, type="project")`.
- The user mentions a person, company, tool, or external service by name → `brain_recall(query=<name>)`.
- The user asks *"what do you know about X"*, *"do you remember Y"*, *"have we talked about Z"*.
- The user references prior work: *"last time we…"*, *"we already…"*, *"remember when…"*, *"that thing we did"*.

### Before acting

- **Before suggesting a tool, library, pattern, or approach** — recall `feedback` for that topic.
  Prior corrections exist to prevent you from repeating mistakes; ignoring them wastes the user's time.
- **Before starting work on a project you haven't recalled yet this session.** The SessionStart
  preload covers the *current* project. If the user switches projects mid-session or references a
  different repo, recall its project context before proceeding.
- **Before making a design or architecture recommendation** — recall `project` context. There may be
  constraints, prior decisions, or rejected approaches that should inform your suggestion.
- **When you're unsure whether something was already decided or discussed** — recall rather than
  guess. A redundant recall that returns nothing costs a fraction of a second. A recommendation that
  contradicts a prior decision costs trust.

## When to checkpoint

Checkpoint **frequently**. The user has explicitly said they lose sessions by accidentally closing
windows before compaction fires. The automated PreCompact and SessionEnd hooks are a safety net that
produces only a structural extract — your `brain_checkpoint` call is the primary mechanism and
produces a real summary while you still have context fresh. Treat checkpoints like incremental saves,
not a final save at the end.

Call `brain_checkpoint(project, summary)` proactively when **any** of the following happens:

- **After every git commit.** The commit just landed — summarize what changed and why.
- **After any change to a plan, roadmap, or design document.** Decisions and direction changes are
  the most valuable things to checkpoint because they're invisible in `git log`.
- **After creating or substantially modifying files.** New modules, new setup scripts, architecture
  changes, large refactors — anything a future session would need to understand.
- **After completing a distinct unit of work**, even if the session continues. Don't batch — if you
  just finished task A and are about to start task B, checkpoint task A now.
- **When the user signals end of session:** *"thanks"*, *"that's all"*, *"we're done"*, *"good night"*.
- **When the user is about to switch to a different project.**

When in doubt, checkpoint. A redundant checkpoint costs almost nothing; a lost session costs the
user's time reconstructing context from scratch.

Format the summary as: what was attempted, what worked, what failed, decisions made, open threads.

## Confidence and verification

Memory records can become stale. When you recall a memory that names a specific function, file, or
flag, verify it still exists before recommending action on it (grep, read the file). If a recalled
memory conflicts with current reality, trust what you observe and call `brain_save` again to
correct the stale entry — or `brain_forget` it.

## Manual escape hatches

The user can also drive the brain manually via `/brain save`, `/brain recall`, `/brain checkpoint`,
`/brain forget`. These are for *their* convenience, not yours. The default expectation is that you
handle everything automatically and they never have to type these commands.
