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
| 26 | High | Temporal cert checked for presence, not validity — an empty file passes and dies inside TLS | `temporal-mcp-proxy.py`, health | **closed** |
| 27 | **Critical** | The always-available core could be evicted from the toolset — including `prepare_to_send`, the staged-send gate | `tool_index._recent` | **closed** |
| 28 | Medium | The local model is whichever LM Studio lists first — an embedding model would answer every turn with silence | `agent._lmstudio_model_id` | **closed** |
| 29 | High | The debugging eval cases were vacuous — parroting the playbook scored 6/8 | `data/evals/debugging.json` | **closed** |
| 30 | High | No Temporal knowledge source existed — the env/namespace/cert map lived only in the proxy, unreadable by any brain | `skills/` | **closed** |
| 31 | Medium | A blank setting in `.env` silently disabled a whole module | `diagnostics.TEMPORAL_PROXY` | **closed** |
| 32 | **Critical** | A stopped turn could not say whether the work finished, was still running, or was wedged — and threw away everything it had done | `copilot_cli`, `claude_cli` | **closed** |
| 33 | High | The code-task ceiling (30 min) sat BELOW the measured p90 (32 min), killing work that was going to succeed | `tasks.TASK_TIMEOUT` | **closed** |
| 34 | **Critical** | A background daemon could replace the offer he was answering — his "yes" reached a question he never read | `offers.offer` | **closed** |
| 35 | High | A read-only question waited behind a running code task | `main._dispatch`, `activity` | **closed** |
| 36 | **Critical** | A workspace that is itself a repo reported ONE repo — the generated-context one — so worktrees, rollback and budget all targeted the wrong tree | `worktrees.repos_in` | **closed** |
| 37 | Medium | A code budget flat across every task, regardless of how many repos it can touch | `tasks.code_timeout` | **closed** |
| 38 | High | Every code task prepared every repo in the workspace, so a one-line change cost three fetches and three checkouts | `worktrees.create` | **closed** |
| 39 | High | A watcher repeating itself filled the bounded offer queue and evicted real offers | `offers.offer` | **closed** |
| 40 | **Critical** | Two MORE inline copies of the repo-discovery rule, one of them deciding where PRs get raised | `tasks.py` | **closed** |
| 41 | High | A read-only side turn could still spawn a CODE task — the guard that would catch it is behind a flag that is off | `agent.delegate_task` | **closed** |
| 42 | Medium | Code questions went to a 9B local model before the CLI subscriptions already paid for | `call_brain` | **closed** |
| 43 | High | The documented cert command truncates its target before fetching, so a failed fetch leaves a 0-byte cert | `deploy/fetch-temporal-cert.sh` | **closed** |

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

### 26 — a cert that exists and cannot be used — CLOSED 2026-08-26
`~/.config/temporal-mcp/preprod.pem` and `preprod.key` are both **0 bytes**. The
proxy gates on `os.path.exists`, which an empty file passes, so the failure
surfaces from inside TLS as

    failed loading client cert key pair: tls: failed to find any PEM data in
    certificate input

— a sentence about PEM parsing that never mentions the empty file. Debugging
preprod would fail with an error pointing at the wrong thing.

`app/diagnostics.py` validates instead of checking presence: missing, empty,
unparseable, expired, expiring, ok — and reads the env map **from the proxy**
rather than restating it, because two copies of a mapping is how a newly added
env goes missing in one of them.

Health reports only *broken* certs — present and unusable. Four of the seven
envs have no cert at all because Arun does not use them; reporting those every
pass is how a health report becomes something people scroll past, which is the
same correction the Teams selector check needed when it called unchecked
selectors BROKEN.

Live result: **3/7 usable** (sit, uat, prod — 260 and 304 days left); dev, qa,
perf never configured; preprod broken. Four mutations caught.

### 27 — the send gate could be evicted — CLOSED 2026-08-26
The worst finding of the session, and it was latent.

`_recent` built the sticky toolset as `picked + prev + floor` and then kept the
first N. The floor — `capabilities.ALWAYS` — was appended **last**, so the one
group whose own comment said "never evicted" was the first group evicted. It
never fired while the registry was small enough that nothing trimmed; adding
three capabilities crossed the threshold and a test caught it.

What it would have cost: the floor is `ask_user`, `continue_working`,
`load_skill`, `remember`, `delegate_task`, `search_memory`,
`list_background_tasks` — and **`prepare_to_send`**. A long enough conversation
would have dropped the staged-send gate, leaving the single hard rule in this
system — nothing leaves the machine unapproved — enforced by a tool the model
could no longer reach.

