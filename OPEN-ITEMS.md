# Open items

Last updated 2026-07-23. Everything previously listed here is now built and
verified; what remains is at the bottom.

> **Superseded for the current state.** The August 2026 architecture review and its
> findings register live in [docs/REVIEW-FINDINGS-2026-08.md](docs/REVIEW-FINDINGS-2026-08.md).
> Read that first — this file is the July record.
>
> **Needs a decision (26 Aug):** a finished task never releases its git worktree.
> `worktrees.remove` is called from exactly one place — `rollback()`, i.e. only when
> Arun explicitly says "undo". So every completed code task leaves its checkout
> behind holding its branch. Live evidence: task #69 was *rejected* and still held
> `feature/TELIKOS-123`, so tasks #70 and #71 could not create a worktree and both
> **silently fell back to the shared checkout** — the exact hazard worktrees exist
> to prevent, and the whole of "parallel tasks don't work". `task_cwd` documents
> that fallback as intentional for repos where a worktree could not be made; it
> should not apply when the reason is a finished task squatting on the branch.
> Proposed: release the worktree when a task reaches a final state, keeping it only
> when it holds uncommitted work; and when a branch is held by a *finished* task,
> take the worktree over rather than fall back. Not done — it changes what happens
> to a checkout that may hold work, so it wants his word first.
>
> Three things still need Arun rather than code:
>
> - **`ANTHROPIC_API_KEY` is refused by the provider.** Health reports it; the
>   rejection is fingerprinted, so it clears itself the moment the key changes.
> - **Atlassian MCP needs re-authorising.** Its stored OAuth state still pointed at
>   the old `help/jarvis` path — repaired (backup in `data/oauth/`), so it now reaches
>   the OAuth step. Sign in with
>   `.venv/bin/python -m app.mcp_login atlassian`.
> - **`telikos-email-service/_pins.yml` contradicts its own `lessons.md`** — the pin
>   omits `clean`, the lesson says it is mandatory. `verify._with_clean` retries once
>   when MapStruct's FilerException appears, but the pin is worth correcting.

---

## Closed on 2026-07-23

### 1. Transient alert suppression — DONE
`app/outlook.py`: `triage_alerts()` parks alert-class mail for
`ASTA_ALERT_HOLD_MINUTES` (default 20). A recovery arriving inside the window
drops BOTH halves, so a self-healing IOM alert never reaches the phone at all.
Anything still unrecovered when the window passes is released once, as
"🚨 Still broken after 20 min". Fire and recovery join on the incident/ticket id
(`INC…`, `PROJ-…`) and fall back to a normalised subject. The ledger
self-expires so it cannot grow forever.

### 2. ServiceNow + duplicate CI mail — DONE
- **CI mail suppressed.** GitHub sends run-failure mail as the actor, so the
  sender is Arun's own name and the bulk-sender list never matched it. `_CI_MAIL`
  matches the subject shape instead. `ci_watch` remains the channel for these —
  it knows the run, branch and recovery.
- **ServiceNow KEPT by default.** Incidents assigned to OH - example - L2 are
  work, not noise; hiding a live incident is the worse failure. Set
  `ASTA_SUPPRESS_SERVICENOW=1` to change that. They deliberately skip the hold
  window — the word "Incident" would otherwise make `_ALERTY` swallow them
  (found and fixed in test).

### 3. Multi-task routing — DONE
`conv_task:` (a single last-write-wins slot) became `conv_tasks:` — a list.
`tasks.live_tasks_for()` returns every live task; `live_task_for()` returns one
only when exactly one is live. With several live, `_dispatch` LISTS them and
asks; naming one ("43 also add tests", "stop 44") routes precisely and the id is
stripped from the instruction. Legacy single links are migrated on read.

### 4. Session rotation — DONE
`main.rotate_sessions()` clears `copilot_session:` / `claude_session:` and is
called from `_digest_loop` right after the episode is written — the durable
knowledge is in memory by then and `recall_block()` resurfaces it, so the raw
session has no remaining value. Phone channels also get a manual reset: "new
chat" / "fresh start" / "reset", answered locally at zero token cost.

### 5. Memory recall — DONE
FTS5 now casts a wide net and local embeddings re-rank it (`memory.recall`),
with a recency weight (`ASTA_RECALL_HALFLIFE`, 45 days) that only separates
near-ties. Degrades to exactly the old keyword behaviour when LM Studio is down.
Required an FTS schema migration to carry `date`; `memory_reindex` detects the
4-column table and rebuilds it automatically.
Episode pruning already existed (`memory.py`, newest 30 kept).

