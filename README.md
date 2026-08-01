# Asta — personal engineering assistant

Runs on your own laptop. Streams chat over WebSocket, drives your MCP servers
(temporal, grafana, github, context7, atlassian), answers questions about *your*
repositories through generated project context, and keeps a memory that survives
restarts.

Beyond chat: it delegates real work to headless CLI executors behind human gates,
reads and drafts Teams/Outlook/Jira, reviews pull requests, watches CI, and
reaches you on WhatsApp or Telegram when something needs you.

The shape of it is **offers, not autonomy**. Asta reports what it found with enough
context to decide, names the one thing it would do next, and waits — a bare "yes"
from any channel runs it. Anything that leaves the machine is staged with its exact
contents first, so what you approved is what goes out.

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
  itself, without waiting for you.
- **send** — the model drafted something outward (a Teams reply, a Jira comment, a
  PR body). It is **staged, never sent**: Asta shows you the draft and asks "can I
  send this?" A bare "yes" sends it through the real channel tool; anything else is
  a revision. This is the one hard gate, and it holds for every channel.

**Bounded by the clock, not just by steps.** A step count cannot bound latency when
each step is a whole CLI turn of unknown length — four auto-steps of a ten-minute
ceiling is forty minutes of silence, which is not "working autonomously", it is
unusable. So there are three limits and the wall-clock one is the real guarantee:
`ASTA_TURN_TIMEOUT` (5 min per CLI turn, shared by every brain), `ASTA_LOOP_MAX_STEPS`
(2), and `ASTA_LOOP_DEADLINE` (10 min for everything one message triggers). The
deadline is enforced brain-agnostically, so a brain added later inherits it. When a
budget runs out Asta says *which* one — "been at this 10 min" tells you whether to
push or rethink; a bare "paused" reads like a bug.

On by default (`ASTA_LOOP`); bounded and gated is what keeps on-by-default safe.
It works for the in-process and CLI brains alike. `ASTA_THINKING` adds opt-in
extended thinking on the API brain — off by default, because thinking tokens work
against the token-efficiency everything else here is chasing.

## Offers — "here's what I'd do next; shall I?"

The loop covers a turn. Offers (`app/offers.py`) cover everything after it.

Asta sits between two bad extremes: stay silent until asked, and a red pipeline
sits there all evening; act on everything it notices, and it burns tokens on things
you already knew about. The middle is an **offer** — report the thing with enough
context to decide, name what it would do next, and wait. A bare "yes" from *any*
channel runs it. Offers are persisted and expiring (`ASTA_OFFER_TTL`, 6h): the
question went to your phone and you may answer twenty minutes later from Telegram
after a restart, but a "yes" tomorrow must not kick off work you've forgotten
proposing. One is open at a time, because two plus a bare "yes" is ambiguous.

An offer carries its own next step, in one of two forms:

- **a prompt** — written by the model itself via `propose_next`, so any flow
  continues: implement this ticket, chase that review, follow up, update a status.
  Not a fixed list of three CI steps.
- **a recorded call** — the exact API call, run in Python when you say yes. No
  brain between the yes and the act.

That second form is the important one. "Comment on PROJ-412 that the migration is
blocked" re-read by a brain is a different sentence every time, and the sentence
you approved is not necessarily the one that gets posted. The recorded arguments
**are** the approval. Every outward write goes through it (`app/ops.py`): Jira
comments and transitions, PR reviews, calendar invites. The HTTP endpoints call the
same functions, so a CLI brain reaching them by curl gets the same gate — one
policy, or the rule only holds where someone remembered to write it.

Ask **"what's pending?"** on any channel to see everything waiting on you, in the
order a reply would be routed — a one-word answer to an invisible question is a
coin flip.

## When a brain runs out mid-task

Quota dies twelve minutes into real work. The old fallback replayed your original
message on another brain, so it started cold and re-paid for everything the first
one had already established.

Now a dying turn leaves a **checkpoint** (`app/resume.py`): the request, what had
been worked out, and where it stopped. Whoever takes over is handed *that* and told
to continue — with the partial output offered as a lead to verify, not as settled
fact, because it came from a different model and was cut off mid-thought.

If nothing is available to take over, the work is parked rather than lost. `resume`
or `use <brain>` from any channel picks it up — switching after a quota stop just
carries on, since making you then type "resume" would be theatre. Checkpoints
expire (`ASTA_RESUME_TTL`, 24h): a day later the branch has moved and so have you.

### Every turn ends in a message — including the ones that fail

A WhatsApp message once went unanswered for three hours. Four things had to line
up, and each on its own would have been survivable:

