# Brain

Persistent memory shared across Claude Code sessions, local LLMs, and machines.

## Layout

- `user/` — who you are (preferences, role, expertise). Shared across all projects.
- `feedback/` — corrections and validated approaches. Things you've told an agent to do or not do.
- `references/` — pointers to external systems (Linear, Grafana, Slack channels, dashboards).
- `projects/<repo-basename>/` — per-project context.
  - `overview.md` — what the project is, why it exists, current focus.
  - `sessions/YYYY-MM-DD-HHMM.md` — append-only checkpoints written by hooks at compaction and session end.
- `activity.md` — rolling one-line breadcrumb log (every assistant turn).
- `_index.md` — map of content; loaded into every new session.
- `_setup/` — bootstrap scripts, templates, and the Brain MCP server.
- `.pending-saves/` — transient queue for save signals the Stop hook caught (excluded from sync).

## How memories are written

You almost never write to these files by hand. The Brain MCP server's `brain_save` tool
(and friends) handle frontmatter and naming. Hooks handle session checkpoints automatically.

The `/brain *` slash commands in Claude Code are manual escape hatches; the model and hooks
should cover normal use without you typing anything.

## Adding a new machine

1. Install Obsidian and enable Obsidian Sync for this vault.
2. Run `Brain/_setup/setup-mac.sh` (or `setup-windows.ps1`).
3. Register the Brain MCP server in LMStudio's settings if you use LMStudio.
4. Done.
