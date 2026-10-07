# Asta

**A personal engineering assistant that runs on your laptop.**

Asta reads your repos, Jira, mail, Teams and CI, does the work in the background,
and reaches you on WhatsApp or Telegram. It never ships, sends or merges anything
you did not approve.

```
you ── web · WhatsApp · Telegram · voice
 │
Asta ── front desk → task engine → attention → hands
 │
brains ── Copilot CLI · Claude CLI · LM Studio · API keys
```

## Features

- **Real work, gated** — plan → your approval → implement in a worktree → verify → you say ship
- **Separate requests stay separate** — independent code tasks can be scheduled while another is live (work in the same workspace is serialized for safety); queued chat messages survive a restart without being mistaken for answers to later questions. Name a task number when amending one of several live tasks (`task 219 also add tests`); `approve both 219 and 220` checks each gate individually.
- **Task checks do not redo work** — asking about a task's CI reads its linked PR and, when you ask why earlier runs failed, checks the run history and available failed logs. A task number alone never authorizes a new implementation; `task 219 fix the failure` does, while `task 219 check CI` leaves its branch and status untouched. Missing evidence is reported as unknown, not called green.
- **Jira updates use recorded approvals** — commenting on a ticket and transitioning its status are separate actions, each showing the exact target and change before approval. A generic “send” draft cannot post to Jira, and a queued action is not yet done.
- **Honest completion** — blocked work cannot be marked done by approving it; code tasks with no worktree change or verified PR are not reported as implemented (explicit no-change results are labeled as such). A local implementation is not called shipped until the requested PRs, including release-branch PRs, have verified GitHub receipts.
- **Teams replies** — a routine 1:1 "checking, will update you" acknowledgement is automatic and deduplicated; it does not count as a reply you wrote yourself. Substantive answers are staged for your approval. If another colleague asks about the same verified PR revision, Asta reuses the review; after you approve and send a generic reply, it may send those *exact same words* to a different 1:1 colleague without another prompt. Personalized replies, group/manager chats, changed commits or CI, and GitHub reviews still need their own approval or recheck. `send to Vinish` approves a draft already staged for Vinish, not a different recipient.
- **Teams Activity safety net** — channel mentions come from Activity, not the chat sweep. An offline browser hint does not by itself stop the read; when offline is reported, Asta checks Teams connectivity before accepting visible feed rows. Failed reads retry sooner without restarting a responsive browser. After an interruption it pages through Activity until it reaches previously seen rows, and warns if older items cannot be verified.
- **Any brain** — Copilot, Claude, local models; fails over when one runs out of quota
- **Resumable** — tasks are checkpointed LangGraph threads; restarts and usage limits pick up where they stopped
- **Quiet by design** — one ranked inbox, a daily interruption budget, a digest for the rest
- **Standing rules** — "from now on…" becomes a rule the code enforces, not a note a model may forget
- **Hands** — writes Excel, Word, PowerPoint and PDF; uses Reminders, Calendar, Notes, Outlook and Finder through their own doors; clicks as a last resort, checking every step
- **Keeps promises** — "warn me if PR 17 isn't merged by 5" is watched, and warns *before* the deadline
- **Improves itself, carefully** — tunes a few bounded settings, and keeps a change only if it beats the current version on the bench
- **Tested like a product** — ~3,000 tests plus a scenario bench that replays a whole working day

## Quick start

```bash
git clone https://github.com/Arunk02/asta && cd asta
python3.13 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/playwright install chromium          # Teams / Outlook
(cd whatsapp && npm install)                   # WhatsApp bridge
cp .env.example .env                           # every setting is documented there
.venv/bin/python -m uvicorn app.main:app --port 8321
```

Open http://localhost:8321 and log in with `ASTA_TOKEN` from `.env`.

Keep it running in the background (starts at login, restarts on crash):

```bash
sh deploy/install.sh
```

## Try

| Say | Asta |
|---|---|
| `implement ABC-123 in booking` | plans, waits for your yes, implements, hands back a diff |
| `why is the ETA not updating in preprod?` | checks Temporal and Grafana, answers with evidence |
| `anything waiting on me?` | one ranked list across mail, Teams, Jira and CI |
| `make me an excel of my open tasks` | writes the file, checks it, sends it to your phone |
| `remind me tomorrow at 9 to chase the review` | puts it in Reminders and reads it back |
| `warn me if PR 17 isn't merged by 5` | watches it; asks "chase them or leave it?" before 5 |
| `don't include PR reviews in standup — always` | proposes a standing rule; enforced on your yes |
| `status` · `my rules` · `roll back 1` | answered instantly, no model involved |

## Configuration

Everything lives in `.env`; new features are **off by default**.

| Flag | Turns on |
|---|---|
| `ASTA_GRAPH` / `ASTA_GRAPHS` | checkpointed task threads / investigate, follow-through, draft-and-send |
| `ASTA_FRONTDESK` | instant answers from state and standing rules |
| `ASTA_ROUTING` | picks the model by how hard the work is |
| `ASTA_PUSH_BUDGET` | interruptions per day before news waits for the digest |
| `ASTA_EVOLVE` | bounded self-tuning, proved before it is kept |
| `ASTA_APPS` / `ASTA_SCREEN` | app doors / the screen-and-mouse fallback |

**macOS permissions** — apps need *Privacy & Security → Automation* for the Python
that runs Asta; the screen fallback also needs *Accessibility*.

## Development

```bash
.venv/bin/python -m pytest -q                       # unit and integration tests
.venv/bin/python -m app.workworld run --k 4         # scenario bench, every scenario 4×
.venv/bin/python -m app.workworld day               # a simulated working day
```

Tests and the bench run in a sandbox: a temp database, scripted brains, and every
outward door — WhatsApp, Teams, files, voice, your apps — replaced by a recorder.
A reported bug gets a failing scenario before it gets a fix.

## Layout

```
app/            server, agent, capabilities and everything above
app/graph/      checkpointed threads (LangGraph)
app/workworld/  the scenario bench
agents/         task pipelines        skills/   playbooks
tests/          tests + bench scenarios (tests/workworld/*.yaml)
whatsapp/       WhatsApp bridge (Baileys)
ui/             web UI (PWA)
deploy/         launchd install
```

## Docs

- [`docs/DESIGN.md`](docs/DESIGN.md) — how each part works and why
- [`GUIDE.md`](GUIDE.md) — setup from scratch and debugging
- [`.env.example`](.env.example) — every setting