Now this turn's picks and the floor are kept whatever the cap says, and only
carried-over tools compete for the remaining room. Two mutations caught,
including restoring the original trim.

### 28 — the local brain was whichever model sorted first — CLOSED 2026-08-26
`_lmstudio_model_id` returned `data[0]["id"]`. Arun has five models loaded and
one of them is `text-embedding-nomic-embed-text-v1.5`; the day that sorted first,
every local completion returns empty — and empty is indistinguishable here from
"the brain had nothing to say", so it reports as Asta not knowing.

The same shape as the refused API key: something was available, so it was used,
and nobody checked it was the right thing.

Now non-chat models are excluded, and `ASTA_LOCAL_MODEL` pins one — worth setting,
because the choice is not cosmetic. Measured on this machine, same question:

    google/gemma-4-e4b     15.9 s
    qwen/qwen3.5-9b        38.9 s   (the difference is reasoning tokens)

A pin naming an unloaded model falls through rather than disabling every local
call. Two mutations caught.

### 29 — the debugging evals measured the prompt, not the answer — CLOSED 2026-08-26
The first debugging suite scored **8/8 against a real brain**. It also scored
**6/8 against an asker that returned the playbook verbatim and reasoned about
nothing** — because the playbook is in the prompt, so any token it contains is
free. The score was real and meant almost nothing.

This is the eval equivalent of a test that passes with the feature deleted, and
it is exactly what finding 14 was supposed to prevent — caught only because the
suite was checked against a deliberately stupid answerer, the same way every fix
in this register is checked against a deliberately broken source.

Rewritten so each case needs a value that appears only in the question (an
identifier), or a **choice** between things the playbook lists — naming one env's
namespace while not naming the others, which a recital fails by definition. Four
degenerate answerers (parrot, silent, waffle, shotgun) now score **0/8**, and
`test_debugging_evals_are_not_vacuous` keeps it that way. A companion test proves
the guard can itself fail, so it is not decoration.

---

### 30 — Asta had no way to know anything about Temporal — CLOSED 2026-08-26
Found by measuring rather than by reading. The first debugging eval run scored
5/8, and two of the three failures were Temporal questions: *which namespace does
sit use*, *which cert does uat use*. Not reasoning failures — Asta had no source
to reason from. `grafana-analyser` existed; there was no Temporal equivalent, and
the env→namespace→cert mapping lived only inside `temporal-mcp-proxy.py`, where
no brain can read it.

`skills/temporal-analyser.md` closes it: tool order (`list_workflows` first,
`workflow_history` last and rarely), reading pending activities before history,
correlating a workflow id into Loki as a line filter — and the env table, which is
**generated from the proxy's own ENV_MAP**, not copied from it.

The generator lives in `diagnostics.write_temporal_skill` and runs at startup, so
the playbook is rebuilt from the mapping every boot and cannot be older than the
code. The file itself stays gitignored, like `grafana-analyser.md` beside it —
that one is a symlink into the booking-service repo, and both quote internal
namespaces that have no business in this repository. Committing the generator and
not the output is what makes the drift question answerable rather than a promise:
a hand-written table is correct on the day it is written, and `_pins.yml`
contradicting its own `lessons.md` is what the other outcome looks like.

The two traps are stated explicitly, since both produce a wrong answer that looks
right: the Temporal namespace is not the Grafana namespace (`telikos-sit-cdt` vs
`telikos-sit`), and `preprod` runs in `telikos-spt-cdt`.

Measured effect: **5/8 → 6/8, and 65s → 17s per question** — the second number
being the larger point. The brain was previously hunting for something it had no
way to find.

### 31 — a blank line in .env switched off a module — CLOSED 2026-08-26
Self-inflicted, while documenting `ASTA_TEMPORAL_PROXY` as the `.env` test
requires. The line was added blank, and `os.environ.get(key, default)` uses its
default only when the key is **absent** — a key present and empty gave `Path("")`,
which does not exist, so every cert check returned "nothing to check". A module
disabled by a blank line, reporting a healthy silence.

Now `os.environ.get(...) or default`, with a test. Worth noting how ordinary this
is: documenting a setting is supposed to be the safe act.

---

