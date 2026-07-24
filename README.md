# Asta — personal engineering assistant

Runs on your own laptop. Streams chat over WebSocket, drives your MCP servers
(temporal, grafana, github, context7, atlassian), answers questions about *your*
repositories through generated project context, and keeps a memory that survives
restarts.

Beyond chat: it delegates real work to headless CLI executors behind human gates,
reads and drafts Teams/Outlook/Jira, reviews pull requests, watches CI, and
reaches you on WhatsApp or Telegram when something needs you.

Everything about your work stays on your machine. The repo holds generic skills
and pipelines; your workspaces, generated context, memory and credentials do not
leave the laptop.

## Run it

```bash
cd ~/help/asta
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8321
```

Open http://localhost:8321 and log in with `ASTA_TOKEN` from `.env`.
Copy `.env.example` to `.env` first — every setting is documented there.

```bash
.venv/bin/python -m pytest tests -q     # 315 tests
```

## How it is put together

Three ideas carry most of the design.

**One engine.** Every piece of work — analysis, a code change, a Teams draft, a PR
review — is a *background task*: a headless `copilot -p` or `claude -p` process
with a self-contained prompt, running outside the chat so the conversation stays
responsive. There is no second path. (There used to be: `missions.py` and
`tasks.py` ran the same plan → approve → implement → verify → ship pipeline, and
every bug had two homes.)

**One capability registry.** `app/capabilities.py` declares each capability once,
with its function; the docstring *is* the description. Chat tools, the block that
teaches the CLI brains, and the MCP server all read from that one table, so a
capability cannot be described two different ways. Per-tool hard rules — a Teams
message means the person's 1:1 chat, a Jira write needs your confirmation, a PR is
never opened unprompted — travel *with* the capability, so a tool can never be
exposed with its rule left behind.

**One trust boundary.** Anything Asta reads from outside — mail, Teams messages,
Jira comments, files in your repos, MCP output, PR descriptions — is wrapped as
data before it reaches a model that can edit code and run shell commands. See
`app/untrusted.py`. It is mitigation, not a guarantee; what actually bounds the
damage is that nothing publishes, ships or sends without your approval.

## Work: task → plan → your approval → implement → you say ship

Say "implement ABC-123 in booking" in chat, WhatsApp or Telegram. Routing is
automatic: a Jira-key ticket runs the full staged pipeline with a plan gate, a
small ad-hoc ask runs the micro pipeline (~25 turns) and escalates itself if it
turns out bigger.

1. A cheap **context gate** first — if the goal is ambiguous it asks *before*
   spending anything on discovery.
2. It plans, and stops. You get the plan on your phone.
3. "approve task N" implements; any other reply re-plans with your feedback.
4. It finishes at a **reviewed diff**. The pipeline never pushes.
5. You say ship → branch pushed, PR opened per repo touched, CI watched.
6. On green it *asks* whether to post the PR for review, and only where you name.

