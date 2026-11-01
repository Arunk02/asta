# Verified lesson promotion

Runtime extraction still quarantines lessons. A caller may promote a candidate only with an independently configured, nonempty tuple of mandatory check IDs. The evaluator must return a nonblank run ID, the candidate's SHA-256, an exact mapping of those IDs to `passed`, matching integer totals, and zero skipped, failed or errored checks. Aggregate pass counts are insufficient.

Before evaluation, the rendered metadata must match the candidate document and its name. After evaluation, both files and the approved predecessor must remain unchanged. Promotion atomically installs the exact tested bytes, archives the predecessor by hash, and records the validation run, content hash and predecessor hash in usage metadata. It does not merge or re-render instructions after testing.

Two reproductions failed before this fix: a document altered before evaluation could cause different metadata-derived instructions to be activated; and a report with skipped checks was accepted if its aggregate pass counts matched.

Validation: 91 focused learning, evolution and promotion tests passed. Tests also cover missing, substituted and failed mandatory checks; wrong hashes; invalid policies; candidate mutation; concurrent predecessor changes; exact-byte promotion; and predecessor retention.

This completes the promotion evidence contract, not a general-purpose lesson evaluator. No runtime extraction is automatically promoted. A trusted task-specific evaluator and a promotion policy must first be integrated; passing unrelated unit tests or asking a model to declare a lesson correct is not sufficient. Automatic regression monitoring/rollback and learner-facing lesson progress remain separate outstanding work.

## Guarded recheck component

`learning_monitor.recheck` accepts a trusted evaluator and mandatory check IDs. It tests an immutable copy of the validated active lesson outside the skill catalog. A behavioral failure can restore a predecessor only if the predecessor's archived bytes match its hash and pass the same independent suite. Missing telemetry, skips, infrastructure errors, absent/corrupt archives and a failing predecessor never trigger replacement. An intervening edit or promotion is rejected before replacement. The decision and evaluator receipts are persisted; restored content gets the new validation receipt.

This API is deliberately not connected to general task failure counters: a failed task does not establish that a lesson caused it. Tests cover a verified regression, unchanged healthy lessons, incomplete/wrong evidence, invalid archives, concurrent edits and evaluator mutation. A trusted task-specific evaluator and scheduling policy still need integration. No generic semantic evaluator, automatic runtime rollout, or full learning milestone is claimed.
