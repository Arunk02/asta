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
