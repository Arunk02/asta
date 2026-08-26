"""Morning brief + standup auto-prep.

Brief (default 08:30, weekdays): one phone message that orients the day —
overnight task/mission results, things awaiting approval, Jira movement,
today's reminders, health problems. Assembled DETERMINISTICALLY (zero LLM
tokens); it's a status report, not prose.

Standup (default 09:15, weekdays): drafts the standup update from yesterday's
real activity (git commits across all workspace repos + finished missions/tasks
+ Jira movement). One Copilot CLI call to phrase it (office-paid); deterministic
fallback if Copilot is down. You edit two words instead of reconstructing your day.

Times: BRIEF_TIME / STANDUP_TIME in .env (HH:MM, empty string disables one).
Both also run on demand: POST /api/brief/now, /api/standup/now, or just ask.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json as _json
import os
import re
import time
from pathlib import Path

from . import copilot_cli, jira, outlook, store, teams_bridge, workspace_tools


def _cfg_time(env: str, default: str) -> dt.time | None:
    raw = os.environ.get(env, default).strip()
    if not raw:
        return None
    h, m = raw.split(":")
    return dt.time(int(h), int(m))


# --- data collection ---------------------------------------------------------

async def _recent_commits(since: str) -> dict[str, list[str]]:
    """repo name -> commit subjects since `since` (git approxidate), across workspaces."""
    out: dict[str, list[str]] = {}
    for ws_root in workspace_tools.WORKSPACES.values():
        for repo in sorted(Path(ws_root).iterdir()):
            if not (repo / ".git").is_dir():
                continue
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git", "-C", str(repo), "log", "--all", f"--since={since}",
                    "--pretty=%h %s", "-n", "30",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                raw, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
                lines = [l for l in raw.decode(errors="replace").splitlines() if l.strip()]
                if lines:
                    out[repo.name] = lines
            except Exception:
                continue
    return out


def _finished_since(rows: list[dict], key: str, cutoff: float) -> list[dict]:
    return [r for r in rows if (r.get(key) or 0) >= cutoff]


async def _jira_recent() -> list[str]:
    if not jira.configured():
        return []
    try:
        issues = await jira.search(
            "assignee = currentUser() AND updated >= -1d ORDER BY updated DESC", limit=8)
        return [f"{i['key']} [{i['status']}] {i['summary']}" for i in issues]
    except Exception as exc:
        return [f"(Jira query failed: {str(exc)[:60]})"]


async def _jira_open() -> list[str]:
    """Everything still on Arun's plate — his open, not-done issues."""
    if not jira.configured():
        return []
    try:
        issues = await jira.search(
            "assignee = currentUser() AND statusCategory != Done ORDER BY priority ASC, updated DESC",
            limit=10)
        return [f"{i['key']} [{i['status']}] {i['summary']}" for i in issues]
    except Exception as exc:
        return [f"(Jira query failed: {str(exc)[:60]})"]


async def _outlook_bits() -> tuple[list[str], list[str]]:
    """(today's meetings, mail needing a human reply) — empty if Outlook is unavailable.

    One browser session serves both; failures are swallowed so a Teams/Outlook
    hiccup can never stop the brief from going out.
    """
    if not (teams_bridge.enabled() and teams_bridge.logged_in_once()
            and store.kv_get("teams_session_ok") != "0"):
        return [], []
    meetings: list[str] = []
    mails: list[str] = []
    try:
        meetings = await outlook.todays_meetings()
    except Exception:
        pass
    try:
        mails = [outlook.fmt_mail(m, context=True)
                 for m in outlook.needs_attention(await outlook.read_mail(25))]
    except Exception:
        pass
    return meetings, mails


# --- morning brief -----------------------------------------------------------

