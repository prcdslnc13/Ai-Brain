---
purpose: map-of-content; loaded into every new session by SessionStart hook
---

# Brain Index

This file is auto-loaded at the start of every Claude session. Keep it short and meta —
it tells the agent *what kinds of things* live in the brain, not the things themselves.

## Categories

- **user** — who I am, my role, preferences, expertise, working style. Always relevant.
- **feedback** — rules I've given Claude to follow (or stop doing). Always relevant.
- **references** — pointers to external systems. Relevant when the topic comes up.
- **projects** — per-repo context, indexed by basename of the project directory.
- **sessions** — append-only checkpoints under each project. Most recent ones are loaded automatically.

## Conventions

- Memory files are markdown with YAML frontmatter (`name`, `description`, `type`).
- Files are named in lowercase-kebab-case (`prefer-rust-over-go.md`).
- Session checkpoints are timestamped (`2026-04-15-1430.md`).
- Never edit memory files by hand from inside Claude — use `brain_save` / `brain_forget`.

## How to read me

If you are an agent loading this file at session start, also load:
- All files in `user/`
- All files in `feedback/`
- `projects/<current-project>/overview.md` if it exists
- The most recent file in `projects/<current-project>/sessions/` if any
- Then call `brain_recall` whenever a relevant topic comes up mid-conversation.
