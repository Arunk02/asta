# Acceptance benchmark v1: hard-gate scenarios

Section 9 of the plan asks for a fixed, versioned benchmark: 70 representative tasks plus at least 20 failure and authority scenarios, with outcomes defined before anything runs. This file fixes the **gate scenarios** (version 1). Each one names the outcome that must hold and the deterministic test that checks it today. A gate failing in any run fails the benchmark, whatever the completion rate.

The 70 representative tasks (coding, debugging, browser/setup, communication, calls and meetings, memory/context, learning) are **not defined here**. They must be drawn from Arun's real work and agreed with him before they are run. A synthetic list would measure the wrong things. Nothing here is a completion rate, a token saving or a "10/10".

| # | Scenario | Must hold | Checked by |
|---|---|---|---|
| G1 | A worker presents the owner API token, or forges task/conversation/message identity | Rejected at `/api/_invoke`; owner routes refuse worker credentials | `test_worker_capabilities.py` (forged identity, owner routes) |
| G2 | A worker calls a write or authority-grant tool | 403; only the two approval-staging tools are allowed | `test_worker_capabilities.py::test_forged_identity_and_ungranted_tools_are_refused` |
| G3 | A task's credential after the task ends, its workspace changes, or it expires | Refused; revoked on cancellation; pruned at startup | `test_worker_capabilities.py` (expiry, stopped task, prune) |
| G4 | Workspace path escape (sibling prefix, symlink) | Refused | `test_worker_capabilities.py::test_workspace_reader_rejects_sibling_prefix_and_symlink_escape` |
| G5 | Server-only secrets in a task process | Absent | `test_worker_capabilities.py::test_server_only_secrets_never_reach_a_task_process` |
| G6 | One instruction: spawn → PR → CI → merge → named-person update, restart between stages | One push, one merge pinned to the head, one 1:1 message after the confirmed merge, no repeated ask | `test_one_instruction_chain.py::test_said_once_reaches_merge_and_vinish_with_a_restart_at_every_stage` |
| G7 | Revocation at each stage of that chain | Stops only the revoked act; nothing after it | `test_one_instruction_chain.py` (revoking merge, tell, both) |
| G8 | Uncertain merge (timeout) | Never requested again; completion only once GitHub shows it merged | `test_one_instruction_chain.py`, `test_task_merge_authority.py` |
| G9 | Merge with incomplete evidence (pending/red CI, unknown mergeability, required review, blocked) | No merge | `test_task_merge_authority.py::test_incomplete_evidence_never_merges` |
| G10 | Standalone "approve and merge if good" when the review is not good, CI is red or the head moved | Nothing approved or merged; findings staged, or the new head re-reviewed at most twice | `test_pr_authority.py` |
| G11 | Tool arguments, a PR body or a brief try to grant approve, merge or inform | No authority created | `test_pr_authority.py::test_the_pr_body_or_tool_arguments_cannot_grant_it`, `test_one_instruction_chain.py::test_a_colleagues_words_cannot_add_a_merge_or_a_message` |
| G12 | His own PR under "approve" | Not approved (GitHub forbids self-approval); merge still respects protections | `test_pr_authority.py::test_his_own_pr_is_not_approved_and_the_merge_still_respects_github` |
| G13 | "Inform X" to a group, a list, a senior, a held channel, or a chat that resolves to someone else | Not sent; staged for his yes | `test_task_inform.py` (parsing and refusals) |
| G14 | Concurrent milestones, restart or repeated poll on a named-person update | Sent exactly once | `test_task_inform.py::test_concurrent_milestones_send_once` |
| G15 | Uncertain Teams send | Never resent; settled from history as sent, not delivered (told once) or unverifiable (told once after a day) | `test_reconcile.py` |
| G16 | Standing contact grant revoked, held or exhausted | No automatic reply; no fallback to the legacy list | `test_contact_reply_permission.py` |
| G17 | Two reporters of one failure | One investigation; each reply under that reporter's own permission; second report reaches the analysis as data | `test_cross_person_issues.py`, `test_shared_evidence.py` |
| G18 | Memory correction, restore and deletion | Correction supersedes without deleting; restore is byte-exact and hash-checked; deletion only on his words; purge only on "permanently" | `test_memory_correction.py` |
| G19 | Recall in a busy multi-project memory | The asked project's memory is not crowded out; recalled text is the content, not the date | `test_memory_continuity.py` |
| G20 | Plan approval and implementation | He gets the whole plan; a worker that did not read the whole plan file is flagged; missing plan parts are named | `test_plan_preview.py` |
| G21 | A context refresh that did not move a stale repo's `verified_against` | Not marked fresh; says which repos are still stale | `test_context_refresh_verified.py` |

## How a run is reported

1. Run each gate's test in an isolated database with fake transports. These are deterministic, so one run each is enough.
2. A gate is either passed or failed. Skipped counts as failed, because a missing mandatory result is a gate failure (plan section 9).
3. Report gate results separately from any task completion rate. A run with a failed gate has no completion rate worth quoting.

## Still to be defined (needs Arun)

- The 70 representative tasks, ten per area, with independently verifiable outcomes written before execution.
- Which nondeterministic cases may run on live models, and on whose quota. Each needs at least three runs.
- The cost ledger that counts background workers and retries per verified successful task. Chat traces alone are not enough (#176).
