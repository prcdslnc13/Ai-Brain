/**
 * Brain — the Ai-Brain memory system as a pi (pi.dev) extension.
 *
 * PI-SETUP.md wires the Brain into pi the cheap way: an AGENTS.md snippet plus
 * the `brain` CLI on the shell tool. That works, but nothing is written unless
 * the model volunteers it — no preload, no checkpoint before compaction, no
 * session-end write. Those three are exactly what Claude Code gets from hooks.
 * This extension closes the gap using pi's lifecycle events.
 *
 * It is a *frontend*, not a second implementation. Every operation shells out
 * to the same `brain` CLI the Claude Code hooks and the MCP server use, and the
 * checkpoint body is rendered by `brain checkpoint --from-pi` in Python rather
 * than assembled here. History earned that rule: cherryd rendered its own
 * checkpoints in a different repo and needed a commit to regain byte parity
 * (down to a trailing newline). One renderer, one format, one place to change.
 *
 * Configuration is environment-only, like the rest of this repo (pi has no
 * per-extension settings block):
 *
 *   BRAIN_VAULT                 vault path (falls back to ~/Vaults/Ai-Brain)
 *   BRAIN_PI_CMD / BRAIN_CMD    path to the `brain` executable
 *   BRAIN_BUNDLE_BUDGET_KB      preload budget, default 12 (a 32k-window model)
 *   BRAIN_PI_PRELOAD=0          skip the session preload
 *   BRAIN_PI_SLIM=0             preload the full bundle, not the slim one
 *   BRAIN_PI_CHECKPOINT=0       skip automatic checkpoints
 *   BRAIN_PI_CHECKPOINT_EVERY   settled turns between cadence checkpoints (3)
 *   BRAIN_PI_TIMEOUT_MS         per-invocation timeout, default 60000
 *   BRAIN_PI_EXTENSION=0        disable the extension entirely
 */

