# Architecture review, August 2026 — findings register

Raised 2026-08-19 from a full review of the running system. Every number here was
measured against the live install, not inferred from the source. Follows the
convention of [ARCHITECTURE-REVIEW-2026-07.md](../ARCHITECTURE-REVIEW-2026-07.md):
each item is closed in place with what was done and how it was proved.

**Scope: `booking` is the only workspace.** `iom-workspace` was removed on
2026-08-19 — Arun does not use it, and its unverifiable repos were the source of
finding 3's evidence.

| # | Severity | Finding | Where | Status |
|---|---|---|---|---|
| 1 | Critical | One cached message short-circuits the live fetch; partial history reported as complete | `agent.py` read-history | **closed** |
| 2 | Critical | Quality gates all switched off (VERIFY, RELEVANCE, TASK_SPEC, DEV_MCP) | `.env` | **closed** |
| 3 | Critical | Drift detection silently skips repos without `.git` — they report clean forever | `providers/indexed.py:_sha_drift` | **closed** |
| 4 | Critical | Unattended CLI runs with all permissions and 7 live secrets | `claude_cli.py`, `copilot_cli.py` | accepted risk (Arun, 2026-08-19) |
| 5 | High | Resolver returns context with no freshness signal | `providers/indexed.py:resolve` | **closed** |
| 6 | High | `attention.consider` reaches 3 of 56 push sites | `attention.py`, `notify.py` | **closed** |
| 7 | High | Asta never reviews the diff it produced | `tasks.py:_finish_code` | **closed** |
| 8 | High | No rollback point for a task's git actions | `tasks.py`, `repo_ops.py` | **closed** |
| 9 | High | A code task is not confined to its workspace | `tasks.py:_cwd` | **closed** |
| 10 | High | One task at a time per workspace | `tasks.py:_ws_lock` | **closed** |
| 11 | High | A browser per Teams operation, 2.49s fixed cost | `teams_bridge.py` | **closed** |
| 12 | High | 63 selectors, no health check | `teams_bridge.py`, `meetings.py` | **closed** |
| 13 | High | A quarter of error handling is silent | 35 pass + 39 suppress / 280 try | **closed** |
| 14 | High | Tests prove mechanism, never answer correctness | `tests/` | **closed** |
| 15 | Medium | Prompt floor ~6,100 tok; narrowing tuned for 32 tools, now 58 | `capabilities.py`, `tool_index.py` | **closed** |
| 16 | Medium | `resolve_context` returns 20,000 chars against a ~350-token contract | `providers/indexed.py` | **closed** |
| 17 | Medium | Regexes alone decide what is said aloud in a call | `meetings.py:classify_line` | **closed** |
| 18 | Medium | No merge capability exists | `capabilities.py` | **closed** |
| 19 | Medium | Code work has no free fallback when quotas are down | `tasks.py:_run_code_leg` | **closed** |
| 20 | Medium | Four modules carry a third of the system | main/tasks/agent/meetings | **closed** |
| 21 | Medium | Synchronous SQLite on the event loop | `store.py` | **closed** |

## Measured baseline, 2026-08-19

Kept so the effect of each fix can be compared against something real.

- code tasks: 54% done, 39% cancelled, median 7.7 min, p90 32 min (n=46)
- analysis tasks: 76% done, median 1.3 min, p90 3.1 min (n=17)
- interruptions: 158 sent, 92 already read elsewhere, 66 ignored
- Teams fixed overhead: 2.49s per operation (0.74s Chromium + 1.75s app boot)
- prompt floor: ~6,100 tokens per turn before any content
- voice: kokoro 8.9s cold / 1.07s warm, chatterbox 9.0s, mic switch 0.38s

---

## Accepted risks

**4 — unattended CLI with all permissions and every secret.** Arun's decision on
2026-08-19: code tasks run in his presence and on his command, so the blast
radius is supervised. Recorded rather than dropped — if Asta is ever left to run
unattended, or a task can be triggered by something other than him, this is the
first thing to revisit.

## New findings raised while fixing

