# Verified lesson promotion

Runtime extraction still quarantines lessons. A caller may promote a candidate only with an independently configured, nonempty tuple of mandatory check IDs. The evaluator must return a nonblank run ID, the candidate's SHA-256, an exact mapping of those IDs to `passed`, matching integer totals, and zero skipped, failed or errored checks. Aggregate pass counts are insufficient.

Before evaluation, the rendered metadata must match the candidate document and its name. After evaluation, both files and the approved predecessor must remain unchanged. Promotion atomically installs the exact tested bytes, archives the predecessor by hash, and records the validation run, content hash and predecessor hash in usage metadata. It does not merge or re-render instructions after testing.

Two reproductions failed before this fix: a document altered before evaluation could cause different metadata-derived instructions to be activated; and a report with skipped checks was accepted if its aggregate pass counts matched.

Validation: 91 focused learning, evolution and promotion tests passed. Tests also cover missing, substituted and failed mandatory checks; wrong hashes; invalid policies; candidate mutation; concurrent predecessor changes; exact-byte promotion; and predecessor retention.

This completes the promotion evidence contract, not a general-purpose lesson evaluator. No runtime extraction is automatically promoted. A trusted task-specific evaluator and a promotion policy must first be integrated; passing unrelated unit tests or asking a model to declare a lesson correct is not sufficient. Automatic regression monitoring/rollback and learner-facing lesson progress remain separate outstanding work.

## Guarded recheck component

`learning_monitor.recheck` accepts a trusted evaluator and mandatory check IDs. It tests an immutable copy of the validated active lesson outside the skill catalog. A behavioral failure can restore a predecessor only if the predecessor's archived bytes match its hash and pass the same independent suite. Missing telemetry, skips, infrastructure errors, absent/corrupt archives and a failing predecessor never trigger replacement. An intervening edit or promotion is rejected before replacement. The decision and evaluator receipts are persisted; restored content gets the new validation receipt.

This API is deliberately not connected to general task failure counters: a failed task does not establish that a lesson caused it. Tests cover a verified regression, unchanged healthy lessons, incomplete/wrong evidence, invalid archives, concurrent edits and evaluator mutation. A trusted task-specific evaluator and scheduling policy still need integration. No generic semantic evaluator, automatic runtime rollout, or full learning milestone is claimed.

## The evaluator and the daily pass (`app/lesson_eval.py`)

The policy lives in code (`CHECKS`, `POLICY_VERSION`); nothing in a lesson can change its own checks. There are six mandatory checks:

| Check | Cost | Fails when |
|---|---|---|
| `structure` | free | front matter or a heading is missing, there is no real procedure step, there is no way to verify it worked, or it is over 6,000 characters |
| `no_ticket_specifics` | free | it contains an issue key, PR number, date, absolute path or commit id |
| `no_secrets` | free | it contains a credential-shaped value |
| `authority_boundary` | free | a procedure step starts by approving, merging, force-pushing, sending, posting or messaging; or anywhere it says `--no-verify`, force push, bypass permissions, skip tests/CI/review/plan/approval, "without asking/approval", `.env`, `guardrails.md`, `ASTA_*=`, or a push to main/master/develop |
| `distinct` | free | its slug words overlap another active lesson's by at least 0.75, so it is a rival rather than an update |
| `rubric` | one Haiku call, tools off | the model, given the exact bytes as delimited data, says it is not general, actionable, verifiable or internally consistent |

- **The rubric runs only if the five free checks pass.** A lesson that already fails costs nothing; its rubric is reported as failed, "not run".
- **Calls are capped per day** (`ASTA_LESSON_EVAL_DAILY_CALLS`; 0 means never; Arun chose 10). The model is set by `ASTA_LESSON_EVAL_MODEL` (Haiku). Each call is recorded in the call ledger as `lesson_eval.rubric`.
- **An unchecked lesson stays inactive.** If the cap runs out or the model's answer is unreadable, the rubric is reported as skipped. That keeps the lesson inactive, and it is retried the next day. It is never passed by default.

The rubric is a model's judgement against a fixed rubric. It is not a replay of the lesson on real tasks. It is one check among six; its pass is required but not sufficient.

`learn.daily_pass` calls `lesson_eval.run_daily()`:

1. **Candidates.** Each candidate goes through `learn.validate_candidate` with this evaluator. A decided candidate (same bytes, same policy) is not judged again. This matters because a promoted candidate's file stays in `.candidates`.
2. **Validated lessons.** These go through `learning_monitor.recheck`, but only when the policy version changed, or when more than two failed runs had the lesson loaded since its last check. Failures only trigger a recheck. Only the evaluator's verdict on the exact bytes, with a passing predecessor, can roll a lesson back. A freshly promoted lesson counts as checked. An unverified recheck is retried the next day.
3. **Older lessons.** Lessons written before quarantine have no validation record. They get the free checks only, and failures are reported once per change. They are never modified or deleted. On 4 Nov, 2 of the 53 live lessons failed, both for naming a specific PR.

`lesson_eval.audit()` returns the state: candidates and their last verdicts, validated lessons, unvalidated older lessons, the older lessons that fail, and today's remaining rubric calls.
