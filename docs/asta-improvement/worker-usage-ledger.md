# Durable worker attempt usage

Each code, analysis or draft worker CLI invocation now gets a durable attempt record before it starts. The record includes task, stage, executor, configured model, session and timestamps. Completion, exception and cancellation update that same record. Session rotation and quota fallback cannot erase earlier attempts.

For Claude, provider-reported session counters are sampled before and after the invocation; the difference belongs to that attempt. Resuming a session does not charge all prior turns again. The message-ID deduplication from the audit fix applies. Missing logs, unsupported telemetry, and counter resets remain unknown. Failed attempts retain any observed usage, but do not establish complete coverage. Copilot input/cache usage is currently unknown rather than estimated as zero.

`task_result` includes a compact known subtotal and how many recorded attempts have telemetry. `worker_usage.report(task_id)` exposes the per-stage records. New input includes cache creation; cached input and output are separate. No monetary amount is inferred from a subscription or missing price table.

Validation covers resumed stages, rotated sessions, repeated provider records, model changes, failed paid attempts, unknown fallback usage, cancellation, counter resets, persistence before execution, and actual task-CLI integration. The focused worker, authentication, MCP, graph and audit suite passes 148 tests after integration with the approval and strict-MCP fixes. A minimal actual Claude worker probe also returned its expected response and recorded one provider call (2,779 new-input tokens, four output tokens), with no tools or corporate source involved.

Remaining limits: this starts recording new worker attempts after installation, does not reconstruct old deleted session associations, and does not cover unrelated parent-chat/background model calls. Provider logs may be incomplete after process loss. There is no monetary pricing ledger or verified-success denominator yet. These records are a foundation for that full task-cost accounting, not a claim that it is complete.
