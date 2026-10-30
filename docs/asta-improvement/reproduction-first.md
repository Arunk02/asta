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

## Real worker pilot: stage completion scope

A public-source Claude pilot reproduced two lesson-promotion defects, then edited production code in the same reproduction invocation. The production snapshot guard rejected that attempt. The worker also returned a generic JSON fence rather than the required reproduction evidence fence. This was not a successful end-to-end pilot.

The reproduction leg now gets its own executor instructions (`agents/reproduce.md`), explicitly stops after reporting the test evidence, and does not inherit the generic implementation outcome contract or an earlier solution plan. Claude receives the stage instructions through its appended system prompt; both executors receive them at the end of the task prompt. The existing snapshot and JUnit checks remain the progression gate. These instructions improve stage adherence; they are not OS-level write isolation.

The next real worker attempt correctly stopped after reproduction and left source unchanged, but its new `reports/junit-repro.xml` was treated as a production edit. The guard now validates the evidence first and exempts only that exact newly created report from the snapshot comparison. Overwriting an existing file or changing production source still blocks progression. Regression coverage includes all three cases; the combined reproduction, graph and context suite passes 121 tests.
