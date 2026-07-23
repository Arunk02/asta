# Asta — Complete Project Guide

*The single document: what Asta is, every component, what it can do, how to set it
up from scratch, and how to debug it. (README.md is the quick version; this is the
full one.) Last updated 2026-07-19.*

---

## 1. What is Asta

Asta is a personal engineering assistant that runs entirely on this laptop
(`~/help/asta`). It chats over web/WhatsApp/Telegram, answers codebase questions
through the project context **booking-workspace**, reads Jira and Grafana/Temporal via MCP,
implements approved code changes headlessly, runs parallel background workers,
fires reminders/briefings to your phone, watches your CI pipelines, and remembers
what it learns day by day.

**Design principles**
- **Copilot-first orchestration**: day-to-day chat and implementation run on the
  office-paid Copilot CLI (zero personal API tokens). Claude/OpenAI/local are optional.
- **Token discipline**: deterministic jobs use no LLM at all; background jobs prefer
  the free local model; prompt caching + recall-in-prompt keep API turns cheap.
- **You stay in control**: code changes and outgoing Teams messages always wait for
  your explicit approval.

### Architecture

```
   Phone (WhatsApp / Telegram / PWA via Tailscale)      Browser (localhost:8321)
        │                                                    │
        ▼                                                    ▼
  whatsapp/bridge.js (Baileys, :8323)  ──────►  FastAPI server (app/main.py, :8321)
  Telegram long-poll (app/telegram.py) ──────►      │  WS streaming chat + REST + UI
                                                    ▼
                       ┌────────────── brains ──────────────┐
                       │ Copilot CLI (default, office-paid) │
                       │ Claude / OpenAI / LM Studio local  │
                       └───────┬─────────────┬──────────────┘
                               ▼             ▼
        agent tools + MCPs (temporal, grafana, context7, atlassian)
        project context workspace (resolve-task.js → exact files)
        memory engine (markdown + FTS5 recall)
                               │
      ┌── background engines (asyncio loops in the server) ──────────────┐
      │ missions · tasks (parallel workers) · reminders · brief/standup  │
      │ health check · CI watcher · Jira watcher · Teams watcher         │
      │ Teams web bridge (Playwright) · project context drift refresh · digests │
      └── all notify via app/notify.py → WhatsApp + Telegram + UI bell ──┘
```

---

## 2. Component map