| # | Severity | Finding | Where | Status |
|---|---|---|---|---|
| 22 | High | Nothing is learned from a task he cancelled or rejected — 39% of code tasks — which is the richest signal there is | `learn.should_extract` | **closed** |
| 23 | Medium | A pooled browser must be closed on shutdown or the process leaks Chromium | `main.py` lifespan | **closed** |
| 24 | High | Three capabilities were unreachable: two modules called by nothing in `app/`, and a capability naming a route that did not exist | `selector_health.py`, `evals.py`, `main.py` | **closed** |
| 25 | Medium | A watcher's failure reason outlives the failure — a healed fault keeps being reported as the cause | `attention.note_scrape` | **closed** |

## Closed

### 1 — cached history reported as complete — CLOSED 2026-08-19
`store.teams_history_covers()` proves both edges before the cache is trusted: the
oldest stored message sits at or before the window start (a read once scrolled
back past it) AND the chat was last read at or after the window closed. Either
edge open means fetch. `agent.teams_history` consults it instead of `if not rows`.

Proved by `tests/test_review_2026_08.py` — nine cases including the real one
(one message cached, ten on the server, all ten returned). Two mutants caught:
restoring any-row-wins, and dropping the front edge. A pre-existing test in
`test_teams_history.py` had encoded the bug — it stored one message and asserted
a cache hit — and was corrected to describe a thread genuinely read across the
window.

### 3 — repos exempt from drift detection — CLOSED 2026-08-19
`_sha_drift` reported clean for anything it could not check. A repo with no git
checkout, an unreadable `_index.json`, or no recorded `verified_against` now
reports "cannot verify" instead of being skipped. A repo with no index at all is
still skipped quietly — it was never in the context, so it is not a staleness
claim. `iom-workspace`, where six of seven repos were being skipped, was removed.

### 5 — context arrived with no freshness signal — CLOSED 2026-08-19
`resolve()` now prefixes every payload with one line: verified against current
HEAD, stale-or-unverified with the reasons, or unknown when the drift check
itself failed. The model answering can no longer mistake never-verified context
for fresh context. A broken drift check degrades to a label, never to a missing
answer.

### 6 — the ledger reached 3 of 56 push sites — CLOSED 2026-08-19
`attention.consider` moved inside `notify.notify`, beside `delivery`, so every
push consults it by construction. The three sites that already asked pass
`considered=True` — without it their own approved push is re-keyed, reads as
already-notified, and is suppressed. Suppressed items still reach the UI bell,
so nothing is lost. With `ASTA_ATTENTION` unset the path is byte-identical to
before. Five test doubles were carrying a stale `notify()` signature and were
brought up to date.

### 16 — resolver payload 60x its contract — CLOSED 2026-08-19
`ASTA_RESOLVE_CHARS`, default 6,000 (~1,500 tokens), down from 20,000 (~5,000).
Documented in `.env` and `.env.example`; a test asserts the cap cannot drift back
above 8,000.

### 2 — quality gates switched off — CLOSED 2026-08-26
`ASTA_VERIFY=1`. But flipping the flag alone was measured to be worthless here,
and the review's own recommendation was wrong on that point: every repo in the
booking workspace is a multi-module Maven build, `_autodetect` deliberately
refuses to run heavy suites, and the poms sit one level down in `service/`,
`componenttest/` and `perftest/` where nothing was looking. The gate would have
been ON and verifying nothing — indistinguishable from a gate that works.

Three changes close that:

- `verify._build_files` looks one level down, so a multi-module repo is no
  longer mistaken for a repo with no tests.
- `verify.unconfigured()` names any repo that plainly has a suite but no command
  Asta may run, so "no oracle" stops being silent.
- `data/verify-commands.json` maps repo -> command on ASTA's side. Arun's repos
  are work repos; a `.asta-verify` dotfile in each would sit in `git status`
  forever and eventually ride along in a commit nobody meant to make. A command
  the repo declares itself still wins.