### 32 — "timed out after 300s" answered none of the questions — CLOSED 2026-08-26
Raised by Arun from a Teams screenshot: Asta implementing a VTS ETA validation,
narrating a dozen real steps, then

    RuntimeError: Copilot CLI turn timed out after 300s

His objection is the right one: *"now it not making sense whether it actually
completed does it doing or struck."* True, and useless.

Two defects, compounding.

**The work was thrown away.** Both drivers accumulate every chunk the brain
streams — every file it opened, every edit it narrated — and the timeout branch
discarded all of it to raise one sentence. The evidence existed, in memory, and
the error path deleted it. There was no way to answer "did it do anything?"
except to go and read the repo.

**The budget was total elapsed time, never silence.** A brain streaming progress
every few seconds and a brain wedged since second three were killed at the same
moment with the same words. Those are opposite situations: one needs more time,
the other needs stopping. Nothing ever looked at *when output last arrived*, so
the system could not tell them apart even in principle.

`app/turn_budget.py` names three outcomes instead of one:

    done     the brain finished
    idle     silent for ASTA_TURN_IDLE (120s) — wedged; more time will not help
    ceiling  still producing output when the budget ran out — a long job, and
             resuming continues it where retrying starts from nothing

The partial output travels with the error, so the report says what it got
through. One module for both brains rather than a copy each: the two pump loops
were byte-identical and had drifted anyway, and `claude_cli` parses NDJSON so it
reports liveness through a `Heartbeat` while the policy stays in one place.

The abandoned pump is cancelled, because a brain still editing files behind an
answer Arun has already read is how the next turn finds a repo that moved.

Four mutations caught: reporting idle as ceiling, discarding the chunks, leaving
the pump running, and lowering the ceiling back under p90. The cancellation test
was itself vacuous first time — `asyncio.run` tore the loop down and killed the
stray task regardless, so it passed either way. Rewritten to wait inside the same
loop.

### 33 — the code ceiling sat below the measured p90 — CLOSED 2026-08-26
`TASK_TIMEOUT["code"]` was 1800s. The baseline measured at the top of this
register: **median 7.7 min, p90 32 min (n=46)**. Thirty minutes is *below* the
p90, so roughly the slowest tenth of code tasks were killed by their own budget —
work that was going to succeed, re-run from nothing, paying the whole cost twice.

Raised to 2700s (45 min), which clears the measured p90 with room. Safe to raise
precisely because finding 32 landed first: a wedged brain is now caught after two
minutes of silence regardless of how much ceiling remains, so the ceiling no
longer has to double as a liveness check. That was the job it was doing badly.

---

### 34 — his yes reached a question he had never read — CLOSED 2026-08-26
From a WhatsApp transcript. Asta staged a Teams call to Vinish; Arun replied "Go
ahead", then "Yes go ahead and call Vinish", then "Yes go ahead". Nothing rang.
The brain eventually concluded approval must live "through a separate
confirmation channel" — a reasonable inference from what it could see, and wrong.

`offer()` wrote to one global slot and every new offer overwrote it. Four
background daemons stage offers — `refresh`, `ci_watch`, and two in `meetings` —
so a staleness proposal took the slot between the call being staged and him
answering. His yes reached a question he had never read; the brain re-staged; the
daemon clobbered it again; the loop repeated.

The docstring defending the single slot was right about the *property* — two open
questions plus a bare "yes" is ambiguous — and wrong about the implementation.
One is still ASKED at a time; later ones queue behind it. The head is immutable
while unanswered, so the thing he approves is the thing he was shown.

The dangerous version is the quiet one: the same race could have had his yes
accept an outward write that replaced the one on screen.

One consequence needed handling. Every producer pushes `o.render()` the moment it
stages, so a queued offer would still have *asked* him a question his yes would
not answer — reintroducing the ambiguity from the other side. `render()` decides
its own wording from whether it is the asked one, which makes all four producers
correct without being touched.

Four mutations caught, including restoring the clobbering. A pre-existing test
asserted the old behaviour — kept its intent, changed what it verifies.

### 35 — a question waited behind a forty-minute implementation — CLOSED 2026-08-26
Same transcript: *"What is the ci status of above PR"* → **"still finishing the
previous one — I'll answer this right after."** Reading a PR's checks does not
conflict with writing code. The serialisation was protecting the conversation,
not the repo.

`activity` gains an `independent` verdict for read-shaped asks carrying no write
verb, and `main` answers those in a concurrent side turn bounded by
`ASTA_SIDE_TURNS_MAX`.