| Component | File(s) | What it does |
|---|---|---|
| Server + API + WS chat | `app/main.py` | FastAPI on :8321; routes every channel's turn to the selected brain; hosts UI + graph |
| Agent & persona | `app/agent.py` | PydanticAI agent, model registry, ~30 tools |
| Copilot CLI brain | `app/copilot_cli.py` | Default brain; per-conversation sessions; first-turn capability block; per-turn `[now:]` stamp + memory recall |
| Memory engine | `app/memory.py`, `memory/` | Markdown facts/episodes, FTS5 recall each turn, digests + nightly consolidation on the free local model |
| Workspace tools | `app/workspace_tools.py` | project context `resolve_context` → exact files/lines in booking-workspace (IOM parked — one commented line restores it) |
| Missions | `app/missions.py` | Jira/request → plan → **your approval** → headless implement (`copilot -p`) → Claude verify pass; injects the repo's `.github/agents`+`skills` playbooks |
| Background tasks | `app/tasks.py` | The orchestrator: parallel headless workers (analysis / code / teams_draft); code serialized per workspace; phone notification per result |
| Reminders | `app/reminders.py` | "remind me at 3pm…"; one-shot or daily/weekdays/weekly; overdue-safe |
| Brief & standup | `app/briefing.py` | 08:30 morning brief (deterministic, zero tokens) + 09:15 standup drafted from real git commits; weekdays, 3h grace window |
| Health check | `app/health.py` | 6-hourly probe of every channel/integration; notifies only on new-problem / full-recovery transitions |
| CI watcher | `app/ci_watch.py` | GitHub Actions across booking repos via `gh` CLI (keychain auth); 🔴 fail / 🟢 recovery; silent first-poll baseline |
| Jira | `app/jira.py` | **Primary** Jira path (REST): read + comment + status transition tools, `/api/jira/*` endpoints for the Copilot brain, 5-min change watcher on `JIRA_WATCH_JQL`. Atlassian MCP = optional fallback (Confluence, issue creation) |
| WhatsApp bridge | `whatsapp/bridge.js` | Baileys on :8323; QR pairing; locked to your personal JID |
| Telegram | `app/telegram.py` | Official Bot API; binds via `/start <ASTA_TOKEN>`; chat + notifications |
| Teams web bridge | `app/teams_bridge.py` | Playwright + your stored Teams web session: read chats, send (approved only); session-expiry watcher; **no meeting joins by design** |
| Teams activity | `app/teams_bridge.py` (`read_activity`) | Reads the Teams **activity feed** directly (mentions, replies, missed calls) — works with muted chats / notifications off; 30-min safety-net poll + live on every "any mentions?" ask |
| Presence | `app/presence.py` | HIDIdleTime → is Arun at the laptop? Gates ambient notifications (direct ones ignore it) |
| Outlook mail + calendar | `app/outlook.py` | Same Playwright session as Teams (no Azure AD needed): inbox triage (unread from real people), today's meetings; read-only, 15-min poll |
| macOS banner watcher | `app/msnotify.py` | *Superseded* — needs Full Disk Access; off by default (`TEAMS_WATCHER=0`) |
| Voice | `app/voice.py`, `~/help/voicebox` | Local Voicebox on :17493 (own venv/process, localhost-only, never sees `.env`): Kokoro neural TTS + Whisper, optional cloned voice. Plain HTTP, **not** MCP — speaking is plumbing, not a model decision, so it costs zero tokens. Browser speech is the automatic fallback |
| Notifications | `app/notify.py` | Fan-out: WhatsApp + Telegram + UI bell (each fails independently, never crashes) |
| Context refresh | `app/refresh.py` | project context drift check (10-min git fingerprint + weekly baseline), graph regen |
| MCP loader | `app/mcp_loader.py`, `mcp.json` | temporal, grafana (40 tools, deferred), context7, atlassian (OAuth), github (needs PAT) |
| Skills | `app/skills.py` | On-demand playbooks (e.g. grafana-analyser) — loaded only when needed, saves tokens |
| Tracing | `traces` table, `app/store.py` | Every turn: latency, tokens, tools, errors; Settings → Performance card |
| Store | `app/store.py` | SQLite (`data/asta.db`): conversations, tasks, missions, reminders, notifications, traces, kv, memory FTS |
| UI | `ui/` | PWA chat + Missions/Tasks + Graph + Memory + Settings tabs; voice in/out, wake word |
| Always-on | `deploy/` | launchd plists (`com.asta.server` under `caffeinate -si`, `com.asta.whatsapp`) |

---

## 3. What you can ask it (capability catalog)

**From any channel — web UI, WhatsApp, Telegram:**