**Closed 2026-08-26.** Arun supplied the commands and all three repos are now
configured — `verify.unconfigured()` returns None for each:

    telikos-booking-service              mvn -q -f service/pom.xml clean test
    telikos-email-service                mvn -q -f service/pom.xml clean test
    telikos-activityplanworkflow-service mvn -q -pl service -am clean test

Three things were learned running them for real, and each is encoded rather than
remembered:

- **Checkstyle is bound to `validate`** in these projects, so one `test` run
  covers checkstyle, compilation and unit tests. No separate lint command.
- **`-am` is not optional for activityplanworkflow.** `service` depends on five
  siblings in the same repo; without `-am` those resolve from the local Maven
  repository instead of the working tree, so a change to a sibling would not be
  checked at all — a gate that runs, passes, and verified nothing.
- **`clean` is mandatory, not hygiene.** MapStruct raises a `FilerException` on
  an incremental rebuild. `verify._with_clean` retries once when that signature
  appears, so a stale-target failure is not reported as a code failure.

The gate distinguishes *the check failed* from *the check could not run*: an
infra failure (`_INFRA_FAILURE`) and a timeout both set `ran=False`, because
"could not verify" reported as "verified broken" sends Arun to debug his own
correct code.

### 7 — Asta never read its own diff — CLOSED 2026-08-19
`review.review_own_diff()` reviews a diff Asta wrote, using the same project
conventions the PR reviewer uses. The prompt asks for problems ONLY and forbids a
summary, because a model asked to describe its own change describes it
approvingly; a clean review returns "" and adds nothing to the completion
message. Wired into `_finish_code`, so the DONE message carries what its own
review found. Never blocks completion — a review that fails is worth less than
the diff it was reviewing.

### 8 — no rollback point — CLOSED 2026-08-19
`mark_rollback_point()` records branch and SHA for every repo before
`_prepare_branches` moves anything; `rollback(task_id)` restores them. It refuses
to touch a repo with uncommitted changes — a hard reset over his own half-finished
edit would be a worse incident than the one being undone — and it does NOT delete
the task's branch, so the undo is itself reversible.

### 9 — code tasks not confined to a workspace — CLOSED 2026-08-19
`tasks.code_cwd()` replaces the silent fallback to Asta's own root. One
registered workspace resolves to it; several refuse to guess; none refuses
outright. Analysis tasks keep the lenient path — they only read. While testing
this, the SUITE was found to be reaching the real booking workspace:
`workspace_tools` re-exports `WORKSPACES` at import time, so patching
`app.workspace` alone left `tasks` looking at live config. Both bindings are now
stubbed in conftest.

### 11 — a browser per Teams operation — CLOSED 2026-08-19
One context is kept alive and reused, verified with a real page round-trip before
every hand-out and discarded on any doubt: dead renderer, age past
`TEAMS_POOL_MAX_AGE`, or an operation that failed with the page in an unknown
state. Headed contexts (calls) are never pooled — a call owns its window.

**Measured: 2.08s for the first operation, 0.01s for every one after it.**

That made frequent polling free, so `TEAMS_ACTIVITY_POLL` went 300s -> 60s. The
old interval was chosen when a poll cost a browser launch, and it was the thing
standing between someone pinging Arun and Arun knowing about it.

### 22 — nothing learned from a task he stopped — CLOSED 2026-08-19
`should_extract` required a status of done or sent. So 39% of code tasks — the
largest category after success — taught nothing, while a run that merely needed
two attempts taught something. Exactly backwards: he kills a task when Asta
misread what he wanted, and he does it within minutes of it happening.

- `STOPPED_BY_HIM = ("cancelled", "rejected")` always extracts, regardless of
  rounds. `failed` deliberately does NOT — that is the machinery breaking, not
  Arun disagreeing, and distilling a crash into a procedure teaches the wrong
  thing.
- The extraction prompt asks **what was misread**, never what worked. Asking
  "what worked here" of a run he stopped would distil the very thing he rejected
  into a procedure. It is told to reply with nothing rather than invent a lesson
  when the stop teaches nothing (he changed his mind, something else broke).