**The safety is in the toolset, not the classifier.** A side turn runs with
`capabilities.READ_ONLY_TURN` set, so it cannot reach any of the 17 write
capabilities — the worst a misclassified message can do is read something and
answer. A ContextVar, because asyncio copies the context when a task is created:
it applies to that turn and everything it awaits, and the implementation already
running is unaffected. A test pins exactly that, since a leak would strip write
tools from work in flight.

`independent` can only ever narrow what would have been `ambiguous`, so nothing
that used to augment or redirect now runs concurrently instead.

### 36 — the workspace reported the wrong repos — CLOSED 2026-08-26
Found while sizing the budget in finding 37, and much worse than the thing that
found it.

`~/booking-workspace` is itself a git repo — `Arunk540/booking-workspace`,
tracking the 234 generated files under `.contmark/` — with the three service
repos inside it as ordinary directories. `repos_in` returned `[root]` the moment
the root had a `.git`, so it reported **one** repo, and that repo was the
generated context rather than any code.

Everything downstream inherited it:

- **worktrees** cut a worktree of the context repo, so the parallel-task isolation
  added earlier in this register was isolating the wrong tree;
- **rollback** (`tasks._repos_under`, a second copy of the same rule with the same
  bug) looked for a task's changes where they were never written;
- the new scope-based budget sized a three-repo job as a one-repo job.

None of it failed loudly, because a wrong repo still exists and still answers git
commands. Inner repos now win over the root; `all_repos_in` keeps the superset for
rollback, where a change written to the root still needs undoing; and
`_repos_under` delegates instead of restating, so the two cannot drift again.