| Say | What happens |
|---|---|
| "why is invoice dispatch email failing?" | project context resolves exact files → reads only those → answers with citations |
| "any failed temporal workflows?" / "grafana errors in booking?" | live MCP calls (grafana follows the analyser skill's query discipline) |
| "my open tickets" / "show BEPTELIKOS-1234" | Jira read (REST — works with every brain) |
| "comment on BEPTELIKOS-1234: fixed in PR #12" / "move it to Ready for Retest" | Jira write — exact text/status confirmed with you before posting |
| "implement BEPTELIKOS-1234 in booking" | Mission: plan drafted → you approve → headless implement per your org's project context playbooks → Claude verify → notified |
| "approve mission 3" / "reject mission 3" | from your phone, mid-commute |
| "delegate a task to analyze X" | parallel background worker; chat stays free; result pushed to phone |
| "draft a Teams reply to Vinish saying …" | draft task → held for your approval → "approve task N" sends it |
| "read my Teams chat with Vinish" | Playwright bridge reads via your web session |
| "any mentions / anything for me?" | Teams activity feed, read live (pre-fetched in Python — works even when Copilot's shell is blocked) |
| "any mail needing my attention?" / "what meetings today?" | Outlook web, read live via the same session |
| "remind me at 15:00 to reply to Vinish" / "…every weekday at 9" | fires on phone + UI |
| "morning brief" / "standup" | on-demand run of the daily digests |
| "health check" / "ci status" | integration health / recent pipeline runs |
| "task 5 result" / "what's running?" | background work visibility |
| "remember: X" | durable memory fact |
| "why was that slow?" | reads its own traces (`trace_report`) |
| "summarize this transcript: …" | meeting recap — **on demand only, never proactive** |

**Things that come to you (no asking):** morning brief (08:30 wd), standup draft
(09:15 wd), CI failures/recoveries (10-min poll), Jira changes (5-min), Teams/Outlook
mentions/DMs (Teams activity feed — DIRECT, always immediate), mail from people (Outlook —
DIRECT), CI for YOUR OWN runs only (ambient: held while you're at the laptop, released when
you step away),
Teams-session expiry, health transitions, project context drift,
reminder fires, every mission/task completion.

---

## 4. Setup from scratch

Everything is already installed on this machine; this section is for rebuild/reference.

### 4.1 Core

```bash
cd ~/help/asta
python3.13 -m venv .venv && .venv/bin/pip install -e .   # deps in pyproject.toml
.venv/bin/playwright install chromium                     # Teams bridge
cd whatsapp && npm install                                # Baileys bridge
```

Dev run (or use the `.claude/launch.json` names "asta" / "whatsapp-bridge"):
```bash
.venv/bin/python -m uvicorn app.main:app --port 8321      # server
node whatsapp/bridge.js                                   # WA bridge, :8323
```
Open http://localhost:8321 → log in with `ASTA_TOKEN` from `.env`.

### 4.2 One-time connections (each independent — do in any order)

1. **WhatsApp**: pair the **second (dedicated) number** — open ⚙ Settings, scan the QR
   from that phone's WhatsApp → Linked devices. `whatsapp/config.json` already locks
   the bot to your personal number's chat. *Warm a fresh number up a few days before
   heavy use (ban risk); keep the SIM alive.*
2. **Telegram** (recommended, zero ban risk): @BotFather → `/newbot` → token into
   `TELEGRAM_BOT_TOKEN` in `.env` → restart → open your bot, send
   `/start <ASTA_TOKEN>`. Strangers are ignored.
3. **Teams bridge**: `.venv/bin/python -m app.teams_bridge login` → complete Maersk
   SSO in the window. Session lives in `data/teams_profile/`; you're notified when
   it expires (every few weeks).
4. **CI watcher**: `gh` CLI must be authed — **already is** (keychain). If ever not:
   `gh auth login`.
5. **Jira REST + watcher**: fill `JIRA_BASE_URL` + `JIRA_API_TOKEN`
   (id.atlassian.com → Security → API tokens). Or richer: Atlassian MCP OAuth —
   `.venv/bin/python -m app.mcp_login atlassian`.
6. **Teams/Outlook mention watcher**: grant the server Full Disk Access
   (System Settings → Privacy) — `TEAMS_WATCHER=1` is already set.
7. **LM Studio** (free background brain): load a model with
   `lms load qwen/qwen3-14b --context-length 16384` — **default 4096 ctx is too
   small** for the agent prompt and errors with "n_keep >= n_ctx".

### 4.3 Always-on + phone

```bash
sh deploy/install.sh        # launchd: start at login, restart on crash, screen-off OK
```
Stop dev instances first (same ports). Limits: lid-closed **on battery** force-sleeps
(physics; keep it on AC). Nightly memory consolidation via crontab:
`30 2 * * * cd ~/help/asta && .venv/bin/python -m app.memory consolidate >> data/consolidate.log 2>&1`

Phone access: Tailscale on Mac + phone → set `ASTA_HOST=<tailscale-ip>` → open
`http://<ip>:8321` on the phone → Add to Home Screen (PWA).

### 4.4 `.env` reference

| Var | Meaning (blank = feature off / default) |
|---|---|
| `ASTA_TOKEN` | auth for UI/API/bridges — everything requires it |
| `ASSISTANT_NAME` | display + wake word (Asta) |
| `ASTA_HOST` / `ASTA_PORT` | bind address (default 127.0.0.1:8321) |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | optional API brains |
| `LMSTUDIO_BASE_URL` | default http://localhost:1234/v1 |
| `COPILOT_CLI_MODEL` | optional model pin — **invalid value makes copilot hang silently** |
| `ASTA_EXECUTOR` | mission executor: copilot (default) / claude |
| `ASTA_TEST_MODEL` | =1 adds a no-LLM test model |
| `JIRA_BASE_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN` / `JIRA_WATCH_JQL` | Jira REST + watcher |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram (chat id normally auto-bound) |
| `TEAMS_BRIDGE` | =1 Playwright Teams read/send |
| `TEAMS_WATCHER` / `TEAMS_WATCH_KEYWORDS` | banner mention watcher |
| `BRIEF_TIME` / `STANDUP_TIME` | HH:MM weekdays; empty disables |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | **deliberately empty** — old PAT leaked in the IDE mcp.json and must be rotated before use; CI watcher does NOT need it (`gh` keychain) |
| `GRAPHIFY_CMD` | graph regeneration command (still unset) |

---

## 5. Debugging guide

### 5.1 First moves, always

```bash
T=$(grep ASTA_TOKEN .env | cut -d= -f2)
curl -s localhost:8321/api/status -H "Authorization: Bearer $T" | python3 -m json.tool
curl -s localhost:8321/api/health -H "Authorization: Bearer $T"   # what's broken
tail -50 data/logs/server.err   # launchd mode (dev mode: the uvicorn terminal)
```
Or just ask Asta: **"health check"**, **"why was that slow?"** (it reads its own traces).

Restart (launchd): `launchctl kickstart -k gui/$(id -u)/com.asta.server` ·
dev: Ctrl-C + rerun uvicorn. DB peek: `sqlite3 data/asta.db '.tables'`.

### 5.2 Symptom → cause → fix

**Chat / brains**

| Symptom | Likely cause → fix |
|---|---|
| Copilot turn hangs forever | invalid `COPILOT_CLI_MODEL` (silent hang) → unset it; or `copilot login` expired |
| "Copilot CLI exited N" | run `copilot -p hi` manually to see the real error; dead `--resume` sessions auto-retry once |
| Copilot doesn't know a new capability (tasks/reminders/Teams) | its session predates the capability block → `sqlite3 data/asta.db "DELETE FROM kv WHERE key LIKE 'copilot_session:%'"` and retry (the block only rides on a session's FIRST turn) |
| Claude/OpenAI turn errors quota/key | expected — auto-falls back to Copilot with a note; check `.env` key if unwanted |
| Local model errors "n_keep >= n_ctx" | LM Studio ctx too small → `lms load <model> --context-length 16384` |
| Turn slow | ask "why was that slow?" or ⚙ Settings → Performance; copilot ≈ 11-20s normal, tool-heavy turns 30-60s |

**Channels**

| Symptom | Likely cause → fix |
|---|---|
| WhatsApp silent | `curl -s localhost:8323/status` — `up:false` → start bridge; `paired:false` → rescan QR (Settings); banned → new number, keep Telegram |
| Telegram silent | status hint in ⚙ Settings: token missing → `.env`; not bound → `/start <token>`; loop crashes are logged server-side |
| Teams read/send fails "session expired" | `.venv/bin/python -m app.teams_bridge login` (expected every few weeks) |
| Teams "search box / message box not found" | Teams web DOM changed → update selector fallbacks in `_find_chat` / `send_message` (`app/teams_bridge.py`); probe with `python -m app.teams_bridge check` |
| Mention watcher silent | needs Full Disk Access + Mac awake; check `teams_watcher` in `/api/status` |
| UI fetch 401 despite valid cookie | `ui/app.js api()` sends `Bearer <localStorage token>` which **overrides** the cookie — log in via the UI (tryLogin), not by hand-setting cookies |

**Background engines**

| Symptom | Likely cause → fix |
|---|---|
| Task stuck "running" | worker died with server restart (state is in-memory) → re-spawn; check `error` via `/api/tasks/<id>` |
| Two code tasks not parallel | by design — per-workspace write lock (analysis runs parallel) |
| Draft sent without approval | cannot happen by design — only `approve` triggers send; check task status history |
| Reminder never fired | `/api/reminders?all=true` — status? `cancelled`? due date wrong (brain mis-parsed)? Server must be running at/after due time (overdue ones fire on next start with a late note) |
| Brief/standup didn't fire | weekends are skipped; >3h late (laptop asleep) is skipped for the day; kv `brief_last_date` shows the guard |
| CI watcher silent | `/api/ci` — `authed:false` → `gh auth login`; first poll is a deliberate silent baseline; only *transitions* notify |
| No notifications anywhere | `sqlite3 data/asta.db "SELECT text FROM notifications ORDER BY id DESC LIMIT 5"` — if rows exist, the channels are down (health check tells you which); if no rows, the source loop crashed → server logs |
| Mission failed | Missions tab → LOG tail (full log: `data/missions/<id>.log`); verify pass demands `VERDICT: PASS` |

**Memory / workspace**

| Symptom | Likely cause → fix |
|---|---|
| Doesn't recall a known fact | `python -m app.memory reindex`; check the fact exists in `memory/facts/`; recall is FTS keyword-based — phrase matters |
| resolve_context empty/wrong | project context drift → "refresh context" in chat or `/api/refresh/booking`; check `.asta-context/` exists in the workspace |
| Graph tab empty | `GRAPHIFY_CMD` unset / graph never generated for that workspace |
| MCP server missing from sidebar | hover its name for the reason (missing binary, empty env var, failed 20s handshake — often VPN for temporal/grafana) |

### 5.3 Known gotchas (hard-won — don't rediscover)

1. `COPILOT_CLI_MODEL` with a stale model name = silent hang (use valid or unset).
2. Capability changes to `_first_turn_context` need `copilot_session:*` kv rows cleared.
3. LM Studio must be loaded with `--context-length 16384`.
4. UI `api()` Bearer-beats-cookie (see table above).
5. Teams selectors: `APP_MARKERS` data-tids distinguish the real app from the
   pre-redirect shell — generic `#app` matches the login page too (false "session ok").
6. Voice/speech APIs need a real browser (Chrome/Safari), not embedded previews;
   one SpeechRecognition instance at a time.
7. Port 8322 is taken by another local service — WA bridge uses 8323.
8. launchd + `caffeinate -si` keeps it alive screen-off **on AC**; lid closed on
   battery force-sleeps, unfixable in software.
9. WhatsApp = unofficial protocol: ban risk lives on the dedicated second number;
   Telegram is the safe channel.
10. `data/teams_profile/` holds corporate session cookies — same sensitivity as a
    browser profile; FileVault on, never commit/copy it.

### 5.4 End-to-end verification checklist (after any big change)

```
□ /api/status — all expected sections, no missing keys
□ /api/health — only known-broken items listed
□ chat turn via web UI (test model, then copilot)
□ chat turn via /api/wa/incoming (real brain, check trace appears in /api/traces)
□ spawn analysis task → completes → notification row
□ reminder due +60s → fires
□ /api/brief/now + /api/standup/now return sane text
□ /api/ci returns runs; server error log clean
```

---

## 6. Security notes

- Everything gated by `ASTA_TOKEN`; server binds localhost (or tailnet IP) only.
- The leaked GitHub PAT (was plaintext in `~/.config/github-copilot/intellij/mcp.json`)
  must be **rotated**; it was never copied into this project. CI uses `gh` keychain auth.
- Teams bridge scope is read+send only; **meeting joins deliberately excluded**
  (participant-list presence + recording consent). Outgoing Teams messages and all
  code changes require your explicit approval.
- Credential logins (Maersk SSO, Atlassian OAuth, `gh auth login`) are always done
  by **you** in a real browser window — Asta never handles your passwords.

## 7. Parked / roadmap

- **IOM workspace** — parked; restore by uncommenting its line in `app/workspace_tools.py`
  (UI option + repos/CI/refresh all follow automatically).
- **Azure AD app registration** (proper Teams/Outlook/Calendar API) — the real
  replacement for the Playwright bridge; needs corporate approval.
- **Always-on Linux box over Tailscale** — the endgame for true 24/7 (no laptop-sleep physics).
- Notification batching, PR pre-review (post-PAT rotation), email triage (post-Azure AD).
