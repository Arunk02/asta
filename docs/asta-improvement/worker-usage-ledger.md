# Durable worker attempt usage

Each code, analysis or draft worker CLI invocation now gets a durable attempt record before it starts. The record includes task, stage, executor, configured model, session and timestamps. Completion, exception and cancellation update that same record. Session rotation and quota fallback cannot erase earlier attempts.

For Claude, provider-reported session counters are sampled before and after the invocation; the difference belongs to that attempt. Resuming a session does not charge all prior turns again. The message-ID deduplication from the audit fix applies. Missing logs, unsupported telemetry, and counter resets remain unknown. Failed attempts retain any observed usage, but do not establish complete coverage. Copilot input/cache usage is currently unknown rather than estimated as zero.

`task_result` includes a compact known subtotal and how many recorded attempts have telemetry. `worker_usage.report(task_id)` exposes the per-stage records. New input includes cache creation; cached input and output are separate. No monetary amount is inferred from a subscription or missing price table.

Validation covers resumed stages, rotated sessions, repeated provider records, model changes, failed paid attempts, unknown fallback usage, cancellation, counter resets, persistence before execution, and actual task-CLI integration. The focused worker, authentication, MCP, graph and audit suite passes 148 tests after integration with the approval and strict-MCP fixes. A minimal actual Claude worker probe also returned its expected response and recorded one provider call (2,779 new-input tokens, four output tokens), with no tools or corporate source involved.

Remaining limits: this starts recording new worker attempts after installation, does not reconstruct old deleted session associations, and does not cover parent-chat turns (those are `traces`). Background one-shot calls are covered separately, below. Provider logs may be incomplete after process loss. There is no monetary pricing ledger or verified-success denominator yet. These records are a foundation for that full task-cost accounting, not a claim that it is complete.

## Background one-shot calls

Every `claude_cli.one_shot` and `copilot_cli.one_shot` call made outside a worker attempt now gets one row in the `call_usage` table (`app/call_ledger.py`). This covers memory consolidation, the reader, reviews, context refresh, the call brain and corpus labelling. A row records its purpose, executor, configured model, status and usage.

- The purpose is the calling app function, as `module.function`, unless the caller passes `purpose=`. A call reached only through `create_task` or `wait_for`, with no app frame above it, is `unattributed`. It is never guessed.
- A Claude call with no session ID is now given one (`--session-id <uuid>`), so its own session log can be found after it exits. The log is also found by session ID when the CLI named the project after a resolved cwd.
- Usage is the before/after delta of that session's provider-reported counters, using the same logic as worker attempts (`worker_usage._delta`). Copilot calls are counted, but their usage is `unavailable`, not zero.
- A call inside `worker_usage.attempt` is skipped, so a task leg is never counted twice.
- A call refused before it runs (CLI missing, usage limit) is not a call and gets no row. A failed run keeps its row and any usage it spent.
- Accounting never fails the call. The table keeps the newest 5,000 rows.

`call_ledger.report(hours)` returns a known subtotal per purpose, with measured/total coverage and no monetary cost. `trace_report` appends the 24-hour summary.

The remaining gap: chat turns and these calls are still two tables, and there is no price table, so no single "this cost X" figure exists. That figure would have to be inferred, and nothing here infers one.
