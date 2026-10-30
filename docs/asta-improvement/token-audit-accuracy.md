# Worker session audit accuracy

The public coding pilot exposed two defects in the existing audit: it searched the shared workspace rather than the task worktree for Claude logs, and it summed repeated provider usage snapshots as separate calls.

The lookup now prefers the isolated task checkout and falls back to the legacy workspace path. The parser counts each provider assistant message ID once and uses its latest usage snapshot. Logs without message IDs retain per-record accounting for compatibility.

On the saved pilot sessions, the old parser counted 62 calls; the corrected parser counts 34, matching an independent message-ID tally. Corrected totals are 27,939 output tokens, 97,785 new input tokens (including cache creation), and 2,745,372 cache-read tokens. These are provider-reported counts, not monetary costs, measured savings, or proof that all context was necessary.

Validation: two failing regressions reproduced the missing session and duplicate usage; 33 audit/verdict/usage tests pass. The corrected parser was also checked against both actual public pilot logs and agrees with the independent receipt.

This remains a current-session audit, not the complete task ledger. Capturing all previous stages, retries, failed attempts, background legs, model changes and verified-success denominators is still outstanding. Waste categories remain heuristic estimates, separate from the provider-reported usage totals.
