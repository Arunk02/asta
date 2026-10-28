# Reproduction before solution planning

A request to reproduce a failure used to start the read-only plan leg. It could
trace the code but could not add a reproducer; a status question at the gate
could start another planning turn. Task 454 exposed both behaviors.

Explicit reproduction requests in trusted user words now select a local test
phase before planning. A named correction is handled before pending drafts and
plan-gate replies; an owner-authenticated `/api/tasks/{id}/reproduce` endpoint
provides the same operation. Worker credentials cannot call it. A paused task
keeps its quota deadline and saves the direction for its next run.

The reproduction phase enables local test edits without setting `task_approved`.
It uses the fence that denies commits, pushes, PR actions and outward messages.
It requires a fresh, in-checkout JUnit XML report, executed test cases, no setup
errors, expected/actual behavior and the command used. Claimed reproduction must
agree with assertion failures; a passing result stops with “not reproduced.”
A confirmed failing assertion advances to a separate, read-only solution plan.
Explicit conditional fix permission applies only after that evidence exists;
requests for outside approval and unanswered plan questions still hold.

Production files are hashed before the first attempt and compared after the
leg, including across quota retries. Changes stop progression and leave the
diff for inspection. This is a workflow check, not a filesystem sandbox. The
worker supplies the report and the semantic explanation; JUnit validation does
not independently prove that the chosen assertion faithfully represents the
reported business incident. The test and expected/actual behavior remain
reviewable evidence. Build/dependency failures and missing reports are blockers,
not confirmed bugs. Non-JUnit repro harnesses need a future evidence adapter.

Tests include real checkpointed graph transitions, quota pause/resume, bounded
worker credentials, rejection of stale/outside/empty/error reports, production
edit detection, and a real pytest subprocess producing JUnit evidence.