- His own words carry through: `reject(why=...)`, `cancel(why=...)`, and — the
  richest case — a mid-task **redirect** in `main.py` now passes `user_text`
  straight through. That sentence is him naming the gap between what he asked for
  and what Asta understood, and it used to be discarded with the task.
- The transcript handed to the learner is three parts: what he asked for, what
  Asta had done when he stopped it, and why he stopped it.

A pre-existing test asserted `rejected -> False`. It encoded the old policy and
was updated with the reasoning rather than the value alone.

### 23 — the pooled browser outlived the process — CLOSED 2026-08-19
Raised while fixing 11. A context kept alive between operations is one the
process owns: without closing it, a restart orphans a Chromium holding the Teams
profile and the next start finds it locked by something nobody is watching.
Closed in the shutdown hook, and closing twice is harmless.

### 21 — SQLite on the event loop — CLOSED 2026-08-26
The first measurement closed this as a non-problem: p50 0.2-0.5 ms, worst p99
4.7 ms, and wrapping hundreds of call sites in `to_thread` would cost more than
the queries. True, and the wrong measurement. Splitting setup from query:

    kv_get  0.231 ms total   of which the QUERY was 0.002 ms

**Ninety-nine percent of every store call was opening a connection** and
re-running `PRAGMA journal_mode=WAL`. One thread-local connection, reused, keyed
on `DB_PATH` so the test isolation rule still gets its own:

    kv_get    0.231 -> 0.003 ms      get_task  0.208 -> 0.008 ms
    worst p99 4.735 -> 0.401 ms

`busy_timeout=5000` came with it: with three tasks running in parallel there now
IS a second connection, and "database is locked" would surface as a lost
notification rather than a delay. Call sites use `with _connect() as conn:`,
which in sqlite3 is a TRANSACTION context manager rather than a closing one, so
nothing about their behaviour changed.

### 20 — four modules carry a third of the system — CLOSED 2026-08-26
Not by reshuffling: a five-thousand-line move is unreviewable, risks a green
suite and buys no capability. By extracting the seam this session created.
`meetings` had grown to 1,543 lines covering four unrelated jobs — building
invites, running a call, reading captions, and deciding what to SAY about them.

`app/call_brain.py` takes the fourth. The first three are mechanics; that one is
judgement, and it is the part with a measurable right answer — which is why the
eval harness can now reach it without a call existing at all. It touches no
microphone, no browser and no live call, and a test asserts it cannot start.

    meetings.py  1,543 -> 1,355      call_brain.py  252 (new)

Names are re-exported from `meetings`, so every caller and test is unchanged —
and so monkeypatching `meetings.answer_from_knowledge` still reaches the
orchestration, which is why that call site uses the local name.

The other three modules are left alone deliberately. Size is a symptom to watch,
not a defect to fix while capability findings remain open.

### 18 — no merge capability — CLOSED 2026-08-26
Asta could review, approve and comment on a PR, all staged, and could post to a
group chat — but the last step of every piece of work was manual.

`merge_pr` stages; `ops.pr_merge` performs. What makes it more than plumbing is
that a merge is the least reversible act in the system: it puts code on the
branch everybody else builds from.

- It **refuses to offer** rather than offering with a warning. Red CI, unfinished
  CI, conflicts, a draft, or requested changes each block it, and each is NAMED —
  he needs to know what is in the way, not that something is. The offer itself
  would be the mistake, because his yes is one tap.
- Unfinished CI blocks as firmly as red CI. Merging while checks are running is
  merging on a guess.
- The offer carries the PR's **real state** — "CI green · approved" — so he is
  approving a fact rather than a hope.
- The blockers are **re-checked at the moment of merging**. The state was read
  when the offer was made and he may say yes an hour later, by which time CI can
  have gone red. The gap between deciding and doing is exactly where an
  irreversible action goes wrong.
- `--squash` by default: a merge commit per ticket makes `develop` unreadable.

Three mutations caught: merging over red CI, merging while CI runs, and dropping
the re-check.

---

