# Asta (né Asta) — personal dev assistant

Multi-LLM chatbot that runs on this laptop: streams over WebSocket, calls your MCP
servers (temporal, grafana, github, context7), answers codebase questions through the
project context workspace (`~/booking-workspace` — IOM parked for now, one commented line in
workspace_tools.py restores it), shows the graphfy view,
and keeps a persistent day-by-day memory.

Also: Jira reading + change notifications, **missions** (Jira ticket → drafted plan →
your approval → headless implementation via Copilot/Claude CLI → Claude test pass),
voice in/out, a WhatsApp bridge, and daily auto-refresh of project context context + graph.

## Run it

```bash
cd ~/help/asta
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8321
```

Open http://localhost:8321 and log in with `ASTA_TOKEN` from `.env`.

## Always-on (runs with the screen off / auto-restarts)

```bash
sh deploy/install.sh        # installs launchd services (remove: sh deploy/install.sh remove)
```

Installs `com.asta.server` + `com.asta.whatsapp` as user LaunchAgents: start at login,
auto-restart on crash, logs in `data/logs/`. The server runs under `caffeinate -si`,
which **prevents system sleep while on AC power** — so screen off / locked / lid open
it keeps working. Hard physics limit: **lid closed on battery = macOS force-sleeps;
no software can prevent that.** Keep it plugged in for lid-closed operation, and
optionally enable System Settings → Battery → Options → "Prevent automatic sleeping
on power adapter when the display is off".

**Why not Docker?** Docker Desktop containers pause when the Mac sleeps — it solves
nothing for the sleep problem, and it complicates everything else: the stdio MCP
proxies are host Python scripts behind the your organisation VPN, LM Studio serves on the host,
`copilot`/`claude` CLIs are host binaries with host auth. Docker earns its keep only
if Asta later moves to an always-on Linux box (mini-PC/NAS/VM) reached over Tailscale
— that is the real "always available" endgame; revisit then.

## Performance & token tracing

