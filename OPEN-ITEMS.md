# Open items

Last updated 2026-07-23. Everything previously listed here is now built and
verified; what remains is at the bottom.

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
