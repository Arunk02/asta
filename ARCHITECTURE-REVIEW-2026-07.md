# Asta (Asta) — architecture review & roadmap to a real end-to-end dev assistant

Reviewed 2026-07-23 against `odysseus-dev/odysseus` @ `dev` (83.6k stars, created
2026-05-31, AGPL-3.0, ~1,350 files).

Scope of my read: all of `app/` (8,543 LOC across 29 modules), `ui/`, `memory/`,
`mcp.json`, `pyproject.toml`, `GUIDE.md`, `OPEN-ITEMS.md`; on the Odysseus side
`specs/architecture-runtime-inventory.md`, `ROADMAP.md`, and the source of
`agent_loop.py`, `tool_index.py`, `teacher_escalation.py`, `context_compactor.py`,
`context_budget.py`, `interactive_gate.py`, `bg_monitor.py`, `agent_runs.py`,
`event_bus.py`, `action_intents.py`, `tool_policy.py`, `prompt_security.py`,
`deep_research.py`, `task_scheduler.py`, `visual_report.py`,
`services/memory/skills.py`, `services/memory/skill_extractor.py`,
`companion/`, `integrations/claude/skills/odysseus/SKILL.md`.

---

## 1. Verdict up front

**Odysseus is not "much better" than Asta. It is much *bigger*, and it is a
different product.** Odysseus is a self-hosted ChatGPT-workspace competitor —
chat, docs, email, gallery, notes, model serving — built for thousands of
strangers on unknown hardware. Asta is a single-operator orchestration layer
wired into one engineer's actual working life: Maersk SSO, Jira, contmark
workspaces, Copilot/Claude CLI quotas, Teams, Outlook, CI.

On the axis you actually care about — *getting real engineering work done
end-to-end* — Asta is ahead of Odysseus today. Odysseus's "agent" is a homegrown
loop that asks a raw LLM endpoint to emit fenced tool blocks. Asta delegates to
Copilot CLI and Claude Code, which are production coding agents with their own
tool stacks, and wraps them in something Odysseus has nothing equivalent to: a
staged pipeline with a context gate, a plan gate, a ship gate, per-stage effort
ladders, cross-repo handoff, quota failover, and a token-waste auditor.

What Odysseus genuinely has that you don't is **an agent runtime with
self-improvement machinery** — RAG tool selection, auto-extracted skills,
teacher escalation, detached runs, prompt-injection hardening, one scheduler
instead of nine loops. That machinery is what "more agentic" actually means, and
it is worth stealing precisely and selectively.

Two things in this repo are not acceptable for a system this valuable, and I'd
fix them before adding a single feature:

1. **No version control.** 8,543 lines of live orchestration code that drives
   `git push` and Teams sends, with `.env.bak-*` as the only safety net.
2. **No prompt-injection boundary.** Asta reads Outlook mail, Teams messages and
   Jira comments — all attacker-writable — and feeds them into CLI agents
   launched with `--allow-all-tools --allow-all-paths` and repo write access.

---

## 2. Where Asta is genuinely ahead of Odysseus

Worth stating clearly so you don't rewrite your strengths away.

| Capability | Asta | Odysseus |
|---|---|---|
| Real coding execution | Delegates to Copilot CLI / Claude Code — full agentic coding tools, sessions, resume | Homegrown fenced-block tool loop over a raw endpoint |
| Staged approval | Context gate → plan gate → implement → ship gate, each notified | Plan mode exists; no staged gates, no ship discipline |
| Cost control | `token_audit.py` (waste categories, grade, trend), `traces` table, effort ladder, deferred MCP schemas, zero-token deterministic paths (`activity.py`, `briefing.py`) | Roadmap item: "Agent prompt/context bloat… too heavy" — unsolved |
| Enterprise reality | Jira REST, Teams/Outlook via the live SSO browser session, ServiceNow triage, alert hold windows, CI watch across 6 repos | Generic IMAP/SMTP + CalDAV — useless inside a Maersk tenant |
| Codebase grounding | contmark `resolve_context` returns exact files/lines before any read | Plain RAG over uploads |
| Multi-channel presence | Web UI, WhatsApp, Telegram, voice, push notifications | Web UI + ntfy |
| Failure-mode knowledge | GUIDE.md's symptom→fix tables and 10 gotchas are better ops docs than Odysseus has | README + setup guide |

Odysseus's own ROADMAP.md opens with "I don't know what I'm doing, help" and
lists "SQUASH BUGS" as high priority. Its architecture spec flags
`tool_implementations.py` at 4,032 lines, `agent_loop.py` at 2,961, 102 importers
on `core/database.py`, and 31 backwards imports from domain into HTTP layer. Do
not treat that codebase as a model of engineering; treat it as a **parts bin**.