import { existsSync, readFileSync } from "node:fs";
import { homedir, platform } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type {
	BeforeAgentStartEventResult,
	ExtensionAPI,
	ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const EXTENSION_DIR = dirname(fileURLToPath(import.meta.url));
/** pi/extensions/brain.ts -> the repo root, which also holds mcp-server/ and templates/. */
const PACKAGE_ROOT = resolve(EXTENSION_DIR, "..", "..");
const IS_WINDOWS = platform() === "win32";
/** The first line of templates/AGENTS-brain.md; how we spot an already-wired AGENTS.md. */
const AGENTS_SNIPPET_MARKER = "managed-by: ai-brain";

const MEMORY_TYPES = ["user", "feedback", "project", "reference"] as const;

function envOff(name: string): boolean {
	return process.env[name] === "0";
}

function envNumber(name: string, fallback: number): number {
	const raw = process.env[name];
	if (!raw) return fallback;
	const n = Number(raw);
	return Number.isFinite(n) && n > 0 ? n : fallback;
}

/**
 * The vault is env-first, exactly like `vault.py`. The ~/Vaults/Ai-Brain
 * fallback is the location setup recommends (and the one that keeps macOS TCC
 * out of the picture) — but only when it actually holds a Brain/ directory, so
 * a machine that has never synced the vault gets a clean self-disable instead
 * of writes into an empty folder.
 */
function resolveVault(): string | null {
	const explicit = process.env.BRAIN_VAULT?.trim();
	if (explicit) return explicit;
	const fallback = join(homedir(), "Vaults", "Ai-Brain");
	return existsSync(join(fallback, "Brain")) ? fallback : null;
}

/**
 * BRAIN_CMD is a *shell string* in the Claude Code templates ("BRAIN_VAULT=… /path/brain"),
 * and pi.exec spawns without a shell. So we accept it only when it is a bare
 * path, and offer BRAIN_PI_CMD for the unambiguous case.
 */
function resolveBrainCmd(): string {
	const piCmd = process.env.BRAIN_PI_CMD?.trim();
	if (piCmd) return piCmd;
	const envCmd = process.env.BRAIN_CMD?.trim();
	if (envCmd && !/\s/.test(envCmd)) return envCmd;
	const venv = join(
		PACKAGE_ROOT,
		"mcp-server",
		".venv",
		IS_WINDOWS ? "Scripts" : "bin",
		IS_WINDOWS ? "brain.exe" : "brain",
	);
	if (existsSync(venv)) return venv;
	return IS_WINDOWS ? "brain.exe" : "brain"; // last resort: whatever is on PATH
}

/** brain-prep lives next to brain in the same venv, or on PATH beside it. */
function resolvePrepCmd(brainCmd: string): string {
	const sibling = join(dirname(brainCmd), IS_WINDOWS ? "brain-prep.exe" : "brain-prep");
	if (existsSync(sibling)) return sibling;
	return IS_WINDOWS ? "brain-prep.exe" : "brain-prep";
}

/**
 * The behavioural guidance is single-sourced from templates/AGENTS-brain.md so
 * the CLI route and this extension never drift into saying different things.
 * Two transforms are needed: the fenced CLI-syntax block is redundant once the
 * tools carry schemas, and the "Session start" paragraph tells the model there
 * is no preload — which stops being true the moment this extension loads.
 */
function loadGuidance(): string | null {
	const path = join(PACKAGE_ROOT, "templates", "AGENTS-brain.md");
	let raw: string;
	try {
		raw = readFileSync(path, "utf8");
	} catch {
		return null;
	}
	const body = raw
		.replace(/^<!--[\s\S]*?-->\n/, "")
		.replace(/```[\s\S]*?```\n/g, "")
		.replace(/Drive it through the `brain` CLI using your shell tool:\n+/, "")
		.split("\n\n")
		.filter((para) => !para.trimStart().startsWith("**Session start:**"))
		.join("\n\n")
		.replace(/Do not edit vault files directly — always go through the CLI\./,
			"Do not edit vault files directly — always go through the brain_* tools.")
		.replace(/needs `--project`/, "needs the project argument")
		.replace(/re-save or `forget` entries/, "re-save or brain_forget entries")
		.trim();
	return body.length > 0 ? body : null;
}

export default function brainExtension(pi: ExtensionAPI) {
	if (envOff("BRAIN_PI_EXTENSION")) return;

	const vault = resolveVault();
	const brainCmd = resolveBrainCmd();
	const prepCmd = resolvePrepCmd(brainCmd);
	// 60s, not 20: the first recall on a machine builds the embedding index for
	// the whole vault, and a killed build is worse than a slow one.
	const timeoutMs = envNumber("BRAIN_PI_TIMEOUT_MS", 60_000);
	const budgetKb = envNumber("BRAIN_BUNDLE_BUDGET_KB", 12);
	const checkpointEvery = envNumber("BRAIN_PI_CHECKPOINT_EVERY", 3);
	const guidance = loadGuidance();

	// A broken Brain must never break the session: say it once, then go quiet.
	const warned = new Set<string>();
	function warnOnce(ctx: ExtensionContext, key: string, message: string) {
		if (warned.has(key)) return;
		warned.add(key);
		if (ctx.hasUI) ctx.ui.notify(`Brain: ${message}`, "warning");
	}

	if (!vault) {
		let toldUser = false;
		pi.on("session_start", (_event, ctx) => {
			if (toldUser) return;
			toldUser = true;
			if (ctx.hasUI) {
				ctx.ui.notify(
					"Brain: no vault found (set BRAIN_VAULT or sync ~/Vaults/Ai-Brain) — memory is off",
					"warning",
				);
			}
		});
		return;
	}

	// pi.exec spawns with the parent environment and no env override, so this is
	// how BRAIN_VAULT reaches the CLI. Hooks have the same problem and solve it
	// by wrapping the command; here we own the process, so we set it directly.
	process.env.BRAIN_VAULT = vault;

	// This extension is the operator, not the model: its checkpoints go through
	// `brain checkpoint --from-pi <session.jsonl>`, which the agent-surface gate
	// refuses. resolveBrainCmd() normally lands on the venv binary (which never sets
	// the flag), but BRAIN_PI_CMD can legitimately point at something that does —
	// Claude Code's generated brain-agent.py launcher, or a wrapper of the user's own
	// that sets it — and then every automatic checkpoint would fail
	// with an exit 2 that nothing surfaces. Clearing it here is safe because the
	// arguments are ours, not the model's: the tools below expose recall/save/list/
	// forget/checkpoint bodies, never a caller-supplied path to read.
	process.env.BRAIN_AGENT_SURFACE = "0";

	interface RunResult {
		ok: boolean;
		stdout: string;
		stderr: string;
		/** Set when the command was killed by the timeout rather than failing. */
		timedOut?: boolean;
	}

	async function run(
		cmd: string,
		args: string[],
		ctx: ExtensionContext,
		signal?: AbortSignal,
	): Promise<RunResult> {
		try {
			const res = await pi.exec(cmd, args, { signal, timeout: timeoutMs, cwd: ctx.cwd });
			if (res.code !== 0 || res.killed) {
				// A kill with no output is almost always the timeout, and the
				// usual cause is the first recall on a machine building the
				// embedding index. Say that, rather than reporting an empty error.
				const detail = res.killed
					? `timed out after ${Math.round(timeoutMs / 1000)}s`
					: (res.stderr || res.stdout || "").trim().split("\n")[0] || `exit ${res.code}`;
				warnOnce(ctx, `fail:${cmd}:${args[0]}`, `${basename(cmd)} ${args[0]} failed — ${detail}`);
				return { ok: false, stdout: res.stdout, stderr: detail, timedOut: res.killed };
			}
			return { ok: true, stdout: res.stdout, stderr: res.stderr };
		} catch (err) {
			warnOnce(ctx, `throw:${cmd}`, `could not run ${cmd}: ${(err as Error).message}`);
			return { ok: false, stdout: "", stderr: String(err) };
		}
	}

	/** The project basename is the vault's key for per-project memory. */
	function projectOf(ctx: ExtensionContext): string {
		return basename(ctx.cwd) || "unknown";
	}

	// ---------------------------------------------------------------- tools

	async function brainText(
		args: string[],
		ctx: ExtensionContext,
		signal: AbortSignal | undefined,
		emptyText: string,
	) {
		const res = await run(brainCmd, args, ctx, signal);
		let text: string;
		if (res.ok) {
			text = res.stdout.trim() || emptyText;
		} else if (res.timedOut) {
			text = `brain ${args[0]} timed out — the embedding index may still be building; try again shortly`;
		} else {
			text = `brain ${args[0]} failed: ${(res.stderr || res.stdout).trim() || "no output"}`;
		}
		return { content: [{ type: "text" as const, text }], details: { ok: res.ok } };
	}

	pi.registerTool({
		name: "brain_recall",
		label: "Brain recall",
		description:
			"Search long-term memory (the Brain) for what is already known about a project, " +
			"person, tool, decision, or past correction. Results are capped and previewed.",
		promptSnippet: "Search long-term memory for prior context on a project, person, tool, or decision",
		promptGuidelines: [
			"Use brain_recall before acting when the user names a project, person, tool, or service you may have history with, before recommending an approach in an area where the user may have corrected you before, and whenever you are unsure whether something was already decided.",
		],
		parameters: Type.Object({
			query: Type.String({ description: "What to search for" }),
			type: Type.Optional(
				Type.Union(MEMORY_TYPES.map((t) => Type.Literal(t)), {
					description: "Restrict to one memory type",
				}),
			),
			project: Type.Optional(Type.String({ description: "Restrict to a project basename" })),
			top_k: Type.Optional(Type.Number({ description: "How many hits (default 3, capped)" })),
			full_body: Type.Optional(Type.Boolean({ description: "Full (still capped) bodies instead of previews" })),
			include_sessions: Type.Optional(Type.Boolean({ description: "Include session checkpoints" })),
		}),
		async execute(_id, params, signal, _onUpdate, ctx) {
			const args = ["recall", params.query];
			if (params.type) args.push("--type", params.type);
			if (params.project) args.push("--project", params.project);
			if (params.top_k) args.push("--top-k", String(params.top_k));
			if (params.full_body) args.push("--full-body");
			if (params.include_sessions) args.push("--include-sessions");
			return brainText(args, ctx, signal, "no memories matched");
		},
	});

	pi.registerTool({
		name: "brain_save",
		label: "Brain save",
		description:
			"Write a memory to the Brain. Types: user (facts about the user), feedback (behaviour " +
			"rules — lead with the rule, then **Why:** and **How to apply:** lines), project " +
			"(context not derivable from the code; needs project), reference (pointers to external " +
			"systems). Do not save code structure, git history, or ephemeral state.",
		promptSnippet: "Save a durable memory: a user fact, a behaviour rule, project context, or a reference",
		promptGuidelines: [
			"Call brain_save immediately, without being asked, when the user states a preference, corrects you, gives a durable rule (\"from now on…\", \"always…\", \"never…\"), or names a deadline, constraint, or stakeholder that is not in the code — and when you make a non-obvious design decision or rule out an approach.",
			"Scope brain_save with project when a feedback rule only makes sense in this repo; leave it off only for rules that apply everywhere.",
		],
		parameters: Type.Object({
			type: Type.Union(MEMORY_TYPES.map((t) => Type.Literal(t)), { description: "Memory type" }),
			title: Type.String({ description: "Short title, 3-8 words" }),
			body: Type.String({ description: "The memory body" }),
			project: Type.Optional(Type.String({ description: "Project basename (required for type=project)" })),
		}),
		async execute(_id, params, signal, _onUpdate, ctx) {
			const args = ["save", params.type, params.title, "--content", params.body];
			if (params.project) args.push("--project", params.project);
			else if (params.type === "project") args.push("--project", projectOf(ctx));
			return brainText(args, ctx, signal, "saved");
		},
	});

	pi.registerTool({
		name: "brain_checkpoint",
		label: "Brain checkpoint",
		description:
			"Write a session checkpoint: what was attempted, what worked, what failed, decisions " +
			"made, open threads. Cheap and incremental — not a final report.",
		promptSnippet: "Checkpoint the session: attempts, outcomes, decisions, open threads",
		promptGuidelines: [
			"Call brain_checkpoint after each commit, after any change to a plan or design document, after completing a unit of work, and when the user signals the session is ending.",
		],
		parameters: Type.Object({
			summary: Type.String({ description: "What was attempted, what worked, what failed, decisions, open threads" }),
			project: Type.Optional(Type.String({ description: "Project basename (defaults to the cwd basename)" })),
		}),
		async execute(_id, params, signal, _onUpdate, ctx) {
			const project = params.project || projectOf(ctx);
			return brainText(["checkpoint", project, "--summary", params.summary], ctx, signal, "checkpoint written");
		},
	});

	pi.registerTool({
		name: "brain_list",
		label: "Brain list",
		description: "Enumerate memories (paths plus one-line descriptions), optionally filtered.",
		promptSnippet: "List stored memories with their paths, to inspect or to pick one to forget",
		promptGuidelines: [
			"Use brain_list to find the exact path of a memory before calling brain_forget.",
		],
		parameters: Type.Object({
			type: Type.Optional(Type.Union(MEMORY_TYPES.map((t) => Type.Literal(t)))),
			project: Type.Optional(Type.String()),
			include_sessions: Type.Optional(Type.Boolean()),
		}),
		async execute(_id, params, signal, _onUpdate, ctx) {
			const args = ["list"];
			if (params.type) args.push("--type", params.type);
			if (params.project) args.push("--project", params.project);
			if (params.include_sessions) args.push("--include-sessions");
			return brainText(args, ctx, signal, "no memories");
		},
	});

	pi.registerTool({
		name: "brain_forget",
		label: "Brain forget",
		description: "Delete a memory by the path a recall or list result reported.",
		promptSnippet: "Delete a memory that has gone stale or wrong, by its path",
		promptGuidelines: [
			"Use brain_forget when a memory conflicts with what you can observe in the code — trust reality, then delete or re-save the stale entry.",
		],
		parameters: Type.Object({
			path: Type.String({ description: "Path from a prior brain_recall or brain_list result" }),
		}),
		async execute(_id, params, signal, _onUpdate, ctx) {
			return brainText(["forget", params.path], ctx, signal, "forgotten");
		},
	});

	// -------------------------------------------------------------- preload

	let preloaded = false;
	let settledTurns = 0;
	pi.on("session_start", () => {
		// session_start fires again on new/resume/fork with a fresh context to fill.
		preloaded = false;
		settledTurns = 0;
	});

	pi.on("before_agent_start", async (event, ctx) => {
		if (preloaded || envOff("BRAIN_PI_PRELOAD")) return;
		preloaded = true;

		const args = ["--project", projectOf(ctx), "--budget-kb", String(budgetKb)];
		if (!envOff("BRAIN_PI_SLIM")) args.push("--slim");
		const res = await run(prepCmd, args, ctx, ctx.signal);
		const bundle = res.ok ? res.stdout.trim() : "";

		// The AGENTS.md snippet and this extension are meant to compose, not
		// duplicate: if the snippet is already loaded, its instructions are in
		// the prompt and ours would be a second, near-identical copy.
		const snippetLoaded = (event.systemPromptOptions?.contextFiles ?? []).some((f) =>
			f.content.includes(AGENTS_SNIPPET_MARKER),
		);

		const result: BeforeAgentStartEventResult = {};
		if (bundle) {
			result.message = {
				customType: "brain-bundle",
				content: bundle,
				display: false,
			};
		}
		if (guidance && !snippetLoaded) {
			result.systemPrompt = `${event.systemPrompt}\n\n${guidance}`;
		}
		return result;
	});

	// ----------------------------------------------------------- checkpoints

	/**
	 * Checkpoints go through `brain checkpoint --from-pi`, which reads the
	 * session file pi has already written and renders the same structural
	 * extract the Claude Code hooks produce. Dedup (an unchanged leaf entry id
	 * means an unchanged conversation) lives there too, in the state file the
	 * cherryd path already uses — so three triggers firing back to back write
	 * one checkpoint, not three.
	 *
	 * This deliberately does not route through tool dispatch: an automatic
	 * checkpoint is the operator's policy executing, not the model asking, so
	 * it must never surface an approval prompt.
	 */
	let checkpointInFlight: Promise<void> | null = null;

	async function checkpoint(ctx: ExtensionContext, source: string): Promise<void> {
		if (envOff("BRAIN_PI_CHECKPOINT")) return;
		const sessionFile = ctx.sessionManager.getSessionFile();
		if (!sessionFile) {
			warnOnce(ctx, "ephemeral", "session is ephemeral (no session file) — nothing to checkpoint");
			return;
		}
		if (checkpointInFlight) return checkpointInFlight;
		checkpointInFlight = (async () => {
			// No ctx.signal here: a checkpoint fired by compaction or shutdown
			// must outlive the turn that triggered it.
			const res = await run(
				brainCmd,
				["checkpoint", projectOf(ctx), "--from-pi", sessionFile, "--source", source, "--json"],
				ctx,
			);
			if (!res.ok) return;
			try {
				const parsed = JSON.parse(res.stdout.trim() || "{}");
				if (parsed.written && ctx.hasUI) {
					ctx.ui.notify(`Brain: checkpointed (${source})`, "info");
				}
			} catch {
				// Output shape changed or was empty; the write either happened or
				// warnOnce already reported the failure. Nothing to add.
			}
		})();
		try {
			await checkpointInFlight;
		} finally {
			checkpointInFlight = null;
		}
	}

	// The real signal, not a proxy for it: pi tells us a compaction is about to
	// discard context, with the reason. Claude Code's PreCompact parity.
	pi.on("session_before_compact", async (event, ctx) => {
		await checkpoint(ctx, `pi:compact:${event.reason}`);
	});

	// agent_settled, not agent_end: agent_end fires per low-level run and pi may
	// still retry, auto-compact, or drain queued follow-ups afterwards.
	pi.on("agent_settled", async (_event, ctx) => {
		settledTurns += 1;
		if (settledTurns < checkpointEvery) return;
		settledTurns = 0;
		await checkpoint(ctx, "pi:cadence");
	});

	pi.on("session_shutdown", async (event, ctx) => {
		await checkpoint(ctx, `pi:shutdown:${event.reason}`);
	});

	// ------------------------------------------------------------- command

	pi.registerCommand("brain", {
		description: "Brain memory: status | checkpoint | recall <query> | list",
		handler: async (args, ctx) => {
			const [sub = "status", ...rest] = args.trim().split(/\s+/).filter(Boolean);
			if (sub === "status") {
				const res = await run(brainCmd, ["stats"], ctx);
				const line = res.ok ? res.stdout.trim() : "unavailable";
				ctx.ui.notify(`Brain: vault ${vault} — ${line}`, res.ok ? "info" : "warning");
				return;
			}
			if (sub === "checkpoint") {
				await checkpoint(ctx, "pi:manual");
				return;
			}
			if (sub === "recall" || sub === "list") {
				const argv = sub === "recall" ? ["recall", rest.join(" ")] : ["list", ...rest];
				const res = await run(brainCmd, argv, ctx);
				const text = res.ok ? res.stdout.trim() || "(nothing)" : "brain failed";
				// nextTurn: the user asked for this, so it belongs in context, but
				// it should not kick off an LLM turn on its own.
				//
				// `pi`, not `ctx`: sendMessage lives on ExtensionAPI, not on
				// ExtensionCommandContext. `ctx.sendMessage` threw "is not a function"
				// at runtime for every `/brain recall` and `/brain list` — the two
				// command paths a person invokes by hand. Caught 2026-08-25 by the
				// typecheck added that day, having gone unnoticed with no typecheck.
				await pi.sendMessage(
					{ customType: "brain", content: text, display: true },
					{ deliverAs: "nextTurn" },
				);
				return;
			}
			ctx.ui.notify(`Brain: unknown subcommand '${sub}'`, "warning");
		},
	});
}