### 37 — one budget for every code task — CLOSED 2026-08-26
Arun's point: *"even small changes getting affected in multiple repos take more
time right does it make sense?"* It does not. Verification is per-repo — `mvn
clean test` on each — so a change landing across three repos pays it three times,
while a one-line fix in one repo does not need the same hour.

`code_timeout` is base + per-repo, capped: 35 min for one repo (clearing the
measured p90 of 32), 65 for three, 90 max. Scope rather than difficulty, because
scope is a fact available before the work starts and difficulty is not.

Safe only because finding 32 landed first: a wedged brain is caught by two
minutes of silence regardless of remaining ceiling, so the ceiling no longer has
to double as a liveness check.

---

### 38 — every task prepared every repo — CLOSED 2026-08-26
Arun's point: *"if u working on two parallel tasks that doesn't conflict, u may
work on two diff stuffs so dont have to worry right?"* Correct, and the code did
not reflect it.

`worktrees.create` prepared every repo in the workspace. That was nearly free
while the workspace mis-reported itself as one repo (finding 36) — and the moment
that was fixed it became **three `git fetch`es and three checkouts for a one-line
change in one service**. A regression introduced by a fix, an hour after the fix.

`repos_for` scopes preparation to the repos the task actually names, matching on
the distinctive part of a repo name — `telikos-booking-service` is "booking" far
more often than its full name — while ignoring the generic halves (`service`,
`telikos`) that every repo in the workspace shares and that would therefore match
all of them while looking like it worked.

**It falls back to preparing everything, and that direction is the design.**
Preparing a repo that turns out unnecessary costs a fetch; failing to prepare one
the task then needs costs the task. A wrong guess must fail towards more work.

### 39 — a watcher repeating itself evicted real offers — CLOSED 2026-08-26
The other half of Arun's point: *"even if u context refresh once u first big task
done, second one that doesn't required."* A proposal already made does not need
making again.

The queue added in finding 34 was bounded but had no dedup. Watchers re-detect
the same state every pass — a stale context is still stale five minutes later —
so the queue would fill with restatements of one thing and evict the offers that
genuinely differ. Which is precisely the failure a bounded queue exists to
prevent, arriving by another route.

Two rules now, both comparing what an offer would DO rather than how it is worded:

- **Re-proposing the question already on screen is a no-op.** This is the loop
  from the transcript directly: his yes was not reaching the staged call, so the
  brain staged it again every turn. Each of those would have become a queue entry
  and changed the id he was shown underneath him.
- **A duplicate already waiting is replaced, not stacked** — replaced rather than
  skipped, because the newer one carries fresher context ("21 days" rather than
  "14") and it is the same question either way.

A staged call is compared on the recorded op name and arguments, so two calls to
the same person are one question and a call to someone else is not.

### 40 — two more copies of the repo rule, and the worst one shipped — CLOSED 2026-08-26
Finding 36 fixed `worktrees.repos_in` and `tasks._repos_under`. There were **three
more inline copies** in `tasks.py`, all carrying the identical bug.

One of them decides where PRs are raised. With the old shortcut, shipping a
finished code task looked for the task's branch in the generated-context repo and
would have **raised no PR at all** for the services the work was actually in — a
task reporting success having delivered nothing.

All of them now delegate, and a test asserts the rule appears exactly once:
`iterdir() if (p / ".git")` must not appear in `tasks.py`. Five copies of one rule
is not a coincidence, it is what happens when a rule is easy to restate — so the
test forbids restating it rather than trusting the next reader to notice.

Five mutations caught across findings 38-40, including restoring the buggy rule in
the PR path.

---

### 41 — the read-only guarantee had a hole — CLOSED 2026-08-26
Found by validating finding 35 rather than trusting it.

The claim was: a side turn cannot write, so a misclassified question can at worst
read something and answer. The enforcement was `write=True` on the capability
table — and `delegate_task` is `write=False`, correctly, because it sends nothing
outward. It also spawns a worker that edits repos.

So a question answered alongside running work could have started a code task.

`relevance.guard_spawn` exists for exactly this shape — "a question is not a
request to go do work" — and it is behind `ASTA_RELEVANCE`, which is **off**, so
today it returns None for every spawn. A safety property must not depend on an
opt-in flag being set; that is a property that is true on the machine where the
flag happens to be on.

`delegate_task` now refuses `kind="code"` when `READ_ONLY_TURN` is set, and says
why. Analysis is still allowed: it is read-only, and a question that wants a
deeper look is a reasonable thing to answer with one.

**Two of the tests written for this validation pass were themselves vacuous**, and
mutation testing is what said so — one asserted a shortcut that the fallback
already covered, the other checked that protected tools survived a tight cap
without ever checking the cap still bound. Both rewritten to state what they
actually verify. That is the same failure as finding 29, caught the same way.

---

### 42 — the weakest brain was answering first — CLOSED 2026-08-26
Arun: *"ignore api key, use cli as well always either copilot or claude cli."*

The order was in-process model first, CLI second. With the hosted key refused and
staying refused, `best_model_name()` resolves to **`local`** on this machine — so
code questions were going to a 9B model in LM Studio, measured at 15.9s (gemma)
to 38.9s (qwen) for a single lookup, and weaker on exactly the questions worth
asking. Two CLI subscriptions he already pays for sat behind it.

CLI first now (`ASTA_CLI_FIRST`, on by default). In-process models stay as the
last resort, which is the case that fallback was written for: both CLIs down or
out of quota.

A pre-existing test broke, and deserved to. It grepped `answer_from_knowledge`'s
source for the string `"claude_cli"`, so it failed the moment the loop moved into
a helper — and would equally have passed if that loop were dead code. Replaced
with a behavioural test: a CLI is actually reached, and the in-process failure is
recorded rather than swallowed.

### 43 — the documented fix was how the cert broke — CLOSED 2026-08-26
Finding 26 found `preprod.pem` and `preprod.key` at **0 bytes**. This is how they
got that way, and it is worth stating because the instruction is the one printed
by the tool itself:

    vault kv get ... -field=TEMPORAL_CERT_PEM | ... > ~/.config/temporal-mcp/preprod.pem

The shell creates and **truncates the redirect target before `vault` runs**. An
expired token, a wrong path, or no VPN therefore leaves a 0-byte file behind —
which passes every `os.path.exists` check and surfaces much later from inside TLS
as "failed to find any PEM data". The documented remedy manufactures the exact
failure state finding 26 had to diagnose.

`deploy/fetch-temporal-cert.sh` writes to a temp file, checks it is non-empty,
checks it *parses* as the right kind of object, and only then moves it into
place. A failed fetch leaves whatever was there before untouched, and says which
of the three usual causes to look at.

It reads the env → cert-name → vault-path mapping **from the proxy** rather than
restating it, for the same reason the Temporal playbook is generated: a second
copy is how a newly added env goes missing in one of them.

Verified refusing cleanly with no `VAULT_ADDR` set, leaving the existing 0-byte
files exactly as they were rather than making them worse.

---

## Where this leaves the review

**43 findings raised, 42 closed**, plus finding 4 recorded as Arun's accepted
risk. 1,766 tests pass. Every fix was mutation-tested — the source was
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