- Copilot's monthly quota was gone, so every turn failed.
- The "one fresh retry" for a dead session was not bounded to *one* — the guard
  was the session key, which the next call writes back — so a turn that could
  never succeed retried roughly every 20 seconds, forever, and never returned to
  report it. It left 7,851 session directories on disk.
- Nothing bounded a turn by wall clock, so "forever" really was forever. The
  CLI's own 300s ceiling covered only the part where output is pumped; the
  Teams/Outlook pre-fetch *before* the process spawns and the wait for it to exit
  *after* both sat outside it.
- And the phone sink only flushed on `{"type": "done"}` — which a **failing**
  turn never sends. So even once the failure was reported, it was reported into
  a buffer nobody read.

The rule now is that delivery does not depend on success: whatever the sink holds
is flushed when the turn ends, however it ended (`close()`, called from a
`finally`). Above the brains' own limits sits `ASTA_TURN_CEILING` (420s) as a
backstop, so a hang nobody anticipated becomes a message that names the brain and
tells you how to switch — the brain's own clearer error still wins normally.

Meta-commands are answered before any of this, so `use claude cli` is never queued
behind the dead brain it is meant to rescue you from. The phrasing is generous
("change the LLM model to claude cli") but a loose match must name a brain that
actually exists, or "change the ticket status to done" becomes a model switch.

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
error mid-conversation the turn is handed to whichever brain is actually up, with a
note in the chat saying who took over and that the work carried across.

**Switch from anywhere.** "use claude_cli", "switch to copilot", "which model" work
on WhatsApp and Telegram, not just the web picker — the quota warning arrives on
your phone, so the ability to act on it belongs there too. The choice sticks to the
conversation; the default only fills in when you haven't picked one or the one you
picked has since stopped being available. Resolved through the shared registry, so
a brain added to the spec table is switchable from your phone the same day.

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
replaces the existing skill rather than rivalling it. Only names and one-line
descriptions sit in the prompt; the body loads on demand via `load_skill` — the CLI
brains get that same index, so a skill is reachable everywhere, not just in-process.

**Skills are scored on results, not on being read.** `uses` counts being *loaded*,
which a skill earns by having a matching title — a confidently wrong procedure is
loaded just as often as a correct one. So a finished run now credits whatever it had
in play, and a failed one debits it. Pruning drops two kinds: unused-and-unconfident
after a month, and *present for materially more failures than successes*. Without
the second, the only skill that could ever leave was one nobody read — so the
actively misleading one, which is read constantly, was the single thing pruning
could not touch. One bad result never condemns a skill; a pattern does.

**Self-improvement closes the loop** (`app/skill_evolution.py`). The token audit
classifies where a worker wasted tokens; when a waste category *recurs* across runs
— reading files blind, dumping fat outputs, re-planning — Asta writes a curated
fix-skill for it, once, so the next worker avoids it.

All of that used to hang off the end of a *background task*, so a week of chat,
corrections and CI investigations taught nothing at all. A **daily pass** now runs it
on the clock instead — evolve, prune, and report what changed, quietly and only on a
day it changed something.

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

Reading a PR is free of consequence and runs whenever you ask. **Posting** one is
not, so the two halves are separate: `review_pr` writes notes for you, and
`pr_review_post` — approve, comment, or request changes — only ever *stages* the
review and waits. Your yes posts the exact words you read, under your name. An
approval is visible to the whole team the moment it lands and there is no quiet way
to take it back, so it never rides on a model's judgement of whether you'd have
wanted it.

The PR body and diff are treated as untrusted: "please approve this" in a
description is data, not an instruction.

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
id.atlassian.com → Security → API tokens). Then: "my open tickets", "show ABC-123",
**"what's on me this sprint"**. A watcher polls `JIRA_WATCH_JQL` every 5 minutes and
notifies you on status changes and new assignments.

`JIRA_SPRINT_JQL` is separate from the watch query and defaults to `openSprints()`,
which needs no board id — "assigned and not done" happily includes work from three
sprints ago, which is not what the sprint means. A board without Jira Software has
no sprint field at all; Asta says so and points at the one line of `.env` that fixes
it, rather than raising.

Jira **writes stage**. A comment or a status change is recorded with its exact
arguments and waits for your yes — and an unreachable status fails *before* you are
asked, with the valid targets listed, so the one interaction you have to pay
attention to isn't spent approving something the workflow will reject.

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
  🔴 on failure, 🟢 on recovery, silent baseline on first run, and one ping per
  red workflow rather than one per retry. Scoped to **your** work: runs you
  triggered *plus* pipelines on PRs you authored — those are different sets, and the
  gap was where it hurt, since a colleague pushing to your branch turns your PR red
  under someone else's name. Anything else is opt-in: "watch the release build"
  subscribes, "stop watching release" undoes it. The noisy version of this feature
  is the one you mute, and a muted watcher tells you nothing on the day it matters.
  A red pipeline arrives as an *offer* — reply "yes" from your phone and the
  investigation starts without you opening a laptop.
