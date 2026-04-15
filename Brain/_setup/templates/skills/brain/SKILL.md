---
name: brain
description: Manual override commands for the Brain memory system. Use only when the user explicitly types /brain. The model handles save/recall/checkpoint automatically per global CLAUDE.md instructions in normal use.
---

# /brain — manual memory commands

These commands let the user drive the Brain memory system explicitly. In normal use the model and
hooks handle everything automatically (see your global CLAUDE.md). These commands are escape hatches
for when the user wants direct control.

All commands route through the `brain_*` MCP tools — never edit memory files directly.

## /brain save <type> <name or content>

Save a memory. Type must be one of `user`, `feedback`, `project`, `reference`. If the user gives you
just a phrase rather than a structured name+content, infer a sensible short name and use the rest as
the content body.

Call: `brain_save(type=<type>, name=<short title>, content=<body>, project=<basename if type=project>)`

After saving, confirm in one short sentence: `Saved as <type>/<filename>.`

## /brain recall <topic>

Search the brain for memories matching a topic. Call `brain_recall(query=<topic>)`. Surface the
results inline, grouped by type, with the relevant snippet from each.

If nothing matches, say so in one sentence — do not pad with apologies.

## /brain checkpoint

Write a session checkpoint immediately, without waiting for compaction or session end.

1. Identify the current project from the working directory (basename).
2. Compose a summary covering: what was attempted this session, what worked, what failed, decisions
   made, open threads. Keep it tight — 6-15 bullets.
3. Call `brain_checkpoint(project=<basename>, summary=<your summary>)`.
4. Confirm the path of the file that was written.

## /brain forget <path or pattern>

Delete a memory. If the user gives a partial name, first call `brain_list` to find candidates and
ask which one to delete (use AskUserQuestion). Once confirmed, call `brain_forget(path=<absolute or relative>)`.

## /brain list [type] [project]

List memories. Call `brain_list(type=<type if given>, project=<project if given>)` and render as a
short bulleted list grouped by type.
