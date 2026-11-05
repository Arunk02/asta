# Asta delivery record — updated 5 November 2026

Arun authorized implementation, PR creation and merging after checks, using his personal GitHub account. Work is isolated from the running checkout. A GitHub merge does not mean the live assistant has been restarted or its data migrated.

| Batch | Result | Evidence and limits |
|---|---|---|
| [#170](https://github.com/Arunk02/asta/pull/170), reliability foundations | Merged, including integration with the user's newer #169 changes | Authentication, atomic draft delivery, plan history, context priority, explicit contact/follow-up authority, learning quarantine. Integrated local suite: 5,090 passed, 28 skipped; final CI pytest and DOM passed. See the original implementation checkpoint for scope. |
| [#171](https://github.com/Arunk02/asta/pull/171), shared issue recognition | Merged | Running investigations can be shared for the same explicit service/environment startup failure; recent completed analysis provides evidence for a fresh check. Regression models the two-report incident. Local: 5,112 passed, 28 skipped; CI passed. Different cases, builds and environments are not silently combined. |
| [#172](https://github.com/Arunk02/asta/pull/172), memory continuity | Merged | Retains facts and episodes with immutable history; source/workspace provenance and project-filtered recall. Final focused run: 117 passed; final CI pytest and DOM passed. Earlier full run exposed two stale test mocks, which were repaired before final CI. Already lost memories are not reconstructed automatically. |
| [#173](https://github.com/Arunk02/asta/pull/173), task merge permission | Merged | Carries a requested merge through plan revisions, binds to the checked commit, preserves uncertain receipts and waits for all task PRs. Full local pre-integration batch: 5,109 passed, 28 skipped; rebased focused run: 96 passed; final CI pytest and DOM passed. |
| [#174](https://github.com/Arunk02/asta/pull/174), contact reply integration | Merged | Durable contact grants now reach ordinary answer delivery and share their daily cap. Concurrent/uncertain automatic answers retain receipts instead of becoming another sendable draft. Focused permission, reply, cross-service and follow-up run: 71 passed. Full local suite: 5,129 passed, 28 skipped. Final CI verifies the rebased branch. |

## Subsequent merged work

GitHub merge receipts were checked on 30 October. These are delivered code and regression coverage; they do not establish deployment or a successful live integration.

| PRs | Delivered behavior | Remaining boundary |
|---|---|---|
| [#175](https://github.com/Arunk02/asta/pull/175), [#176](https://github.com/Arunk02/asta/pull/176) | Repository/task-bound PR actions; measured token usage distinguished from estimates | A complete per-task cost ledger and verified-success denominator remain outstanding. |
| [#177](https://github.com/Arunk02/asta/pull/177), [#180](https://github.com/Arunk02/asta/pull/180), [#183](https://github.com/Arunk02/asta/pull/183), [#190](https://github.com/Arunk02/asta/pull/190) | Scoped worker API credentials, expired scope cleanup, server-secret filtering, registered-workspace reads | These are API/process controls, not an OS filesystem isolation boundary. |
| [#178](https://github.com/Arunk02/asta/pull/178), [#179](https://github.com/Arunk02/asta/pull/179) | One instruction can carry standalone review/approve/merge and a named task notification | Authority stays bound to the requested task, repository, action and contact. |
| [#181](https://github.com/Arunk02/asta/pull/181), [#182](https://github.com/Arunk02/asta/pull/182) | Scoped recall before limits, second-reporter context, full plan previews, memory correction/restore and context refresh | Representative real-task quality still needs measurement. |
| [#184](https://github.com/Arunk02/asta/pull/184), [#185](https://github.com/Arunk02/asta/pull/185) | Durable guide-to-setup stages, call/meeting lifecycle and isolated end-to-end tests | Real setup/access and call pilots need concrete targets and authorization. |
| [#186](https://github.com/Arunk02/asta/pull/186)–[#192](https://github.com/Arunk02/asta/pull/192) | Exact dictated sends, recipient-aware follow-up stop, restart-safe intake, delivery retries, review freshness and named draft approval | Regression coverage is not evidence that messages were sent to real contacts during this work. |
| [#193](https://github.com/Arunk02/asta/pull/193) | Requested reproduction before solution planning; fresh JUnit and production snapshot gates | Live pilot exposed stage-scope and report handling defects; see pending #197. |
| [#194](https://github.com/Arunk02/asta/pull/194) | Required named checks and exact evaluated bytes before lesson promotion | Trusted task-specific evaluators, monitoring and guarded rollback remain outstanding. |
| [#195](https://github.com/Arunk02/asta/pull/195) | All 21 mandatory gates run in CI; missing/skipped evidence fails | 88 gate test instances do not replace the 70-task repeated benchmark. |
| [#196](https://github.com/Arunk02/asta/pull/196) | Missing Claude login pauses tasks and preserves resumable sessions | 49 focused tests plus full pytest/DOM and mandatory gate CI passed before merge. |

## Active real coding pilot

The public Asta lesson-promotion pilot uses an isolated database, API and source checkout. The first worker reproduced the defects but edited production during reproduction; the guard blocked it. A subsequent worker stayed within scope, exposing report artifact/path handling defects. [PR #197](https://github.com/Arunk02/asta/pull/197) addresses these with dedicated reproduction instructions, exact-new-report exemption and unambiguous repository-relative report resolution. Its 122 focused tests pass; CI and merge are pending at this checkpoint.

After those corrections, the real worker produced fresh failing JUnit evidence for both defects with production unchanged, and the evidence gate confirmed reproduction. The same task is proceeding through plan, implementation and verification. Full coding success has **not yet been established**. The independent fix already merged in #194 is not attributed to this pilot.

Counts overlap and must not be summed. Environment-dependent skips are not proof of passing live integrations. Tests use isolated databases and fake communication transports; none of these checks messages real contacts.

```mermaid
flowchart LR
    A[Your instruction] --> B[Saved task or contact permission]
    B --> C[Use current project evidence]
    C --> D[Do the authorized work]
    D --> E[Verify exact result]
    E --> F[Save receipt and continue]
    E --> G[Uncertain result: reconcile before retry]
```

## Merged and deployed, 30 October – 5 November (to #220)

The merge receipts were checked on 5 November. Since #215, each batch was deployed only when Asta was idle, and was checked afterwards:
- one listener
- health and status 200
- 39/39 daemons
- WhatsApp and Teams bridges up

Each batch has a data backup in `data/backup-<date>-pre-<n>`. Every batch passed the frozen full suite and CI before merge.

| PRs | Delivered behavior | Remaining boundary |
|---|---|---|
| [#197](https://github.com/Arunk02/asta/pull/197), [#199](https://github.com/Arunk02/asta/pull/199) | Reproduction workers stay scoped to evidence; conditional fix authority survives reproduction and planning | — |
| [#200](https://github.com/Arunk02/asta/pull/200)–[#202](https://github.com/Arunk02/asta/pull/202), [#208](https://github.com/Arunk02/asta/pull/208) | Scoped Claude workers get only the selected MCP servers. Worker usage is kept across stages, failures and retries. Background one-shot calls are measured from their own session log (`call_usage`) | Copilot one-shots report no usage, and are recorded as unavailable, not zero |
| [#203](https://github.com/Arunk02/asta/pull/203), [#209](https://github.com/Arunk02/asta/pull/209) | Evidence-gated lesson regression rollback. A trusted lesson evaluator (five free checks, then a capped Haiku rubric) runs in the daily pass | — |
| [#204](https://github.com/Arunk02/asta/pull/204)–[#207](https://github.com/Arunk02/asta/pull/207), [#210](https://github.com/Arunk02/asta/pull/210) | Context refresh: fallback hints are not treated as ownership; unmapped entry points are found by Temporal annotation; commented-out classes are ignored | — |
| [#211](https://github.com/Arunk02/asta/pull/211), [#212](https://github.com/Arunk02/asta/pull/212) | Versioned acceptance benchmark: 70 cases plus 22 authority gates, pass^3; v2 may verify a case by tests of the real code | — |
| [#213](https://github.com/Arunk02/asta/pull/213), [#214](https://github.com/Arunk02/asta/pull/214) | Meeting prep from the meeting's own Teams thread; recap action items kept with owners. Learning sessions whose answers are graded against their source | — |
| [#215](https://github.com/Arunk02/asta/pull/215)–[#217](https://github.com/Arunk02/asta/pull/217) | One ask, one task, one PR. A parked task's PR is still watched. A stale or overtaken Teams draft is held instead of sent on a yes | — |
| [#218](https://github.com/Arunk02/asta/pull/218), [#219](https://github.com/Arunk02/asta/pull/219) | One case, one conversation. Side-turn answers and ceiling-cut replies are kept. A dead question's answer reaches the brain. A named target is not asked back. Hand-overs cover follow-ups. Findings and what was said are carried into every lane. A draft resting on an unconfirmed finding is flagged and never sent automatically. One correction draft when a finding overturns what was said. Prove-first tasks can end `no_change` | The correction draft depends on the analysis writing `Corrects:` |
| [#220](https://github.com/Arunk02/asta/pull/220) | A red SonarQube gate is read from Sonar (`ASTA_SONAR_TOKEN`): failing conditions, issues on the change and uncovered lines. The CI fix round plans from them | Needs his token in `.env`; without one it says so and stops |

**Benchmark** (see `benchmark.md`):
- **v3, deterministic, pass^3:** 77/80 at #218, with the gate at 22/22. #219 covered the one missing case.
- **Live case comparison:** Copilot (claude-sonnet-5), Claude Sonnet and Claude Opus each passed 9/9, with each model confirmed from its transcript.

## Remaining acceptance work

1. **OS isolation.** A worker OS/filesystem boundary needs a macOS user or VM. That is a system change Arun makes.
2. **Live pilots.** The live Decision Hub setup and call pilots need concrete inputs and authorization.
3. **Tool-using live tier.** A live tier where the brain gathers evidence with tools waits on serving the sim world to a brain safely.
4. **Cost per verified success.** The usage ledgers exist; the per-task cost divided by verified successes has not been reported yet.

No 10/10 rating or token-saving percentage is claimed.