---

## Still open

- **Verifier gate — Phase 4: non-code verification (DEFERRED, revisit with data).**
  The verifier loop (`app/verify.py`, `tasks._verify_gate`, off behind `ASTA_VERIFY`)
  loops a CODE task to green against the repo's own test/typecheck command. It stops
  there on purpose: code has a **free, un-fakeable judge** (a subprocess exit code,
  zero model tokens). Non-code outputs — an answer, a summary, an analysis — have no
  such judge. Checking them needs either a rigid schema (only works for structured
  output) or an **LLM grading itself**, which costs tokens every loop AND can be
  flattered ("looks great") — the exact cheap, honest bar that makes the code loop
  worth it disappears. Building it blind also risks the worst failure: the loop
  refusing to finish a perfectly good answer because a *guessed* rule said "not good
  enough", stranding real work.
  **Why it's fine to wait:** for a chat answer, Arun is already the judge, cheaply,
  in the moment — an automated loop there burns tokens for little gain.
  **When to revisit / what would justify building it:** the convergence data from
  `quality.verify_convergence()` (avg fix-rounds-to-green) shows a *specific* class
  of non-code task that recurs often AND has a genuine, cheap, objective check
  available (a real schema/assertion — not an LLM judge). Build it only for that
  case, reusing the same gate shape: resolve a check → run it → loop/park. Do NOT
  add a general "loop on any answer" path.
  **Update (relevance gate, below):** the *measure-only* half of this is now shipped
  for chat — `relevance.judge_answer` records off-topic answers without ever looping
  or blocking. The still-deferred piece is only the *acting* on that verdict.

- **Act on the interruption numbers (MEASURE-ONLY TODAY, by design).** The
  attention ledger records whether each interruption earned its place — engaged,
  ignored or muted — and reports precision per tier. Nothing yet SUPPRESSES on that
  history beyond the deliberately conservative contacts prior (which can quiet an
  FYI but never silence a question, never re-rank breakage, and never mute someone
  whose meetings he attends). **When to revisit:** once `quality.report()` shows a
  week or two of real numbers. If P0 engagement is high and FYI engagement is near
  zero, the FYI tier can start collapsing into the brief instead of pushing. If P0
  engagement is LOW, the ranking is miscalibrated and the fix is in `attention.score`
  — not in more suppression. Same order as the relevance gate: measure, then act.

- **Browser executor — agent-browser vs Playwright (DECISION PENDING).** The
  Playwright layer Asta uses today (teams_bridge read/send, `meetings.join`) is
  *deterministic scripted* automation — fixed selectors, no LLM in the loop, so zero
  model tokens for the automation itself. agent-browser's advantage is token
  efficiency *for an LLM driving the browser*, a different job — so it does not help
  read/send/join and would regress their reliability for no token saving.
  agent-browser is the right primary for a NEW agentic-browser capability (an
  API-less task where a brain must work the UI out on the fly); build it when such a
  use case is named, keeping Playwright as the deterministic path.

## Closed on 2026-08-02

### Relevance gate — intent drift caught before it acts (off behind `ASTA_RELEVANCE`)
The failure: a passive question ("No recent one..?") made Asta spawn a repo analysis
on an unrelated project — it answered a question never asked and *acted* on it.
`app/relevance.py` closes it in three layers, each ordered by how objective its signal
is, and all feeding the `quality` scoreboard so precision is proven in data before any
fuzzy layer is trusted to block:
- **Intent-type gate (blocks).** A question → side-effecting-spawn is a structural
  mismatch (0 tokens, un-fakeable), so `guard_spawn` in `delegate_task` holds it for a
  one-line confirm. A command — even one phrased as a question — is never held.
- **Anchor drift (measures).** Provenance-stamps whether a conversation's workspace was
  *inherited* (adopted from an offer) or *explicit* (Arun picked it); a spawn into an
  inherited workspace the ask never named records `drift`. Fires even for a command —
  the wrong-TARGET half — but never blocks.
- **Semantic tier (measures).** `judge_answer` (fire-and-forget, all channels) asks
  whether the answer addressed the question. Two-stage for cost: a free word-overlap
  pre-filter settles the common case, and only a low-overlap answer spends one tiny
  local-model yes/no; local model down → skip. Records only the adjudicated
  `ontopic`/`offtopic`, so the scoreboard stays signal-dense.
Off by default; additive; no-op when the flag is unset. Next increment: promote anchor
drift / off-topic from measure to block once the numbers show the precision is there.

