# What a background worker can and cannot reach

This is the boundary as it stands after #177–#179. It records what is enforced, by what, and what is not, so that nobody describes the current arrangement as containment.

## Enforced

| Boundary | Enforced by | What it stops |
|---|---|---|
| Asta's own API | Task-bound, expiring worker credential (`app/worker_auth.py`), checked server-side on `/api/_invoke`. Every other owner route rejects it. | Acting as Arun through Asta: owner routes, other tasks, other conversations, forged message origin, ungranted tools, writes other than the two approval-staging tools |
| Owner API token | Removed from every fenced task subprocess and task MCP config (`task_fence.env`, `mcp_server.config_entry`). Config fails closed without a worker credential. | A worker presenting the owner token it was handed |
| GitHub decisions that are his | `gh` wrapper first on PATH (`app/shims`), plus each CLI's deny rules (`task_fence.denied`) | `gh pr review/merge/close/reopen` in any task; pushes, PR edits and comments in analysis and draft tasks |
| Outward acts after the task | Server-side only: merge (#173), standalone review/merge (#178), named-person update (#179), each with receipts and revocation rechecks | A worker deciding that a merge or a message happens |
| Stale credentials | Revoked when the leg ends, and pruned at startup (expired, unreadable or finished-task records) | Reuse of a credential left behind by a crashed leg |

## Not enforced

- **Inherited service credentials.** A worker runs as Arun's macOS user, with his environment apart from `ASTA_TOKEN`. A code task must push its branch and open its PR, so `gh` and `git` keep his credential helper, and other provider tokens in the environment remain readable.
- **Files.** Nothing stops a worker reading files his user can read: `.env`, `~/.config/gh`, the keychain via `security`, Asta's SQLite database, other worktrees.
- **Shell.** The `gh`/`git` wrapper is a PATH entry. An absolute path (`/opt/homebrew/bin/gh`), a different client (`curl` against the GitHub API with a token it found), or a script bypasses it. The CLI deny rules narrow this, but they match command prefixes and are not a sandbox.
- **Other MCP servers.** Dev MCP servers configured for code tasks (`ASTA_DEV_MCP`) carry their own credentials and are not routed through the worker credential.

## Why macOS does not give this for free

- `sandbox-exec` profiles still work but are deprecated and undocumented. A deny-by-default profile that still allows builds (JDK, Maven, Node, Xcode toolchains) is brittle, and any mistake fails open.
- A separate Unix user per task needs administrator setup, its own `gh` login and keychain, and file ownership changes across worktrees. That is a deployment change, not a code change.
- Containers (Docker or a Linux VM) are not used on this machine (see the voice-stack memory). Running builds there would change how every Java and Node task runs.

## What closes the gap, in order

1. Run workers with a reduced environment: an allowlist of variables instead of `os.environ` minus one token, with provider tokens removed for analysis and draft tasks.
2. Push and open PRs from the server (as `ship` already does for most code tasks) with a short-lived, repository-scoped GitHub token. Workers would then need no `gh` credential at all.
3. A per-task macOS user, or a VM, for file and shell isolation. This needs Arun's decision and admin setup, so it stays a recommendation here.

Until then, a worker is trusted not to go around the fence deliberately. What is guaranteed is that Asta's own API, the owner token and every outward act Asta itself performs are bound to the task's recorded authority.
