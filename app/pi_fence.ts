/**
 * The task fence for a pi leg — the same rule Claude legs get through
 * --disallowed-tools, enforced in pi's own tool_call hook.
 *
 * Asta passes the rule in, it is never decided here (app/task_fence.py owns it):
 *   ASTA_PI_DENY       JSON list of command prefixes refused anywhere in a bash
 *                      command ("gh pr merge", "git push", …)
 *   ASTA_PI_PLAN_ONLY  "1" before Arun approved: no write, no edit, no commit,
 *                      no push, no PR — the plan gate, made structural.
 *
 * The gh/git wrappers on the task's PATH (app/shims) stay the second layer;
 * this one stops the call before it runs and tells the model why.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const PLAN_DENY = ["git commit", "git push", "gh pr create"];

function prefixes(): string[] {
	try {
		const raw = JSON.parse(process.env.ASTA_PI_DENY || "[]");
		return Array.isArray(raw) ? raw.map(String) : [];
	} catch {
		return [];
	}
}

/** Every simple command in a shell line: split on ; && || | newlines and $( ). */
function segments(command: string): string[] {
	return command
		.split(/\n|;|&&|\|\||\||\$\(|`|\(|\)/)
		.map((s) => s.trim().replace(/^(?:sudo|env|command|exec|nohup|time)\s+/, ""))
		.map((s) => s.replace(/^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+/, ""))
		.filter(Boolean);
}

/** `/usr/bin/git -C dir --no-pager push` → `git push`: the tool by name, its
 * global options (and their values) dropped, so a prefix matches the subcommand. */
function canonical(seg: string): string {
	const w = seg.split(/\s+/).filter(Boolean);
	if (!w.length) return "";
	const tool = w[0].split("/").pop() || w[0];
	const out = [tool];
	for (let i = 1; i < w.length; i++) {
		const x = w[i];
		if ((tool === "git" && (x === "-C" || x === "-c")) || (tool === "gh" && (x === "-R" || x === "--repo"))) {
			i++;
			continue;
		}
		if (out.length === 1 && x.startsWith("-")) continue;
		out.push(x);
	}
	return out.join(" ");
}

export function refused(command: string, deny: string[]): string | undefined {
	for (const seg of segments(command)) {
		const s = canonical(seg);
		for (const p of deny) {
			const q = canonical(p);
			if (s === q || s.startsWith(q + " ")) return p;
		}
	}
	return undefined;
}

export default function (pi: ExtensionAPI) {
	const planOnly = process.env.ASTA_PI_PLAN_ONLY === "1";
	const deny = [...prefixes(), ...(planOnly ? PLAN_DENY : [])];

	pi.on("tool_call", async (event) => {
		if (planOnly && (event.toolName === "write" || event.toolName === "edit")) {
			return {
				block: true,
				reason: "Planning leg: Arun has not approved the plan yet, so no file may be changed. Put the change in the plan.",
			};
		}
		if (event.toolName !== "bash") return undefined;
		const command = String((event.input as { command?: string }).command || "");
		const hit = refused(command, deny);
		if (hit) {
			return {
				block: true,
				reason: `\`${hit}\` is blocked in an Asta task — that step is Arun's, done with his yes. Put what you would do in your answer.`,
			};
		}
		return undefined;
	});
}