### Coding-brain tooling — Serena + Context7 for code tasks, and the approved plan as a durable spec (both off by default)
Two token-lean upgrades for the brains Asta spawns on code work, each behind its own
flag and a pure no-op until flipped:
- **Serena + Context7 reach the code brains** (`app/dev_mcp.py`, off behind
  `ASTA_DEV_MCP`). Serena gives symbol-level nav/edit scoped to the repo
  (`--project <cwd>`); Context7 injects version-correct docs — it was already wired
  for the chat agent via mcp.json but never reached the code brains, and that gap is
  closed. One shared policy builds the inline mcpServers JSON that both the claude
  (`--mcp-config`) and copilot (`--additional-mcp-config`) task legs pass through; a
  missing binary is skipped rather than fatal (mcp_loader's contract). Attaches to
  code + analysis legs only — never teams_draft, never chat.
- **The approved plan is kept as the definition of done** (`app/task_spec.py`, off
  behind `ASTA_TASK_SPEC`) — GSD's one good idea. Captured at the moment Arun approves
  the plan (the one unambiguous point, no mid-output marker parsing), first-approval
  wins, and re-injected into a resumed/compacted implementation leg so a worker that
  lost its context rebuilds against the same bar. Same off-by-default/additive
  contract as the verify and relevance gates.
Tests: `test_dev_mcp.py` (12, incl. e2e wiring through both brains),
`test_task_spec.py` (9). Full suite 800 passed.

### Notifications — ranked, remembered, and measured (six flags, all off by default)
The old pipeline compressed every judgement about the inbound world into two
booleans (`Verdict.action`, `notify.urgency`) spread across 34 independent push
sites. Four states, no memory, no ranking. Six additive stages:
- **`app/attention.py` (`ASTA_ATTENTION`)** — one ledger, one policy. Cross-source
  dedup joins on the incident/ticket id both channels carry verbatim, so one
  incident arriving as mail AND a Teams mention is one interruption; the general
  answer to the collision `goes_to_hold` patched by hand. Plus the freshness
  heartbeat, NOT behind the flag — both watchers swallow exceptions and continue,
  so a broken selector reads as a quiet week.
- **Ranking (same flag)** — P0..P3 from objective signals first: provable breakage,
  then a parsed clock deadline (a past one stays past — he is late), then wording,
  then the chase count only the ledger can see.
- **`app/contacts.py` (`ASTA_CONTACTS`)** — a learned sender prior replacing a
  hand-written regex, seeded from calendar co-attendance. Three refusals make it
  safe: it can quiet noise but never silence a question, never re-ranks breakage,
  and is capped at one tier.
- **Feedback (same flag)** — `quality.report()` now scores the thing that actually
  interrupts him, per tier, from labels he already produces. Recorded even while
  the prior is off, so switching it on starts with real history.
- **`app/delivery.py` (`ASTA_DELIVERY`)** — quiet hours (only breakage earns the
  night; unranked pushes always go out), coalescing, and chasing an unanswered ask
  once. Never twice: an assistant that nags gets muted.
- **`app/agenda.py` (`ASTA_MEET2`)** — closes a real functional hole: `meetings.join()`
  needed a URL nothing produced, so "join my 3pm" was unreachable. Now resolved from
  the calendar, refusing on ambiguity. Plus clash/back-to-back warnings, per-type
  lead, and attendance-need (advisory only — it quiets a ping, never suppresses one).
Tests: `test_attention` 29 · `test_priority` 37 · `test_attention_feedback` 16 ·
`test_contacts` 20 · `test_delivery_policy` 30 · `test_agenda` 40 ·
`test_attention_integration` 14 (all six flags on together, plus schema migration).
Full suite 985 passed.

- **Voice clone** — flow is built and waiting on Arun: record the five takes
  (`python -m app.voice script`), then
  `python -m app.voice clone "Arun" take1.m4a …`. Voicebox must be running:
  `cd ~/help/voicebox && backend/venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 17493`
- ~~**No version control.**~~ DONE — `~/help/asta` is a git repo on
  `feature/agentic-loop`, pushed to `Arunk02/asta` PR #11 (personal account).
- **`ASTA_EMBED_MODEL` unset**, so recall uses the first model loaded in LM
  Studio. A dedicated embedding model would rank better.

---

## Test suites (scratchpad)

`test_openitems` 28 · `test_models` 28 · `test_conductor` 20 · `test_regress` 9
· `test_premeet` 8 · `test_learning` 21 · `test_task_pipeline` 0 failures.
All green as of 2026-07-23.
