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

- **Voice clone** — flow is built and waiting on Arun: record the five takes
  (`python -m app.voice script`), then
  `python -m app.voice clone "Arun" take1.m4a …`. Voicebox must be running:
  `cd ~/help/voicebox && backend/venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 17493`
- **No version control.** `~/help/asta` is not a git repo. Everything is
  working files only; `.env.bak-*` are the sole safety net. `git init` is
  worth doing before more accumulates.
- **`ASTA_EMBED_MODEL` unset**, so recall uses the first model loaded in LM
  Studio. A dedicated embedding model would rank better.

---

## Test suites (scratchpad)

`test_openitems` 28 · `test_models` 28 · `test_conductor` 20 · `test_regress` 9
· `test_premeet` 8 · `test_learning` 21 · `test_task_pipeline` 0 failures.
All green as of 2026-07-23.
