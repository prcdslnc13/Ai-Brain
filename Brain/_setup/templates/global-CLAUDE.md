# Global memory directives

You have a persistent, cross-machine, cross-account memory system called the **Brain**, backed by an
Obsidian vault at `__BRAIN_VAULT__` (synced via Obsidian Sync). The Brain is exposed as MCP tools
named `brain_*`. Use them. Do **not** write to memory by editing files directly, and do **not** use
the per-project `~/.claude-*/projects/*/memory/` directories — those are obsolete and ignored.

The SessionStart hook automatically preloads the Brain bundle (index, user profile, all feedback,
project overview, latest session checkpoint) into your context at the top of every session. You do
not need to call `brain_session_start` yourself unless that preload is missing.

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
any of the following happens:

- The user states a preference: *"I prefer X"*, *"I always do Y"*, *"I hate Z"*.
- The user corrects your approach: *"don't do that"*, *"stop"*, *"that's wrong because…"*.
- The user validates a non-obvious choice you made: *"yes exactly"*, *"perfect"*, *"that was the right call"*.
- The user gives a durable rule: *"from now on…"*, *"next time…"*, *"never…"*, *"always…"*, *"going forward…"*.
- The user mentions a deadline, stakeholder, incident, or constraint that won't be in the code.
- The user mentions an external system as the source of truth for some kind of information.

The user said: *"I am awful at remembering to do things like this and you are here to save me from
myself."* Take that seriously. The cost of a missed save is high; the cost of saving something
slightly redundant is low. Lean toward saving when you're on the fence — but never save things in
the "do NOT save" list above.

## When to recall (proactive triggers)

You must call `brain_recall` immediately when:

- The user mentions a project, repo, codebase, or file by name → `brain_recall(query=<name>, type="project")`.
- The user mentions a person, company, tool, or external service by name → `brain_recall(query=<name>)`.
- The user asks *"what do you know about X"*, *"do you remember Y"*, *"have we talked about Z"*.
- You're about to make a non-trivial decision or recommendation, and there's any chance prior
  feedback applies → `brain_recall(query=<topic>, type="feedback")`.

Recall is cheap. Use it before answering, not after.

## When to checkpoint

Call `brain_checkpoint(project, summary)` proactively when:

- The user signals end of session: *"thanks"*, *"that's all"*, *"we're done"*, *"good night"*.
- You finish a multi-step task that took several turns.
- The user is about to ask you to switch to a different project.

The PreCompact and SessionEnd hooks ALSO write checkpoints automatically as a safety net, but those
are structural extracts. Your `brain_checkpoint` call produces a real summary and is preferred when
you have the context fresh.

Format the summary as: what was attempted, what worked, what failed, decisions made, open threads.

## Pending save markers

If a previous turn's Stop hook detected a save signal phrase the model missed, it drops a marker
file in `__BRAIN_VAULT__/Brain/.pending-saves/`. The UserPromptSubmit hook will surface those
markers in your context. When you see them: read the marker, decide if a `brain_save` is warranted,
make the call, then delete the marker file.

## Confidence and verification

Memory records can become stale. When you recall a memory that names a specific function, file, or
flag, verify it still exists before recommending action on it (grep, read the file). If a recalled
memory conflicts with current reality, trust what you observe and call `brain_save` again to
correct the stale entry — or `brain_forget` it.

## Manual escape hatches

The user can also drive the brain manually via `/brain save`, `/brain recall`, `/brain checkpoint`,
`/brain forget`. These are for *their* convenience, not yours. The default expectation is that you
handle everything automatically and they never have to type these commands.