---

## 3. The twelve things worth taking from Odysseus

Ranked by (value to your end goal) ÷ (effort).

### T1 — RAG tool selection `src/tool_index.py` ★★★★★
Odysseus embeds every tool description and retrieves only the top-K per message,
with a deliberately tiny always-available set (`manage_memory`, `ask_user`,
`update_plan`). You inject all 33 tool schemas plus a 70-line persona plus the
memory index plus the skills index on **every** turn. That is your single biggest
fixed cost and it gets worse with every tool you add — and you plan to add many.
You already have local embeddings (`memory.local_embed`) and a recall ranker;
this is the same machinery pointed at tool descriptions.

### T2 — Skills that are auto-extracted and structured ★★★★★
`services/memory/skill_extractor.py`: after any run that took ≥2 rounds or ≥2
tool calls, an LLM distils it into a skill (`title`/`problem`/`solution`/`steps`/
`tags`/`confidence`), stored as `SKILL.md` with a structured body (When to Use /
Procedure / Pitfalls / Verification), usage counters in a sidecar, and eviction
below confidence 0.6.

**This is the finding I'd underline.** After five days of heavy use you have
**one fact file and one skill file** against 18 episodes. `learn_from_correction`
is good design and only landed on 2026-07-23, but the durable-learning loop is
currently producing almost nothing. Your assistant is not compounding. Episodes
are prose digests capped at 30 — they are diary entries, not procedures. The
lessons that actually matter (the `mvn clean` MapStruct rule, `--allow-all-paths`
vs `--allow-all-tools`, "Jira ACs hide in comments", "boot.sh in one call",
"CLI echoes the prompt so trust the last line") all live in *my* memory file and
in your head, not in Asta's.

### T3 — Prompt-injection boundary `src/prompt_security.py` ★★★★★
Guard markers around untrusted blocks, marker-escaping so an attacker can't close
the sandbox early, and a policy line that outranks the persona. Asta pipes mail
bodies, Teams messages and Jira comments straight into an agent with repo write
and shell. A crafted Jira comment on a ticket you ask Asta to work on is a
straight path to a commit. Non-negotiable.

### T4 — Expose Asta's own capabilities as an MCP server / scoped API ★★★★★
Odysseus ships `integrations/claude/skills/odysseus/` + a scoped `/api/codex/*`
surface + `odysseus_api.py` so Claude Code talks to it natively.

You currently teach capabilities **three separate ways**: PydanticAI tool
functions (`agent.py`), curl instructions embedded in Copilot's
`_first_turn_context`, and `--append-system-prompt` for Claude CLI. Three sources
of truth, and your own memory records the tax: *"changing `_first_turn_context`
requires clearing `copilot_session:*` kv rows."* The regex pre-fetch hacks
(`_teams_activity_context`, `_outlook_context`) exist purely because the CLI
brain can't reliably shell back into you.

Fix: **one MCP server exposing Asta's tools**, consumed by Copilot CLI, Claude
Code, and PydanticAI alike. The three teaching paths collapse to one, the regex
pre-fetchers die, and every future tool is written once.

### T5 — One scheduler instead of nine loops ★★★★☆
`src/task_scheduler.py` + `src/event_bus.py`: cron-shaped scheduled tasks, event
triggers with thresholds, and a singleflight TTL cache so tasks firing in the
same minute share fetches. Your `startup()` hand-rolls loops for digest, Jira
watch, msnotify, premeet, briefing scheduler, CI, health, refresh, Outlook and
Teams activity. Each has its own guard, backoff and failure mode. One scheduler
table + one runner makes "watch X and tell me" a row, not a module.

