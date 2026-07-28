---
name: brain
description: Persistent cross-machine memory (the Brain). Use to save a preference/correction/decision, recall what is known about a project/person/tool, checkpoint a work session, or list/delete memories. Invoked by /brain or whenever a memory operation is needed and the exact CLI syntax is not already known.
allowed-tools: Bash(__BRAIN_CMD__:*)
---

# Brain memory — CLI reference

The Brain is an Obsidian vault of memory files, driven through the `brain` CLI.
Every command below is run with the Bash tool. The full invocation prefix on
this machine (env + absolute path, substituted by setup) is:

```
__BRAIN_CMD__
```

Referred to as `brain` below. Do **not** edit memory files in the vault
directly — always go through the CLI so frontmatter, slugs, and the vector
index stay consistent.

Proactive triggers (when to save/recall/checkpoint without being asked) live in
your global CLAUDE.md — this skill is the syntax reference and the handler for
explicit `/brain` commands.

## Recall

```
brain recall <query words> [--type user|feedback|project|reference] [--project <basename>]
```

Returns up to 3 short previews, server-capped. Escalate only when a hit
matters: add `--full-body` (bodies still capped) or `--top-k N` (capped at 10).
Session checkpoints are excluded unless you pass `--include-sessions` — use
that when looking for what happened in past work sessions.

## Save

Body comes from stdin — use a heredoc for anything multi-line:

```
brain save <type> "<short title 3-8 words>" [--project <basename>] <<'EOF'
<body>
EOF
```

- `type` is one of: `user` (facts about the user), `feedback` (behavior rules —
  lead with the rule, then `**Why:**` and `**How to apply:**` lines), `project`
  (ongoing-work context not derivable from the code; requires `--project` with
  the project *directory basename*), `reference` (pointer to an external system).
- Short one-liners can use `--content "..."` instead of stdin.
- Confirm afterwards in one short sentence: `Saved as <path>.`

## Checkpoint

```
brain checkpoint <project-basename> <<'EOF'
<summary: what was attempted, what worked, what failed, decisions made, open threads>
EOF
```

Confirm the path that was written. Keep summaries tight — 6-15 bullets.

## List / forget

```
brain list [--type <type>] [--project <basename>]
brain forget <path-from-recall-or-list>
```

For `/brain forget` with a partial name: `brain list` first, ask the user which
candidate to delete (AskUserQuestion), then forget the confirmed path.

## Health

```
brain stats
brain doctor [--project <basename>] --quiet
```

Run `doctor` when the Brain feels stale or broken (missing context, recall
returning nothing, save errors).

## /brain command mapping

- `/brain save <type> <name or phrase>` → infer a short name if given a bare
  phrase, body from the rest → `brain save`
- `/brain recall <topic>` → `brain recall`, surface hits grouped by type; if
  nothing matches say so in one sentence
- `/brain checkpoint` → compose the summary, `brain checkpoint <cwd basename>`
- `/brain list [type] [project]` → `brain list`, render as a short bulleted list
- `/brain forget <path or pattern>` → confirm-then-delete flow above

## Say = do

If you tell the user you're saving or checkpointing, the matching `brain save`
/ `brain checkpoint` command must run **in the same turn** (a Stop-hook gate
enforces this). Prefer: run the command first, then mention it in past tense.