async def morning_brief() -> str:
    day = dt.date.today().strftime("%a %d %b")
    cutoff = time.time() - 24 * 3600
    parts = [f"☀️ Morning brief — {day}"]

    done_tasks = _finished_since(store.list_tasks(), "finished_at", cutoff)
    done_missions = [m for m in store.list_missions()
                     if m["status"] in ("done", "failed") and m["updated_at"] >= cutoff]
    if done_tasks or done_missions:
        lines = [f"• task #{t['id']} {t['title']} — {t['status']}" for t in done_tasks]
        lines += [f"• mission #{m['id']} {m['title']} — {m['status']}" for m in done_missions]
        parts.append("Finished in the last 24h:\n" + "\n".join(lines))

    waiting = [f"• task #{t['id']} {t['title']} (Teams draft for {t['teams_chat']})"
               for t in store.list_tasks() if t["status"] == "awaiting_approval"]
    waiting += [f"• mission #{m['id']} {m['title']}"
                for m in store.list_missions() if m["status"] == "awaiting_approval"]
    if waiting:
        parts.append("⏳ Waiting on YOU:\n" + "\n".join(waiting))

    jira_lines = await _jira_recent()
    if jira_lines:
        parts.append("Jira moved (24h):\n" + "\n".join("• " + l for l in jira_lines))

    open_lines = await _jira_open()
    if open_lines:
        parts.append("📋 On your plate today (open Jira):\n" + "\n".join("• " + l for l in open_lines))

    meetings, mails = await _outlook_bits()
    if meetings:
        parts.append("📅 Today's meetings:\n" + "\n".join("• " + m for m in meetings))
        # A double-booking is invisible until the day: both invites were accepted
        # at different moments, and the collision usually surfaces about four
        # minutes beforehand. The brief is the last useful place to say it.
        from . import agenda
        if agenda.enabled():
            try:
                warnings = agenda.day_warnings(await _cached_meetings())
            except Exception:
                warnings = []
            if warnings:
                parts.append("\n".join(warnings))
    if mails:
        parts.append("📧 Mail that looks like it needs you:\n" + "\n".join("• " + m for m in mails))

    todays = [r for r in store.list_reminders()
              if dt.date.fromtimestamp(r["due_at"]) == dt.date.today()]
    if todays:
        parts.append("Today's reminders:\n" + "\n".join(
            f"• {dt.datetime.fromtimestamp(r['due_at']).strftime('%H:%M')} {r['text']}"
            for r in todays))

    problems = _json.loads(store.kv_get("health_problems") or "[]")
    if problems:
        parts.append("🩺 Needs attention: " + ", ".join(problems))

    if len(parts) == 1:
        parts.append("Quiet night — nothing finished, nothing waiting, no reminders today.")
    return "\n\n".join(parts)


# --- standup -----------------------------------------------------------------

async def standup_draft() -> str:
    commits = await _recent_commits("yesterday.midnight")
    cutoff = time.time() - 24 * 3600
    done = [f"task: {t['title']} ({t['status']})"
            for t in _finished_since(store.list_tasks(), "finished_at", cutoff)]
    done += [f"mission: {m['title']} ({m['status']})"
             for m in store.list_missions()
             if m["status"] in ("done", "failed") and m["updated_at"] >= cutoff]
    jira_lines = await _jira_recent()

    facts = []
    for repo, lines in commits.items():
        facts.append(f"{repo} commits:\n" + "\n".join("  " + l for l in lines[:10]))
    if done:
        facts.append("Assistant work finished:\n" + "\n".join("  " + d for d in done))
    if jira_lines:
        facts.append("Jira updated:\n" + "\n".join("  " + l for l in jira_lines))
    if not facts:
        return ("Standup draft: no commits, finished work or Jira movement since "
                "yesterday — say what you worked on and I'll phrase it.")

    raw = "\n\n".join(facts)
    try:
        return await copilot_cli.one_shot(
            "Draft a concise daily standup update (plain text, three sections: "
            "Yesterday / Today / Blockers) from this real activity log. Group related "
            "commits, drop noise (merge commits, version bumps). For 'Today' infer "
            "likely continuations; for 'Blockers' write 'none' unless the log shows "
            "one. Max 10 lines total. Output only the standup.\n\n" + raw,
            timeout=120)
    except Exception:
        return "Standup draft (raw activity — Copilot unavailable):\n\n" + raw[:1500]


# --- pre-meeting heads-up ----------------------------------------------------

# Meetings whose whole point is that Arun says something — worth arriving with
# a draft rather than a reminder.
_SPEAKING_MEETING = re.compile(
    r"\b(stand[- ]?up|scrum|daily|sync|retro(spective)?|refinement|grooming|"
    r"planning|review|demo|1[:-]?1|one[- ]on[- ]one|catch[- ]?up|weekly)\b", re.I)

PREMEET_MINUTES = int(os.environ.get("ASTA_PREMEET_MINUTES", "30"))
_MEET_CACHE_TTL = 900     # scraping the calendar spins a browser; don't do it per tick