### 24 — code that is complete, covered, and reachable by nobody — CLOSED 2026-08-26
Found while writing the README, by trying to document how to *use* what had been
built. Three separate instances of one failure:

- **`app/selector_health.py`** — 300 lines, its own tests, called by nothing in
  `app/`. The Teams selector check existed and never ran.
- **`app/evals.py`** — same. The answer-quality measurement written to close
  finding 14 was never invoked outside its tests.
- **`merge_pr`** advertised `POST /api/pr-merge` in its capability row, which is
  the string every CLI brain is taught verbatim. There was no such route. Also
  found: `teams_search` had named `GET /api/teams/search` since long before this
  review, and that route did not exist either.

This is worse than a missing feature, because everything *looks* right in review:
the module is there, the tests pass, the capability is declared. Only the calling
edge is absent, and nothing in a diff shows an edge that was never drawn. It is
the same failure as findings 7 and 8 — where my own tests covered the functions
but not that anything called them — repeated one level up.

**Closed by making reachability testable rather than remembered:**

- `check_teams_selectors` and `answer_quality` are now capabilities, so they are
  askable, and `POST /api/teams/selector-check` and `POST /api/evals` exist.
- `selector_health.watch_loop` runs daily under `daemon.start`, supervised like
  every other loop.
- `POST /api/pr-merge` and `GET /api/teams/search` now exist.
- **`test_every_capability_http_route_exists`** compares every capability's
  advertised endpoint against the routes FastAPI actually serves. It compares
  path *shapes*, because the docs name a parameter for a reader (`{workspace}`)
  and FastAPI names it for the function (`{name}`) — a test that called that a
  mismatch would be noise, and noise is how a check gets ignored.

The interval for the daily check is a plain constant, not an env setting. The two
knobs this module used to have were removed on Arun's objection — "I can't go and
update everytime" — and a schedule he would never tune is one more of the same.
A test enforces that no `ASTA_SELECTOR_CHECK_*` setting comes back, and it looks
for an environment *read* rather than the spelling, so the comment explaining why
there is no knob is still allowed to name it.

Four mutations caught: unstarting the loop, renaming the merge route, letting the
check's exception escape the loop, and reintroducing the env knob.

---

### 25 — a healed fault kept reporting its old cause — CLOSED 2026-08-26
Found live, in the minutes after the restart that put this review's code into
service. `attention_scrape_error:teams` held

    RuntimeError: Teams app did not load within 75s

while the Teams watcher was, at that moment, reading successfully every 60
seconds — proved three ways: `last_scrape` resetting on a ~60s cycle, health not
flagging `teams_watcher` at all, and a live selector check reporting all three
reachable selectors still matching.

`note_scrape_error` wrote the reason; nothing ever cleared it. `health.checks`
quotes that reason whenever a source goes stale, so the next unrelated stall
would have been explained with a cause that had healed hours earlier. A wrong
cause is worse than no cause, because it gets followed.

`note_scrape` now clears the error for that source — and only that source, so
Teams recovering cannot hide Outlook still being broken. Two mutations caught:
dropping the clear, and clearing every source at once.

The same rule as the rest of this register: report the current state, not the
last state anyone happened to write down.

---

## Where this leaves the review

**25 findings raised, 24 closed**, plus finding 4 recorded as Arun's accepted
risk. 1,683 tests pass. Every fix was mutation-tested — the source was
deliberately broken and the suite had to notice.

Still needing Arun rather than code:

- **`ANTHROPIC_API_KEY` is refused by the provider.** Health reports it; it
  clears itself the moment the key changes.
- **Atlassian MCP needs re-authorising.** The stale `help/jarvis` paths are
  repaired (backup in `data/oauth/atlassian.bak-2026-08-19`), so it now reaches
  the OAuth step instead of failing on a dead path. Signing in is his to do.
- **`telikos-email-service/_pins.yml` contradicts its own `lessons.md`**: the pin
  omits `clean`, the lesson says it is mandatory. Asta now retries once with
  `clean` when MapStruct's FilerException appears, but the pin is worth fixing.