### T6 — Interactive gate `src/interactive_gate.py` ★★★★☆
Background work waits until UI traffic has been quiet for N ms, with a browser
heartbeat. This is *literally your stated rule* ("quiet while he's at the
laptop") implemented properly, ~120 lines.

### T7 — Detached agent runs `src/agent_runs.py` ★★★★☆
Server-side replay buffer per run; SSE clients subscribe, replay, then stream
live. Closing the tab drops a subscriber, not the run. Your WS chat streams to
whoever is connected — close the phone PWA mid-turn and you lose the stream.
Given you drive this from a phone over Tailscale, this matters more for you than
for them.

### T8 — Teacher escalation `src/teacher_escalation.py` ★★★★☆
Detect failure (tier-1 regex over tool output and reply), escalate to a stronger
model, **and have the teacher write the SKILL.md so the cheap model succeeds
alone next time.** You already have the escalation half — micro tier emits
`ESCALATE:` and `tasks.py` flips to the full pipeline. You are missing the
learning half: an escalation today teaches nothing about tomorrow.

### T9 — Deep research `src/deep_research.py` + `visual_report.py` ★★★☆☆
IterResearch-style plan → search → extract → synthesise loop, with a
date-grounding preamble (so the model stops emitting "2025" queries), low-quality
detection, and a self-contained styled HTML report. You have **no research
capability at all** — no web search, no report generation. For "analysis, review"
this is a real hole: incident write-ups, dependency/CVE checks, "how do other
teams solve X", design-option comparisons.

### T10 — Adaptive context budget + structured compaction ★★★☆☆
`context_budget.py` derives the budget from the model's real context window;
`context_compactor.py` compacts at 85% with a Cursor-style *structured* summary.
Yours compacts at a fixed 30 messages into free prose via the local model. Fixed
thresholds mis-serve both a 4k local model and a 200k Claude.

### T11 — `ask_user` and `update_plan` as always-available tools ★★★☆☆
Structured mid-run clarification and plan write-back. Asta's gates stop the whole
pipeline and wait for approval — good for a plan, heavy for "which of these two
repos did you mean?". A cheap `ask_user` that pushes one question to WhatsApp and
resumes on reply would cut the stop/restart cost you measured (+26 calls,
+500k tokens for a re-plan cycle).

### T12 — Tests in the repo ★★★★☆
583 test files, ~54,800 lines, checked in and runnable by contributors. You have
seven suites (`test_openitems` 28, `test_models` 28, `test_conductor` 20,
`test_regress` 9, `test_premeet` 8, `test_learning` 21, `test_task_pipeline`) —
all green, all living in a **session scratchpad** that is not the project and not
backed up. They are your regression net for a system that pushes commits. Move
them in.

### Deliberately not worth copying
Cookbook / hardware-fit model serving (you use hosted CLIs), gallery + image
editor, document/notes/CalDAV suites, themes, blind model Compare, 2FA and
multi-user auth, Docker (your memory already records why Docker is wrong on this
Mac), and above all **the shape** — 54 flat route files, a 4,032-line tool module,
a 36,653-line stylesheet.

---

## 4. Honest critique of Asta as it stands

**Risks**

- **No git.** Top of the list. `git init`, commit, push to a private remote.
- **No injection boundary** (T3).
- **Secrets in plaintext `.env`** — Anthropic, OpenAI, Gemini, Jira, GitHub PAT,
  Telegram — on a box reachable from your phone, behind one static bearer token
  (`ASTA_TOKEN`) with no expiry or scoping. Odysseus's answer is a vault plus
  scoped, mintable tokens; you need at least scoping and rotation.
- **Tests outside the project** (T12).

**Design debt**

- **`missions.py` and `tasks.py` are the same engine twice.** Both do
  plan → approve → headless implement → verify → ship, both track sessions and
  executors, both notify. `missions.py` (382 lines) is the older, weaker one;
  `tasks.py` (829 lines) has the context gate, micro/full routing, multi-task
  routing, handoff hops and audit. Two entry points to one concept means two
  places to fix every bug, and the agent has to choose between
  `create_mission` and `delegate_task` on every request. **Collapse into one.**
- **Triple capability teaching** (T4).
- **`main.py` at 1,247 lines** holding auth, nine background loops, ~45 REST
  endpoints, the WS chat loop, sinks and quota fallback. This is exactly the
  monolith Odysseus wrote a 412-line spec to escape — with the difference that
  you can still fix it cheaply.
- **The 70-line persona is a monolithic constant.** It carries hard rules
  (Teams 1:1, no AI attribution in commits, Jira write confirmation) mixed with
  tool tutorials that RAG tool selection should deliver on demand.
- **Regex pre-fetchers** (`_teams_activity_context`, `_outlook_context`) — a
  symptom of T4, and they only fire on phrasings you anticipated.
- **`app/store.py` has no migration story** — schema is `CREATE TABLE IF NOT
  EXISTS` plus one ad-hoc FTS rebuild. You already hit this once when `date`
  had to be added to the FTS schema.

---

## 5. Gap analysis against the stated end goal

> "end-to-end AI assistant developer with enormous experience: coding, calls,
> meetings handling, analysis, review, meeting draft, reply persons"

| Goal | Today | Gap |
|---|---|---|
| **Coding** | Strong. Micro/full pipelines, gates, handoff, ship, CI watch | No *review* of others' code; no test-first mode; no repo-level refactor mode |
| **Code review** | **Absent** | No "review PR #123" flow. You have `gh` + CI + contmark context — this is close to free and it's the highest-leverage missing dev capability |
| **Calls** | **Absent** | No audio capture, no live transcription. Voicebox already gives you Whisper — what's missing is system-audio capture and diarisation |
| **Meetings handling** | Read-only list of today's meetings + a pre-meet nudge | No prep pack (who/what/last thread/related Jira), no notes, no recap, no action-item extraction → Jira/reminders |
| **Analysis** | Strong for *your* stack (Grafana/Temporal/Loki via MCP + skill discipline) | No web research; no cross-incident trend analysis |
| **Review (non-code)** | Absent | No document/design-review pass |
| **Meeting draft / replies** | `teams_draft` with approval | Outlook is read-only — no reply drafting, no send. Jira comment drafting isn't a pipeline. No per-person tone/history model |
| **Reply persons** | Teams 1:1 send with a hard rule | No **people model**: who Vinish is, what you owe him, what you last promised, what's outstanding |

Two structural gaps stand out beyond individual features:

- **No people/relationship memory.** Every one of "calls, meetings, replies,
  review" is about *people*. There is no contacts table, no per-person thread
  history, no open-commitment tracking. This is the backbone of the assistant you
  described, and nothing in the current design carries it. (Odysseus has
  `contacts_routes.py` + `do_resolve_contact`; the idea is right even if their
  implementation is generic.)
- **No self-evaluation.** `token_audit.py` measures *cost*. Nothing measures
  *quality* — did the plan hold, did the PR pass review, did the draft get sent
  unedited, did the recall surface the right memory. Without that, "improve the
  assistant" stays a feeling.

---

## 6. Target architecture

Keep the orchestrator identity. Change the runtime underneath it.

```
                   ┌─────────────────────────────────────────┐
   channels ──────▶│  Conductor  (one turn loop)             │
  web/WA/TG/voice  │  intent → policy → tool retrieval (RAG)  │
                   │  → brain → gates → learn                │
                   └───────────────┬─────────────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
  ┌───────────┐            ┌──────────────┐          ┌────────────────┐
  │ Asta MCP  │            │  Executors   │          │  Scheduler     │
  │ server    │            │ copilot/claude│         │  (one table)   │
  │ ONE tool  │            │  CLI, staged │          │ cron + events  │
  │ registry  │            │  pipelines   │          │ + interactive  │
  └─────┬─────┘            └──────┬───────┘          │   gate         │
        │                         │                  └────────────────┘
        ▼                         ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ Knowledge:  facts · skills(SKILL.md) · people · episodes      │
  │             FTS5 + local embeddings + recency + usage counts  │
  │             writers: corrections, skill extraction, teacher    │
  └──────────────────────────────────────────────────────────────┘
        ▲
  ┌─────┴────────────────────────────────────────────────────────┐
  │ Trust boundary: every external string (mail, Teams, Jira,     │
  │ web, MCP output) wrapped as UNTRUSTED before it reaches a brain│
  └───────────────────────────────────────────────────────────────┘
```

Five invariants:

1. **One tool registry.** Every capability is defined once and reachable by all
   three brains through MCP.
2. **One execution engine.** Missions and tasks become one `runs` concept with a
   `pipeline` field.
3. **One scheduler.** Feature loops become rows.
4. **Everything external is untrusted** until wrapped.
5. **Every non-trivial run must be able to leave a skill behind.**

---

## 7. Roadmap

### Phase 0 — Safety net (half a day, do first)
- `git init`, `.gitignore` for `.env*` / `data/` / `memory/episodes`, private remote.
- Move the seven scratchpad suites into `tests/`; add `pytest` to the project.
- Rotate the leaked GitHub PAT (still open from July 18).

### Phase 1 — Trust boundary (1 day)
- `app/untrusted.py`: guard markers + marker escaping + policy line, modelled on
  `src/prompt_security.py`.
- Wrap **every** external string at its source: `outlook.read_mail`,
  `teams_bridge.read_chat`/`read_activity`, `jira.get_issue` (description **and**
  comments), all MCP results, any future web fetch.
- Add the policy line to the persona and to both CLI system prompts.
- Rule: untrusted content can never *originate* a write action (commit, send,
  transition) — only inform one that you asked for.

### Phase 2 — One tool registry, one engine (3–4 days) ← unlocks everything
- `app/mcp_server.py` exposing Asta's tools over MCP; register in `mcp.json`,
  pass to Copilot/Claude CLI, keep PydanticAI on the same registry.
- Delete `_first_turn_context` curl tutorials, `_teams_activity_context`,
  `_outlook_context`.
- Merge `missions.py` into `tasks.py` as `pipeline: mission`; one `runs` table
  with a migration; agent gets one `start_run` tool.
- Split `main.py`: `api/`, `chat.py`, `loops.py`.

### Phase 3 — Compounding learning (3–4 days) ← the "more agentic" you're after
- **Skills v2**: `skills/<category>/<name>/SKILL.md` — frontmatter (`tags`,
  `confidence`, `uses`, `last_used`, `source_run`), body as When to Use /
  Procedure / Pitfalls / Verification.
- **Skill extractor**: after any run ≥2 rounds or ≥2 tool calls, distil a skill;
  local model first, Copilot fallback; drop below confidence 0.6; dedupe titles.
- **Teacher escalation**: when the micro tier emits `ESCALATE:`, the full-tier run
  writes the SKILL.md that would have let micro finish.
- **Seed it manually today** with the lessons already earned: MapStruct `clean`,
  `--allow-all-paths`, Jira ACs live in comments, `boot.sh` one-call boot, CLI
  last-line parsing, `_first_turn_context` session invalidation, Copilot safety
  classifier degradation.
- **RAG tool selection** over the same embedding path as `memory.recall`;
  always-available set = `remember`, `ask_user`, `activity/status`.
- Then cut the persona down to identity + hard rules; tool tutorials move to
  retrieved skills.

### Phase 4 — People & communications (4–5 days) ← unlocks calls/meetings/replies
- `people` table: name, aliases, Teams/Outlook/Jira identities, role, tone notes,
  last interaction, **open commitments** (what you owe them / they owe you).
- Populate from Teams activity, Outlook senders and Jira assignees; ask once
  when ambiguous rather than guessing.
- **Meeting prep pack** (fires at `ASTA_PREMEET_MINUTES`): attendees + who they
  are + last thread with each + related Jira + your open commitments + last
  meeting's actions.
- **Outlook reply drafting** on the same approval rail as `teams_draft` (draft →
  approve → send). Sending needs a real path — Graph via a personal token, or
  Playwright on the existing SSO session, which you've already proven works.
- **Commitment extraction**: after any thread/meeting, "you said you'd X by Y"
  → reminder + people row.

### Phase 5 — Meetings & calls (5–7 days, the hardest)
- System-audio capture (BlackHole/Loopback on macOS) → chunked Whisper via
  Voicebox → rolling transcript.
- Live: silent capture; post-meeting: recap, decisions, action items with owners,
  Jira/reminder proposals — all draft-first, nothing auto-posted.
- Only then consider live in-meeting nudges.

### Phase 6 — Review & research (3–4 days)
- **PR review**: `review_pr(repo, number)` → `gh pr diff` + contmark
  `resolve_context` on touched files + repo lessons → structured findings
  (correctness / test gaps / convention) posted **only** after approval.
- **Deep research**: plan → search → extract → synthesise, with the date-grounding
  preamble, running on the local model where possible; HTML report served
  alongside the graphs.

### Phase 7 — Runtime polish (2–3 days)
- One scheduler + event bus; convert the nine loops to rows.
- Interactive gate for background work.
- Detached runs with replay buffer (fixes phone reconnect).
- Adaptive context budget + structured compaction.
- `ask_user` as a first-class tool over WhatsApp.

### Phase 8 — Self-evaluation (ongoing)
Extend `token_audit.py` from cost into quality: plan-revision rate, gate
rejection rate, drafts sent unedited, PRs passing review first time, recall
precision (did the recalled memory get used), skill hit rate. Weekly digest.
Without this the roadmap after Phase 4 is guesswork.

---

## 8. What "more agentic" actually means here

Not more autonomy — you deliberately built gates and you were right to. It means:

1. **It gets better on its own.** Every run either succeeds and leaves a skill,
   or fails and leaves a lesson. (Phase 3)
2. **It carries the right context, not all context.** Retrieved tools, retrieved
   skills, retrieved people — not a fixed 5k-token preamble. (Phase 3)
3. **It knows the humans.** Threads, commitments, tone, history. (Phase 4)
4. **It can be trusted with untrusted input.** (Phase 1)
5. **It measures itself on outcomes, not tokens.** (Phase 8)

Phases 1–3 are ~8 working days and are where the compounding starts. Phase 4 is
what turns a very good coding orchestrator into the assistant you described.