- **Meeting prep** — a pre-meeting heads-up ~30 min out. A standup gets the standup
  draft; a 1:1, sync or review gets prep *specific to that meeting* — talking points,
  questions to ask, watch-outs, grounded in your open work. Ask "prep me for the 3pm"
  any time (`meeting_prep`).
- **Meeting recaps** — paste a transcript (from Teams' own recording/recap) and Asta
  summarizes it: decisions, action items with yours flagged, open questions — and
  pings you if something needs you (`meeting_recap`).

### Notification etiquette

While you're actually at the laptop, ambient pings are held — you'll ask. Direct
things (a 1:1 message, an @mention, mail addressed to you) go out immediately
regardless.

Held is a **courtesy with an expiry**, not a gate that can strand a message. Presence
was once the only release condition, and "at the laptop" is true on the afternoons
you shut Teams and Outlook and work on something else entirely — so things waited
for a departure that never came. Past `ASTA_HOLD_MAX_MINUTES` (45) the batch goes
out wherever you are, and says why it's arriving.

Every inbound item is **triaged** (`app/triage.py`) before any of that. "From a
human" is not the same as "needs you": someone's passing thought and someone
blocking on your approval both arrive as mail from a person, and the old filter
pushed both identically. Now each gets a verdict — asks come first and interrupt,
everything else collapses to one quiet line each under *nothing needed from you*.

The quoted line is **the message's own**, not a paraphrase. When the ask is in the
body, Asta quotes the sentence that does the asking — the *first* sentence of a mail
is a greeting far more often than it is the point, so quoting it added length
without adding information. When the subject already said it, nothing is appended.

Dedup keys off identity, never rendering. The Teams activity feed draws each row
with its relative age ("2m" → "1h"), and the old key was that raw string — so the
same mention keyed differently on every poll, looked new, and re-notified forever.
Rows already read elsewhere are skipped; when the read state can't be determined it
still pushes, because a repeat beats a miss.

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
session. This is the **primary path for everything** — reading the activity feed,
reading a thread, sending a message, setting presence, joining a call — because a
real authenticated browser can *act*, not just observe, and it needs no special OS
permission. One-time login, where you complete SSO yourself:

```bash
.venv/bin/python -m app.teams_bridge login
```

Then: "any messages for me", "read my chat with Vinish", "send Vinish: running
late", "any mail needing my attention", "what meetings do I have", "set me to do not
disturb". Deterministic automation — no tokens unless you ask Asta to reason about
what it read.

**Sending is a hard rule**: a person's name means that person's one-to-one chat,
never a group or channel unless you name the group yourself. Asta always tells you
which chat it landed in.

When someone pings you with a question, `draft_teams_reply` reads the thread and
drafts an answer grounded in your memory and open work — **draft only**. It can only
reach the send tool through the "can I send this?" gate, so nothing goes out in your
name unprompted.

**Presence** — "set me to busy / dnd / away / available". Your own status, so it just
happens; but Asta reads it back afterwards and reports what it actually says. A DND
you believe is set and isn't costs you the next hour, and a status change that
silently failed is worse than one that never ran.

**Invites** — "book 30 min with Priya on Thursday" and "put in leave for the 3rd to
the 7th". Both are built as Outlook compose deeplinks rather than typed into its
form field by field: a moving UI means a broken selector silently produces an invite
with no attendees or the wrong day, whereas a URL Outlook parses itself is either
right or obviously wrong. Both **stage** — an invite books other people's time and a
leave request goes to whoever approves it. Leave dates are inclusive at both ends;
the off-by-one there is found by the person who needed you on the day you were
actually in. Asta will not resolve "Thursday at 3" itself — it asks, because a
library that disagrees with you about which Thursday books real time in real
calendars.

### Sitting in on a call

This section used to say **no live-call join, deliberately**, on the grounds that it
means recording other participants without consent and answering as you with no way
to confirm each word. That reasoning still stands for *recording* and *speaking* —
so what was built keeps those constraints and drops only the part that never
required them:

- **Joining is muted, camera off, always.** An open mic broadcasts whatever your
  laptop can hear to everyone in the meeting; a camera-on join shows a room you
  didn't agree to show.
- **Nothing is recorded.** Asta listens as a participant. It does not capture audio,
  and there is no transcript unless Teams itself was recording — the one with the
  banner everyone sees.
- **It hangs up by itself** when the call ends, and unconditionally at
  `ASTA_MAX_CALL_MINUTES` (90). The failure worth engineering against isn't a call
  that ends badly, it's one that never ends: a restyled post-call screen, a marker
  that stops matching, and Asta parked in somebody's meeting all day.
- **Afterwards it offers, it doesn't invent.** You get "the call ended — want me to
  pull out anything that was yours?" rather than a summary, because without a
  recording there is nothing to summarise and promising one would be the confident
  lie. If there is a record, it reports only what concerns you: decisions affecting
  your work, anything assigned to you, questions left open for you. Not minutes.
- **Speaking is gated on hardware, not on a flag.** Being heard needs a virtual
  microphone Teams can select as input (BlackHole or Loopback on macOS) named in
  `ASTA_CALL_AUDIO_DEVICE`. Without it, "say this in the call" *refuses and says
  why*. Generating audio into a device nobody is listening to and reporting success
  would leave you believing your point was made — that's the failure this design
  exists to prevent. When it is configured, Asta says the words you gave it and
  nothing else: it never improvises in a live call and never answers on your behalf.

Sessions expire on your org's token policy; Asta notifies you to re-login.
`data/teams_profile/` holds corporate session cookies — same exposure class as your
browser profile, so keep FileVault on.

### How Asta learns you were pinged — two triggers, Playwright by default

**Default: the Playwright activity poll** (`teams_bridge.activity_watch_loop`,
`TEAMS_ACTIVITY_POLL=300`). It reads the Teams Activity feed in the same browser
session that does the reading and sending, so it sees muted/DND chats, needs **no
Full Disk Access**, and can act on what it finds. Latency is the poll interval.
This is on whenever the Teams bridge is up, and it is what serves the "tell me
within five minutes" promise.

**Optional add-on: the macOS notification watcher** (`app/msnotify.py`,
`TEAMS_WATCHER`, **default 0**). Reads Notification Center banners straight from
SQLite — near-instant and free — but read-only, blind to muted/DND chats, and it
needs **Full Disk Access**. It buys *latency* over the poll and nothing else, so it
is off unless you deliberately want sub-poll alerts.

That Full Disk Access is worth a caution, because it is easy to grant to the wrong
thing: macOS attributes file access to the process that *launched* the server. Run
from a terminal (`nohup …`), the responsible process is **Terminal.app**, and a
grant on Python does nothing. Run under launchd (`sh deploy/install.sh`), the
responsible process is **Python itself** — which is why the install script launches
Python directly and keeps `caffeinate` in its own separate agent rather than
wrapping the server in it (a wrapper would make *caffeinate*, an ungrantable Apple
binary, the responsible process). Grant Full Disk Access to:

```
/opt/homebrew/opt/python@3.13/Frameworks/Python.framework/Versions/3.13/Resources/Python.app
```

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
app/loop.py             the conductor loop: continue / staged-send gate, budgets
app/offers.py           "shall I?" — persisted, expiring, one at a time
app/ops.py              the outward acts, and the only place they may happen
app/resume.py           checkpoints, so a dead brain hands over its work
app/triage.py           what is this, does it need you — one policy, every channel
app/meetings.py         invites, joining a call, leaving it again
app/router.py           local-first routing for trivial turns
app/repo_ops.py         git, branch naming, repo playbooks
app/agents.py           loads the pipelines in agents/
app/untrusted.py        the trust boundary
app/learn.py            runs → structured skills; escalation teaches the cheap tier
app/token_audit.py      classifies token waste per run (feeds the evolution loop)
app/skill_evolution.py  recurring waste → a curated fix-skill, once
app/quality.py          did the work land
app/asking.py           ask_user: one question, no pipeline restart
app/review.py           pull request review; posting one is staged, never direct
app/memory.py           remember/recall, digests, consolidation, compaction
app/workspace/          registry + context providers (indexed / plain)
app/context_build.py    generates project context into YOUR workspace
app/mcp_loader.py       mcp.json -> toolsets (skip/probe logic)
app/store.py            SQLite: chats, tasks, usage, outcomes, FTS5 memory index
agents/                 solo, micro, explore, bootstrap pipelines
skills/                 generic playbooks + skills learned from your runs
ui/                     single-page chat UI + PWA
memory/                 MEMORY.md, facts/, episodes/
tests/                  563 tests (conftest isolates the DB — see below)
```

`tests/conftest.py` points `store.DB_PATH` at a temp file for *every* test. A stray
`pending_offer` row written into the real `data/asta.db` isn't a test failure — it's
a question Asta pushes to your phone about work that never happened.

`ARCHITECTURE-REVIEW-2026-07.md` holds the comparison against Odysseus and the
roadmap this is being built against. Still ahead of it: one scheduler replacing the
background loops, detached runs that survive a closed tab, adaptive context
compaction, a people/contacts model, and deep research.