Kinds: **analysis** (read-only, runs in parallel), **code** (edits a repo,
serialised per workspace so two tasks can't fight over git state), **teams_draft**
(never sent automatically).

Commits are plain. No co-author trailer, no assistant name, no "Generated with"
line — your commits read as your own work.

Pipelines live in `agents/` (`solo`, `micro`, `explore`, `bootstrap`) and belong to
Asta, not to your repos — improving one improves every run. Your repos still supply
the facts, through their own `.github/agents` and `.github/skills` when present.

## The conductor loop — it keeps working instead of idling

A chat turn used to end the moment the model stopped typing, and nothing happened
until you sent the next message. The loop (`app/loop.py`) lets a turn hand back one
of two signals instead:

- **continue** — the model isn't done and named the next step. Asta runs that step
  itself, without waiting for you, bounded by `ASTA_LOOP_MAX_STEPS` so a confused
  model can't spin forever.
- **send** — the model drafted something outward (a Teams reply, a Jira comment, a
  PR body). It is **staged, never sent**: Asta shows you the draft and asks "can I
  send this?" A bare "yes" sends it through the real channel tool; anything else is
  a revision. This is the one hard gate, and it holds for every channel.

On by default (`ASTA_LOOP`); bounded and gated is what keeps on-by-default safe.
It works for the in-process and CLI brains alike. `ASTA_THINKING` adds opt-in
extended thinking on the API brain — off by default, because thinking tokens work
against the token-efficiency everything else here is chasing.

## Workspaces and project context

Workspaces are configured in the UI (⚙ Settings → Workspaces), not in code. Point
Asta at a directory, pick which repos you care about, and it detects how to answer
questions about them:

- **indexed** — a generated `.asta-context/` with a resolver that maps a question
  to exact files and line numbers;
- **plain** — ripgrep/git-grep fallback, so an unindexed repo still works.

Selecting repos kicks off a background pass that reads them and writes that
context **into your workspace**, next to the code. It states its cost before
spending, runs at most three repos at a time, and notifies you when it lands.

Then the agent has `resolve_context` (always called before reading code — never
blind exploration), `read_workspace_file` and `list_services`. The **Graph** tab
embeds the generated graph pages.

Drift is watched for free: a 10-minute git fingerprint, zero tokens. Only
*material* change counts — adding test fixtures or data files won't flag your
context as stale. Re-enrichment costs tokens and is always your call.

## Models

**Copilot CLI (office)** is the day-to-day default: chat runs through `copilot -p`
with per-conversation continuity, at zero personal API cost. If it hits a quota
error mid-conversation the turn is re-routed to **Claude CLI** automatically, with
a note in the chat.

Optional in `.env`: `ANTHROPIC_API_KEY` (Claude, with prompt caching),
`OPENAI_API_KEY`, or LM Studio running locally — auto-detected, and it powers the
free background jobs (digests, consolidation, compaction, skill extraction,
embeddings). Models without a key show as "(off)" in the picker.
`ASTA_TEST_MODEL=1` adds a no-LLM model for pipeline debugging.

Asta is the **orchestrator**: it plans, remembers, notifies and delegates. It does
not implement in chat.

**Local-first routing** (`app/router.py`, `ASTA_ROUTER`, on by default): a pure
pleasantry — "hi", "thanks", "great" — is answered on the free local model or a
canned line, instead of spawning a paid CLI (~24k tokens) to say hello. Conservative
by design: only self-contained social turns are diverted; anything with real content
always reaches the brain you picked.

## Tool selection

A turn carries roughly ten of forty tools, not all of them — capabilities are
ranked against the message (embeddings via LM Studio when it's up, lexical overlap
otherwise) and only the relevant ones plus a tiny always-available core are
exposed.

Two things keep this from backfiring. The selection is **sticky per conversation
and only ever grows**, because tool definitions sit in the cached prompt prefix and
re-picking every turn would trade a fixed cost for a recurring cache miss. And when
ranking is uncertain it returns *everything* — an expensive turn is a far smaller
failure than a tool the model could not reach. `ASTA_TOOL_RAG=0` restores the
all-tools behaviour.

## Memory and learning

**Memory** is what you told it and what sessions uncovered:

- `memory/MEMORY.md` — a tiny index, always in the system prompt
- `memory/facts/*.md` — durable facts (the `remember` tool writes here; plain
  markdown, edit or delete by hand)
- `memory/episodes/*.md` — session digests, written when a chat goes idle 30 min
- recall is automatic per message (SQLite FTS5, recency-weighted)

**Skills** are procedures, and they are earned. A run that took several rounds — or
that escalated because a cheap tier couldn't finish it — is distilled into a
structured skill: when to use it, the procedure, the pitfalls, how to verify. When
a task escalates, the *stronger* tier writes the skill, so the cheap one gets
through alone next time; an escalation that teaches nothing just repeats next week.

Guarded on purpose: below 0.6 confidence nothing is written, a near-duplicate
replaces the existing skill rather than rivalling it, and unused low-confidence
skills are pruned. Only names and one-line descriptions sit in the prompt; the body
loads on demand via `load_skill` — the CLI brains get that same index, so a skill is
reachable everywhere, not just in-process.

**Self-improvement closes the loop** (`app/skill_evolution.py`). The token audit
classifies where a worker wasted tokens; when a waste category *recurs* across runs
— reading files blind, dumping fat outputs, re-planning — Asta writes a curated
fix-skill for it, once, so the next worker avoids it. Measure → improve, without you
in the loop.

Nightly consolidation (merge duplicates, prune, rewrite the index):

```cron
30 2 * * * cd /Users/arun.k.k/help/asta && .venv/bin/python -m app.memory consolidate >> data/consolidate.log 2>&1
```

## Knowing whether it is any good

`trace_report` and the token audit (`GET /api/token-audit`) measure what a turn
*cost*. `quality_report` measures whether the work *landed*: plans approved as-is versus sent back, tasks finished, drafts sent
unedited, questions answered, PRs opened, skills learned.

These are facts Asta already observes — no model judging its own output, so the
numbers can't flatter themselves. Ask "how are you doing?" in chat, or
`GET /api/quality`.

Every turn is also traced (model, first-token and total latency, tokens
in/out/cached, prompt sizes, tools, errors) into the `traces` table — ⚙ Settings →
Performance, `GET /api/traces`, or just ask.

## Asking you one question

When the answer genuinely changes what it would do — which of two repos, which of
two people — Asta asks *one* question, it reaches your phone, and the caller
resumes with your answer. Nothing restarts.

This exists because the approval gates are the right price for "approve this plan"
and far too expensive for "which repo did you mean?" — a re-plan cycle costs
hundreds of thousands of tokens. Reply from any channel; a bare reply answers when
one question is open, `answer 3 <text>` when several are.

## Reviewing pull requests

"review PR 123 in booking" gathers the PR, its diff, its CI checks and your project
context, then runs the review as a background task. You get reviewer notes:
verdict, blocking defects with file:line, non-blocking notes, test gaps, questions.

Read-only by design — it writes notes for *you* to post, and never comments on or
approves a PR itself. The PR body and diff are treated as untrusted: "please
approve this" in a description is data, not an instruction.

## MCP

### Servers Asta uses

`mcp.json` uses the standard `mcpServers` shape (same as Claude Desktop / Cursor).
Adding one is a JSON entry, no code. A server whose binary is missing is skipped, a
server with an empty required env var is skipped, and every server gets a 20-second
handshake probe at startup — dead ones are dropped and the sidebar shows why.

**Atlassian (Jira/Confluence)** needs a one-time OAuth login:

```bash
.venv/bin/python -m app.mcp_login atlassian
```

Tokens persist under `data/oauth/atlassian/`. The REST `JIRA_API_TOKEN` config
stays as the primary path for reads and is what the change watcher uses.

### Asta as an MCP server

Asta serves its own capabilities over MCP, so a CLI brain calls `resolve_context`
directly instead of being taught a curl line for it:

```bash
.venv/bin/python -m app.mcp_server --list           # what it exposes
.venv/bin/python -m app.mcp_server --print-config   # the mcpServers entry
```

The config is printed, not installed — pointing your Copilot or Claude CLI at it is
a change to *your* tools, and yours to make.

## Jira

Set `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` in `.env` (token from
id.atlassian.com → Security → API tokens). Then: "my open tickets", "show ABC-123".
A watcher polls `JIRA_WATCH_JQL` every 5 minutes and notifies you on status changes
and new assignments. Any Jira **write** shows you the exact text or target first,
unless you dictated it in the same message.

## Daily rhythm

- **Reminders** — "remind me at 3pm to reply to Vinish", from any channel. One-shot
  or daily/weekdays/weekly. Overdue ones (laptop asleep) fire on wake with a "was
  due N min ago" note.
- **Morning brief** (`BRIEF_TIME=08:30`, weekdays) — finished work, things waiting
  on you, Jira movement, today's reminders, health issues. Deterministic, zero LLM
  tokens.
- **Standup draft** (`STANDUP_TIME=09:15`, weekdays) — from yesterday's real git
  commits across your workspace repos, plus finished work and Jira.
- **Health check** (6-hourly) — channels, sessions, Copilot, LM Studio, disk.
  Notifies on a *new* problem and once on recovery; no repeat nagging.
- **CI watcher** (10-minute poll, `gh` auth from the keychain — no PAT in `.env`) —
  🔴 on failure, 🟢 on recovery, silent baseline on first run.
- **Meeting prep** — a pre-meeting heads-up ~30 min out. A standup gets the standup
  draft; a 1:1, sync or review gets prep *specific to that meeting* — talking points,
  questions to ask, watch-outs, grounded in your open work. Ask "prep me for the 3pm"
  any time (`meeting_prep`).
- **Meeting recaps** — paste a transcript (from Teams' own recording/recap) and Asta
  summarizes it: decisions, action items with yours flagged, open questions — and
  pings you if something needs you (`meeting_recap`).

Notification etiquette: while you're actually at the laptop, ambient pings are held
— you'll ask. Direct things (a 1:1 message, an @mention, mail addressed to you) go
out immediately regardless.

## Channels

### Phone access (Tailscale)

1. Install Tailscale on the Mac (`brew install --cask tailscale`) and your phone;
   sign both into the same tailnet.
2. `tailscale ip -4` on the Mac → e.g. `100.101.102.103`.
3. Set `ASTA_HOST` to that address in `.env` and restart.
4. Open `http://100.101.102.103:8321` on the phone, log in, then **Add to Home
   Screen** — the PWA manifest makes it install like an app.

The token cookie lasts 90 days; everything is behind `ASTA_TOKEN`.

### Telegram (official API — the recommended channel)

Zero ban risk, works anywhere. Message **@BotFather** → `/newbot`, put the token in
`.env` as `TELEGRAM_BOT_TOKEN`, restart, then send `/start <ASTA_TOKEN>` to your bot
(binds it to your chat; strangers are ignored).

### WhatsApp

```bash
cd whatsapp && npm start     # then pair from ⚙ Settings
```

**Use a second, dedicated number as the bot account** — your personal account then
carries no ban risk. Scan the QR from that number, set "allowed JID" to your
personal number, and your personal WhatsApp chats with Asta like a normal contact.
Unofficial protocol (Baileys), so the residual risk sits on the throwaway number.
Bridge listens on 127.0.0.1:8323.

### Teams and Outlook (no Azure AD)

`app/teams_bridge.py` drives Teams web through a Playwright profile holding your
session. One-time login, where you complete SSO yourself:

```bash
.venv/bin/python -m app.teams_bridge login
```

Then: "any messages for me", "read my chat with Vinish", "send Vinish: running
late", "any mail needing my attention", "what meetings do I have". Deterministic
automation — no tokens unless you ask Asta to reason about what it read.

**Sending is a hard rule**: a person's name means that person's one-to-one chat,
never a group or channel unless you name the group yourself. Asta always tells you
which chat it landed in.

When someone pings you with a question, `draft_teams_reply` reads the thread and
drafts an answer grounded in your memory and open work — **draft only**. It can only
reach the send tool through the "can I send this?" gate, so nothing goes out in your
name unprompted.

Deliberately **no live-call join**. Asta will not silently sit in a call: that means
recording other participants without their consent, on a corporate tenant, and
answering as you with no way to confirm each word. The supported path is *after* the
call — feed it the transcript from Teams' own recording (the one with the recording
banner everyone sees) and it recaps + flags your actions. Sessions expire on your
org's token policy; Asta notifies you to re-login. `data/teams_profile/` holds
corporate session cookies — same exposure class as your browser profile, so keep
FileVault on.

### macOS notification watcher

`app/msnotify.py` watches the Notification Center database for Teams/Outlook
banners mentioning you. Pure-Python filtering, so notification text never reaches a
model. Off by default: grant **Full Disk Access** to the terminal running Asta, set
`TEAMS_WATCHER=1`, restart. It only sees what macOS shows as a banner, so muted
chats are invisible — the Teams bridge above covers those.

### Voice

🎙 dictates one message; 🔊 speaks replies. 🎧 is hands-free conversation mode: it
listens, sends when you pause, speaks the reply, then listens again — same agent,
same memory and tools. Ends via the banner, the toggle, or after ~4 silent rounds.
Works in Chrome/Edge/Safari including the phone PWA.

## Always-on

```bash
sh deploy/install.sh        # remove with: sh deploy/install.sh remove
```

Installs `com.asta.server` and `com.asta.whatsapp` as user LaunchAgents: start at
login, auto-restart on crash, logs in `data/logs/`. The server runs under
`caffeinate -si`, which prevents system sleep **on AC power** — screen off, locked,
lid open, it keeps working.

Hard physics limit: **lid closed on battery, macOS force-sleeps.** No software
prevents that. Keep it plugged in for lid-closed operation.

**Why not Docker?** Docker Desktop containers pause when the Mac sleeps, so it
solves nothing here and complicates everything: the stdio MCP proxies are host
Python scripts behind the corporate VPN, LM Studio serves on the host, and
`copilot`/`claude` are host binaries with host auth. Docker earns its keep only if
Asta moves to an always-on Linux box reached over Tailscale — that's the real
endgame; revisit then.

## Security notes

- Credentials live in `.env`, which is gitignored, as are `data/`, `memory/`,
  workspace config and the WhatsApp/Teams session directories.
- The API is behind a single bearer token with no expiry or scoping. That is
  acceptable for one user on a tailnet; it needs scoping and rotation before this
  is exposed any wider.
- Rotate the GitHub PAT if one is sitting in plaintext in
  `~/.config/github-copilot/intellij/mcp.json`, and reference it as
  `${GITHUB_PERSONAL_ACCESS_TOKEN}` there instead.

## Layout

```
app/main.py             FastAPI: WS chat, REST, graph hosting, auth
app/agent.py            the agent, model registry, persona
app/capabilities.py     ONE registry — chat tools, CLI teaching, MCP all read it
app/tool_index.py       ranks capabilities per message; sticky per conversation
app/mcp_server.py       serves Asta's capabilities over MCP
app/tasks.py            the one work engine (plan → gate → implement → ship)
app/loop.py             the conductor loop: continue / staged-send gate
app/router.py           local-first routing for trivial turns
app/repo_ops.py         git, branch naming, repo playbooks
app/agents.py           loads the pipelines in agents/
app/untrusted.py        the trust boundary
app/learn.py            runs → structured skills; escalation teaches the cheap tier
app/token_audit.py      classifies token waste per run (feeds the evolution loop)
app/skill_evolution.py  recurring waste → a curated fix-skill, once
app/quality.py          did the work land
app/asking.py           ask_user: one question, no pipeline restart
app/review.py           pull request review
app/memory.py           remember/recall, digests, consolidation, compaction
app/workspace/          registry + context providers (indexed / plain)
app/context_build.py    generates project context into YOUR workspace
app/mcp_loader.py       mcp.json -> toolsets (skip/probe logic)
app/store.py            SQLite: chats, tasks, usage, outcomes, FTS5 memory index
agents/                 solo, micro, explore, bootstrap pipelines
skills/                 generic playbooks + skills learned from your runs
ui/                     single-page chat UI + PWA
memory/                 MEMORY.md, facts/, episodes/
tests/                  315 tests
```

`ARCHITECTURE-REVIEW-2026-07.md` holds the comparison against Odysseus and the
roadmap this is being built against. Still ahead of it: one scheduler replacing the
background loops, detached runs that survive a closed tab, adaptive context
compaction, a people/contacts model, meeting and call capture, and deep research.