async def _cached_meetings() -> list[dict]:
    """Today's meetings, cached — the scrape costs a Playwright session (~20s),
    so a 60s watcher tick must not trigger one every time."""
    raw = store.kv_get("meet_cache")
    stamp = float(store.kv_get("meet_cache_at") or 0)
    today = dt.date.today().isoformat()
    if raw and time.time() - stamp < _MEET_CACHE_TTL:
        try:
            cached = _json.loads(raw)
            if cached.get("date") == today:
                return cached["events"]
        except (ValueError, KeyError):
            pass
    events = await outlook.todays_meetings(structured=True)
    store.kv_set("meet_cache", _json.dumps({"date": today, "events": events}))
    store.kv_set("meet_cache_at", str(time.time()))
    # Free, objective, and already in hand: the people he actually sits in
    # meetings with. It is the one thing known about a person before any learning
    # has happened, and it is what stops a colleague ever being auto-muted.
    from . import contacts
    contacts.seed_from_meetings(events)
    return events


async def premeeting_loop() -> None:
    """Ping ~30 min before each meeting.

    Speaking meetings (standup, sync, 1:1) arrive with a draft already written —
    that's the point of the heads-up. Everything else just asks, because
    pre-writing prep for a meeting that needs none is wasted tokens.
    """
    from . import notify
    while True:
        try:
            now = dt.datetime.now()
            if now.weekday() < 5:
                nowmin = now.hour * 60 + now.minute
                from . import agenda
                for ev in await _cached_meetings():
                    lead = ev["minutes"] - nowmin
                    # A 1:1 needs a moment to gather a thought, not half an hour of
                    # runway; a broadcast needs a nudge and no prep at all. One
                    # fixed lead treated them identically.
                    want = agenda.lead_minutes(ev, PREMEET_MINUTES) if agenda.enabled() \
                        else PREMEET_MINUTES
                    # One tick's worth of window, so a ping can't be missed or doubled.
                    if not (want - 2 <= lead <= want + 2):
                        continue
                    # Advisory only: a meeting Asta guessed was optional is the one
                    # mistake here he finds out about by missing it, so this moves
                    # the ping from interrupting to ambient and never suppresses it.
                    needed, why = agenda.attendance(ev) if agenda.enabled() else (True, "")
                    urgency = "direct" if needed else "ambient"
                    key = f"premeet:{now.date().isoformat()}:{ev['start']}:{ev['title'][:30]}"
                    if store.kv_get(key):
                        continue
                    store.kv_set(key, "1")
                    who = f" (by {ev['organizer']})" if ev["organizer"] else ""
                    head = f"📅 In {lead} min — {ev['title']}{who}, {ev['start']}"
                    if why:
                        head += f"\n   ↳ you may not need this one — {why}"
                    if _SPEAKING_MEETING.search(ev["title"]):
                        # Standups get the standup draft; a 1:1/sync/review gets prep
                        # specific to THAT meeting (talking points, watch-outs).
                        if re.search(r"stand[- ]?up|scrum|daily", ev["title"], re.I):
                            draft = await standup_draft()
                        else:
                            from . import agent as agent_mod
                            draft = await agent_mod.meeting_prep(ev["title"])
                        # No draft means no draft. Announcing "📝 Draft for it:"
                        # above nothing is the worst of both: it promises content,
                        # delivers none, and still costs him the read.
                        body = (draft or "").strip()
                        await notify.notify(
                            f"{head}\n\n📝 Draft for it:\n\n{body}" if body
                            else f"{head}\n\nNo prep drafted — say the word and I'll "
                                 f"pull it together now.",
                            "premeeting", urgency=urgency)
                    else:
                        await notify.notify(
                            f"{head}\n\nWant me to prep anything for it? "
                            "Reply with what you need and I'll pull it together.",
                            "premeeting", urgency=urgency)
        except Exception:
            pass
        await asyncio.sleep(60)


# --- scheduler ---------------------------------------------------------------

async def scheduler_loop() -> None:
    from . import notify
    while True:
        try:
            now = dt.datetime.now()
            if now.weekday() < 5:  # weekdays only
                for env, default, kv_key, fn, label in (
                    ("BRIEF_TIME", "08:30", "brief_last_date", morning_brief, "brief"),
                    ("STANDUP_TIME", "09:15", "standup_last_date", standup_draft, "standup"),
                ):
                    t = _cfg_time(env, default)
                    if t is None:
                        continue
                    if store.kv_get(kv_key) == now.date().isoformat():
                        continue
                    due = dt.datetime.combine(now.date(), t)
                    if now < due:
                        continue
                    store.kv_set(kv_key, now.date().isoformat())  # guard before the slow part
                    # Fire late (laptop was asleep) within a 3h grace window; past
                    # that it's not "morning" anymore — skip today silently.
                    if now - due > dt.timedelta(hours=3):
                        continue
                    text = await fn()
                    if label == "standup":
                        text = "🧍 Standup draft:\n\n" + text
                    await notify.notify(text, label)
        except Exception:
            pass
        await asyncio.sleep(60)