Every turn is traced (model, first-token + total latency, tokens in/out/cached, prompt
sizes, tools used, errors) into the `traces` table — view it in **⚙ Settings →
Performance / token trace**, query `/api/traces`, or just ask Asta ("why was that last
answer slow?") — it has a `trace_report` tool to read its own telemetry.

## Models — Copilot-first orchestration

**Copilot CLI (office)** is the default day-to-day brain: chat turns run through
`copilot -p` on the office subscription (currently backed by Claude Sonnet 5!), with
per-conversation continuity via `--session-id/--resume`. Zero personal API cost.
Asta is the orchestrator: it plans, remembers, notifies, and delegates the heavy
lifting (chat + mission implementation) to Copilot; the Claude API is used when you
select it — and if it ever hits a quota/credit error mid-conversation, the turn is
**automatically re-routed to Copilot CLI** with a note in the chat.

Optional extras in `.env`:

- `ANTHROPIC_API_KEY` → enables **Claude** (prompt caching on; used for mission verify pass)
- `OPENAI_API_KEY` → enables **OpenAI**
- **Local**: start LM Studio — auto-detected; powers free background jobs
  (digests, consolidation, compaction).

Models without a key show as "(off)" in the picker. `ASTA_TEST_MODEL=1` adds a
no-LLM test model for pipeline debugging.

## MCP servers

`mcp.json` uses the standard `mcpServers` shape (same as Claude Desktop / Cursor).
Adding a future MCP (db, diff-logs, …) is one JSON entry — no code. Rules:

- a server whose binary is missing is skipped, never fatal;
- a server with an empty required env var (e.g. `GITHUB_PERSONAL_ACCESS_TOKEN`) is skipped;
- at startup every server gets a 20s handshake probe — dead ones are dropped and the
  sidebar shows why (hover the name).

**Security TODO:** rotate the GitHub PAT that was sitting in plaintext in
`~/.config/github-copilot/intellij/mcp.json`, put the new one in `.env` here, and
remove it from the IDE file (use `${GITHUB_PERSONAL_ACCESS_TOKEN}` there too if the
IDE supports it).

**Atlassian MCP (Jira/Confluence)** — the same remote MCP your IDE uses, with OAuth.
One-time login (opens your browser for the Atlassian/your organisation's SSO consent):

```bash
cd ~/help/asta && .venv/bin/python -m app.mcp_login atlassian
```

Tokens persist under `data/oauth/atlassian/`; restart Asta afterwards and the
sidebar shows atlassian with its tool count. The agent then reads Jira through
`atlassian_*` tools directly (the REST `JIRA_API_TOKEN` config remains an optional
fallback and is still what the 5-minute change watcher uses).

## Workspaces / graphfy

Pick `booking` in the top bar; the agent then has:

- `resolve_context(workspace, task)` → project context's resolve-task.js, returns exact files/lines
- `read_workspace_file`, `list_services`

The **Graph** tab embeds `.asta-context/graph/*/graph.html` (workspace-wide + per-service).
(IOM workspace is parked — uncomment its line in app/workspace_tools.py to bring it back.)

## Memory

- `memory/MEMORY.md` — tiny index, always in the system prompt
- `memory/facts/*.md` — durable facts (the agent's `remember` tool writes here; you can
  edit/delete these files by hand, they're plain markdown)
- `memory/episodes/*.md` — session digests (auto-written when a chat goes idle 30 min)
- Recall is automatic per message (SQLite FTS5 over the markdown).

Nightly consolidation (merge dupes, prune, rewrite index) — add to crontab
(`crontab -e`):

```cron
30 2 * * * cd /Users/arun.k.k/help/asta && .venv/bin/python -m app.memory consolidate >> data/consolidate.log 2>&1
```

## Phone access (Tailscale)

1. Install Tailscale on the Mac (`brew install --cask tailscale`) and on your phone;
   sign both into the same tailnet.
2. `tailscale ip -4` on the Mac → e.g. `100.101.102.103`.
3. In `.env` set `ASTA_HOST=100.101.102.103` (or run uvicorn with `--host 0.0.0.0`
   **only** if you understand the exposure; the tailnet IP is safer) and restart.
4. On the phone open `http://100.101.102.103:8321`, log in with the token,
   then **Add to Home Screen** — the PWA manifest makes it install like an app.

The token cookie lasts 90 days; everything is protected by `ASTA_TOKEN`.

## Jira

Set `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` in `.env` (token from
id.atlassian.com → Security → API tokens). Then in chat: "my open tickets",
"show ABC-123". A watcher polls `JIRA_WATCH_JQL` every 5 min and notifies you
(bell + WhatsApp) on status changes / new assignments.

## Missions (ticket → plan → approve → implement → test)

Say "implement ABC-123 in booking" in chat (or WhatsApp, or the Missions tab):

1. Asta pulls the Jira issue + project context context and drafts a plan;
2. you get a notification; review the plan in the Missions tab (or reply
   "approve mission N" from WhatsApp);
3. on approval the executor implements **headlessly in the repo** —
   `copilot -p … --allow-all-tools` (default, your Copilot subscription) or
   `claude -p … --permission-mode acceptEdits` (`ASTA_EXECUTOR` in .env);
4. an independent **Claude verify pass** reviews the diff and runs tests
   (`VERDICT: PASS/FAIL`), and you're notified. Review the final diff in IntelliJ.

Note: IntelliJ itself can't be remote-controlled reliably — the missions system
drives the same Copilot brain via its CLI instead, which is scriptable and monitorable.

## Background tasks (the orchestrator layer)

Asta can spawn parallel background workers mid-chat, so the main conversation's
context is never blocked or polluted — say "delegate a task to …" (works from
web, WhatsApp or Telegram). Each worker is its own headless `copilot -p` process
with a self-contained prompt; you get a WhatsApp/Telegram/UI notification when
it finishes. See them in the Missions tab → Background tasks.

Kinds:
- **analysis** — read-only investigation/summarization; runs fully in parallel.
- **code** — edits code in a workspace; serialized per workspace (one writer at
  a time, so parallel tasks can't fight over git state).
- **teams_draft** — drafts a Teams reply (set the target chat). The draft is
  **never sent automatically**: you approve it from the UI, or reply
  "approve task N" / "reject task N" from your phone.

API: `GET/POST /api/tasks`, `POST /api/tasks/{id}/approve|reject` (bearer auth).
Both brains know how to use it: the PydanticAI models via the `delegate_task` /
`list_background_tasks` / `task_result` / `approve_task` tools, the Copilot CLI
brain via the capability block it gets on each fresh session.

## Daily rhythm: reminders, brief, standup, health, CI

- **Reminders** — "remind me at 3pm to reply to Vinish" (any channel). One-shot or
  daily/weekdays/weekly; fires to WhatsApp/Telegram/UI; overdue ones (laptop asleep)
  fire on wake with a "was due N min ago" note.
- **Morning brief** (`BRIEF_TIME=08:30`, weekdays) — finished tasks/missions, things
  waiting on you, Jira movement, today's reminders, health issues. Deterministic —
  zero LLM tokens. On demand: "morning brief" or `POST /api/brief/now`.
- **Standup draft** (`STANDUP_TIME=09:15`, weekdays) — drafted from yesterday's real
  git commits across all workspace repos + finished work + Jira; one Copilot call.
  Fires late (up to 3h) if the laptop was asleep, skips beyond that.
- **Health check** (6-hourly) — WhatsApp/Telegram/Teams-session/Copilot/LM Studio/disk.
  Notifies only on a NEW problem and once on full recovery — no repeat nagging.
- **CI watcher** (10-min poll, `gh` CLI auth from the keychain — no PAT in .env) —
  watches every workspace repo's GitHub Actions: 🔴 on failure, 🟢 on recovery,
  silent baseline on first run. On demand: "ci status" or `GET /api/ci`.
- Missions and code-tasks automatically point their executor at the workspace's own
  `.github/agents` + `.github/skills` (project context playbooks) so implementation and
  unit/component tests follow your org's conventions.
- Meeting recaps: on demand only — paste a transcript and ask.

## Voice

- 🎙 dictates one message (browser speech recognition, auto-sends on pause);
  the 🔊 toggle speaks replies aloud.
- 🎧 **voice conversation mode** — fully hands-free back-and-forth: it listens,
  sends when you pause, speaks the reply, then automatically listens again for
  your follow-up. Same agent, same memory and tools, so it's a real conversation,
  not one-shot dictation. Ends via the banner button, the 🎧 toggle, or after
  ~4 silent rounds. Works in Chrome/Edge/Safari (incl. the phone PWA); speech
  APIs are blocked inside embedded preview panes.

## WhatsApp

```bash
cd whatsapp && npm start     # then open UI → ⚙ Settings to pair
```

**Use a second (dedicated) number as the bot account** — your personal account
carries zero ban risk that way. Pair by scanning the QR in **⚙ Settings** from
the *second* number's WhatsApp; set "allowed JID" to your personal number
(`91...@s.whatsapp.net`). Then your personal WhatsApp just chats with Asta like
a normal contact. Tips: use the new account manually for a few days before
pairing (fresh accounts get banned faster), keep the SIM alive for re-verification.
Unofficial protocol (Baileys) — the residual ban risk sits on the throwaway
number only. Bridge listens on 127.0.0.1:8323.

## Telegram (official API — recommended channel)

Zero ban risk, works from anywhere. Setup (~2 min): message **@BotFather** →
`/newbot`, put the token in `.env` as `TELEGRAM_BOT_TOKEN`, restart Asta, open
your bot in Telegram and send `/start <ASTA_TOKEN>` (binds it to your chat —
strangers are ignored). Full chat + all notifications, same brain and memory
as the web UI. Status shows in ⚙ Settings.

## Teams bridge (read + send, no Azure AD)

`app/teams_bridge.py` drives **Teams web** through a Playwright browser profile
holding your session. One-time login (you complete the your organisation's SSO yourself):

```bash
.venv/bin/python -m app.teams_bridge login
```

Then in chat: "read my Teams chat with Vinish", "send Vinish: running late".
Deterministic automation — no LLM tokens unless you ask Asta to reason about
what it read. Sessions expire on your organisation's token policy (every few weeks); Asta
notifies you to re-login. **Deliberately no meeting-join**: your name would show
in the participant list while you're absent — use Teams recording/recap and ask
Asta to summarize the transcript instead. Note the profile dir
(`data/teams_profile/`) holds corporate session cookies — same exposure class as
your Edge profile; keep FileVault on.

## Teams / Outlook mentions (no M365 API)

`app/msnotify.py` watches the **macOS Notification Center database** for Teams and
Outlook banners that mention you and raises a Asta notification (bell + WhatsApp).
Pure-Python filtering — notification text never reaches an LLM, so token cost is zero.

Off by default. To enable: grant **Full Disk Access** to the terminal/Python running
Asta, set `TEAMS_WATCHER=1` (and optionally `TEAMS_WATCH_KEYWORDS=arun,mentioned`)
in `.env`, keep Teams/Outlook banners on in System Settings, restart. Status shows
in the ⚙ Settings tab. Limits: it only sees what macOS shows as a banner (muted
chats are invisible) and only the preview text; it can't reply in Teams. The real
long-term fix is a Graph API app registration (phase 6 below).

## Auto-refresh of context + graph

Follows the project context-workspace skill's model: **detection is free** (`check-drift.js`
diffs `verified_against..HEAD` — deterministic node, no LLM), **enrichment costs
tokens** (the evolution loop rewriting mini-skills) and is never run automatically.

- **Change-triggered** (primary): a 10-min git fingerprint (pure `git rev-parse` /
  `status`, zero tokens) fires a refresh only when a repo got new commits or 40+
  dirty files — i.e. a feature actually landed.
- **Weekly baseline**: every `REFRESH_EVERY_DAYS` (default 7) at `REFRESH_AT`, as a
  safety net. Set `REFRESH_EVERY_DAYS=0` to disable the baseline entirely.
- **Quiet when clean**: notifications only on drift or failure; when drift is found
  the notification tells you to say "update the stale context" — the token-costly
  enrichment is always your call.
- Set `GRAPHIFY_CMD` in `.env` to regenerate graphify too ("{workspace}" placeholder).
- On demand: "refresh booking context" in chat, or `POST /api/refresh/booking`.

## Later (phase 6)

- **Teams / Outlook / meetings**: needs an Azure AD app registration in the your organisation
  tenant with Microsoft Graph scopes (Mail.Read, Chat.Read, Calendars.Read, Send).
  When approved, add a Graph MCP server to `mcp.json` and it plugs straight in.
- Telegram companion bot for push notifications.
- Voice in/out (Whisper + TTS).

## Layout

```
app/main.py             FastAPI: WS chat, REST, graph hosting, auth
app/agent.py            PydanticAI agent, model registry, persona
app/memory.py           remember/recall, digests, consolidation, compaction
app/mcp_loader.py       mcp.json -> toolsets (skip/probe logic)
app/workspace_tools.py  project context resolve/read/list + graph pages
app/store.py            SQLite: chats, usage, FTS5 memory index
ui/                     single-page chat UI + PWA
memory/                 MEMORY.md, facts/, episodes/
```
