"""Outlook Web bridge (Playwright) — inbox triage + today's meetings.

Reuses the SAME logged-in Chromium profile as the Teams bridge (one your organisation's SSO
covers both), so there is nothing extra to set up: if Teams works, this works.
That is what makes mail/calendar possible without an Azure AD app registration.

Everything here is READ-ONLY and deterministic (DOM scrape + regex, zero LLM
tokens). Asta never sends, deletes, or replies to mail — drafting a reply is a
separate, explicitly-approved action.

CLI:
    .venv/bin/python -m app.outlook mail [limit]      # recent inbox
    .venv/bin/python -m app.outlook attention         # unread that looks human
    .venv/bin/python -m app.outlook meetings          # today's calendar

Fragility note: same as Teams — Outlook Web DOM changes without notice; the
selectors below fall back and fail loudly rather than silently returning [].
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import re
import sys
import time

from . import store, teams_bridge

MAIL_URL = "https://outlook.office.com/mail/"
CALENDAR_URL = "https://outlook.office.com/calendar/view/day"
MAIL_LIST = '[role="listbox"][aria-label*="Message"]'

POLL_SECONDS_DEFAULT = 900
SEEN_KEY = "outlook_seen_mail"

# Senders that are machine traffic: alerts, digests, no-reply blasts. They are
# still listed by `mail`, just never treated as "needs your attention".
_BULK_SENDER = re.compile(
    r"(no[-_]?reply|donotreply|notification|alerts?@|mailer|newsletter|automated|"
    r"jira@|confluence@|github\.com|servicenow|workplace|benefits|communications|"
    # Social/announcement traffic Arun never actions from a notification.
    r"viva|yammer|engage|sharepoint|onedrive|forms@|bookings@|planner|"
    r"teams@|microsoft|survey|events?@|learning|training)", re.I)

# Some noise arrives from a human-looking sender, so the subject has to be
# checked too — monitoring digests and IOM platform alerts are the usual ones.
_BULK_SUBJECT = re.compile(
    r"(\[(?:alert|alarm|monitor|grafana|prometheus|nagios|dynatrace)\]"
    r"|\b(?:auto[- ]?(?:resolved|recovered|closed)|resolved automatically)\b"
    r"|\bdaily (?:digest|summary|report)\b|\bweekly (?:digest|summary|report)\b"
    r"|\byou (?:were )?mentioned in\b|\bcommunity (?:update|digest)\b"
    r"|\binvitation to (?:join|follow)\b|\bhas (?:posted|shared) in\b)", re.I)

_TIME = re.compile(r"\b(\d{1,2}:\d{2}\s?[AP]M|\d{1,2}/\d{1,2}/\d{2,4}|Yesterday|Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b", re.I)
# Outlook pads rows with avatar initials and private-use-area icon glyphs.
_NOISE_CELL = re.compile(r"^[-\s]*$|^[A-Z]{1,3}$")


def _clean_cells(text: str) -> list[str]:
    out = []
    for cell in text.split("|"):
        c = re.sub(r"[-]", "", cell).strip()
        if c and not _NOISE_CELL.match(c):
            out.append(c)
    return out


async def _open(page, url: str, ready: str, timeout: int = 70) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    for _ in range(timeout // 2):
        if "login" in page.url or "microsoftonline" in page.url:
            raise RuntimeError("SESSION_EXPIRED")
        if await page.query_selector(ready):
            await asyncio.sleep(2)  # let the virtualized list paint
            return
        await asyncio.sleep(2)
    raise RuntimeError(f"Outlook did not load in {timeout}s (url: {page.url[:100]})")


async def read_mail(limit: int = 15) -> list[dict]:
    """Recent inbox rows as {unread, sender, subject, when, important}."""
    async with teams_bridge._lock:
        pw, ctx = await teams_bridge._launch()
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await _open(page, MAIL_URL, MAIL_LIST)
            rows = await page.evaluate(
                """(sel) => {
                    const list = document.querySelector(sel);
                    if (!list) return [];
                    return Array.from(list.querySelectorAll('[role="option"]')).map(r => ({
                        aria: r.getAttribute('aria-label') || '',
                        text: r.innerText.replace(/\\n/g, ' | '),
                    }));
                }""", MAIL_LIST)
            store.kv_set("teams_session_ok", "1")  # shared session proved alive
        finally:
            await ctx.close()
            await pw.stop()

    out: list[dict] = []
    for row in rows[:limit]:
        aria = row["aria"]
        cells = _clean_cells(row["text"])
        when = _TIME.search(aria)
        out.append({
            "unread": "Unread" in aria,
            "important": "Important" in aria,
            "sender": cells[0] if cells else "?",
            "subject": cells[1] if len(cells) > 1 else (cells[0] if cells else "?"),
            "when": when.group(1) if when else "",
            "preview": _preview(aria, when),
        })
    return out


# Outlook's row aria-label is "<flags> <sender> <subject> <date> <body preview…>",
# so the date is the seam: everything after it is real body text. The list DOM
# itself carries no preview cell, but this does — and it's already fetched, so
# subject-only notifications were throwing away the context for free.
# External-sender banners sit BETWEEN the date and the real body, and Outlook
# truncates the aria mid-sentence — so a plain "remove the banner" regex leaves
# fragments like "of this email and know th". Cutting at the LAST banner marker
# instead drops the whole prefix however it was clipped.
_BANNER_END = re.compile(
    r"(?:know (?:th|the content is safe\.?)"
    r"|Learn why this is important"
    r"|recognize the source of this email[^.]*\.?"
    r"|do not click links or open attachments[^.]*\.?"
    r"|sent from outside of your organization\.?"
    r"|CAUTION[^.]*\.)", re.I)
_TRAILING_NOISE = re.compile(r"(No conversations selected|Collapsed|Pinned)\s*$", re.I)
# Outlook pads previews with zero-width joiners/spaces to fix row height.
_INVISIBLE = re.compile(r"[​-‏⁠﻿­]+")


def _preview(aria: str, when_match) -> str:
    """Body snippet from the row's aria-label — the context a subject line lacks."""
    tail = aria[when_match.end():] if when_match else aria
    hits = list(_BANNER_END.finditer(tail))
    if hits:
        tail = tail[hits[-1].end():]
    tail = _TRAILING_NOISE.sub(" ", _INVISIBLE.sub(" ", tail))
    text = " ".join(tail.split())[:220].strip(" -–—|")
    # What survives a clipped banner is usually a fragment, not context. Better
    # to show the subject alone than a confusing half-sentence.
    return text if len(text) >= 25 else ""


# --- transient alerts --------------------------------------------------------
#
# Platform alerts that heal themselves are the worst kind of notification: by the
# time Arun looks, there is nothing to do. Holding them briefly and dropping the
# pair when a recovery lands means only the ones that STAYED broken reach him.
HOLD_MINUTES = int(os.environ.get("ASTA_ALERT_HOLD_MINUTES", "20"))
_HOLD_KEY = "alert_hold"

_ALERTY = re.compile(
    r"\b(alert|alarm|incident|error|failed|failure|down|unhealthy|degraded|"
    r"threshold|breach|timeout|exception|critical|warning)\b", re.I)
_RECOVERY = re.compile(
    r"\b(recover(ed|y)?|resolved|closed|back to normal|healthy again|"
    r"cleared|ok now|restored|no longer)\b", re.I)

# What the alert is ABOUT, so a recovery can be matched to its firing. Ticket and
# incident ids are the reliable join key; otherwise fall back to the subject with
# the volatile parts stripped.
_IDISH = re.compile(r"\b((?:INC|CHG|PRB|ALERT)[0-9]{4,}|[A-Z][A-Z0-9]{1,9}-\d+)\b")


def alert_key(m: dict) -> str:
    subj = m.get("subject", "")
    ids = _IDISH.findall(subj)
    if ids:
        return ids[0].upper()
    core = re.sub(r"\b(re|fw|fwd)\b[: ]*", "", subj, flags=re.I)
    core = _RECOVERY.sub("", core)
    core = re.sub(r"[0-9]{2,}", "", core)          # timestamps, counts, run numbers
    return " ".join(core.lower().split())[:60]


def _load_holds() -> dict:
    try:
        return _json.loads(store.kv_get(_HOLD_KEY) or "{}")
    except ValueError:
        return {}


def triage_alerts(mails: list[dict], now: float | None = None) -> tuple[list[dict], list[str]]:
    """Split alert-class mail into (release now, cancelled as self-healed).

    Firing alerts are parked. A later recovery for the same key drops BOTH — the
    firing never reaches Arun at all, which is the whole point. Anything still
    unrecovered once the window passes is released.
    """
    now = now or time.time()
    holds = _load_holds()
    release, cancelled = [], []

    for m in mails:
        subj = m.get("subject", "")
        if not _ALERTY.search(subj) and not _RECOVERY.search(subj):
            continue
        key = alert_key(m)
        if _RECOVERY.search(subj):
            if key in holds:
                holds.pop(key, None)
                cancelled.append(key)
            else:
                # Recovery for something never held — nothing to tell him about.
                holds[key] = {"first": now, "done": True}
            m["_alert"] = "recovery"
        elif key not in holds:
            holds[key] = {"first": now, "subject": subj[:120]}
            m["_alert"] = "held"

    for key, info in list(holds.items()):
        if info.get("done"):
            if now - info.get("first", now) > 6 * 3600:
                holds.pop(key, None)               # stop the ledger growing
            continue
        if now - info.get("first", now) >= HOLD_MINUTES * 60:
            if not info.get("released"):
                info["released"] = True
                release.append({"subject": info.get("subject", key), "key": key})
        if now - info.get("first", now) > 24 * 3600:
            holds.pop(key, None)

    store.kv_set(_HOLD_KEY, _json.dumps(holds))
    return release, cancelled


def is_alerty(m: dict) -> bool:
    subj = m.get("subject", "")
    return bool(_ALERTY.search(subj) or _RECOVERY.search(subj))


# Arun's own CI failures already reach him via ci_watch's push, which is the
# better channel (it knows the run, the branch and the recovery). The mail copy
# is a duplicate; GitHub sends it as the actor, so the sender is Arun's own name
# and the bulk-sender list never catches it.
_CI_MAIL = re.compile(
    r"(\[[^\]]*/[^\]]*\]\s*Run (failed|cancelled|succeeded)"
    r"|^\s*(Run failed|Run cancelled):"
    r"|github\.com.*(actions|workflow))", re.I)

# ServiceNow incidents assigned to Arun's group are real work, not noise, so they
# are KEPT by default — hiding a live incident is the worse failure. Flip this if
# the L2 queue turns out to be pure noise for him.
SUPPRESS_SERVICENOW = os.environ.get("ASTA_SUPPRESS_SERVICENOW", "0") == "1"
_SERVICENOW = re.compile(r"\b(service ?now|it service desk|incident (INC)?\d+)\b", re.I)


def needs_attention(mails: list[dict]) -> list[dict]:
    """Unread mail from a human — alerts/newsletters/no-reply blasts filtered out."""
    out = []
    for m in mails:
        if not m["unread"]:
            continue
        subj, sender = m.get("subject", ""), m.get("sender", "")
        if _BULK_SENDER.search(sender) or _BULK_SUBJECT.search(subj):
            continue
        if _CI_MAIL.search(subj):
            continue                               # ci_watch already pushed it
        if _SERVICENOW.search(sender + " " + subj):
            # A ticket ASSIGNED to Arun's group is work, not a self-healing blip,
            # so it skips the hold window entirely — the word "Incident" would
            # otherwise make _ALERTY swallow it. Suppress only on explicit opt-in.
            if not SUPPRESS_SERVICENOW:
                out.append(m)
            continue
        if is_alerty(m):
            continue                               # goes through the hold window
        out.append(m)
    return out


def fmt_mail(m: dict, context: bool = False) -> str:
    flag = "🔵 " if m["unread"] else ""
    imp = "❗️" if m["important"] else ""
    when = f" ({m['when']})" if m["when"] else ""
    line = f"{flag}{imp}{m['sender']} — {m['subject']}{when}"
    # Notifications add the gist so Arun can judge "reply now or later" from the
    # phone without opening Outlook; plain listings stay one line each.
    if context and m.get("preview"):
        line += f"\n   ↳ {m['preview'][:180]}"
    return line


async def _todays_events() -> list[dict]:
    """Raw calendar rows scraped from Outlook, parsed into dicts."""
    async with teams_bridge._lock:
        pw, ctx = await teams_bridge._launch()
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await _open(page, CALENDAR_URL, '[role="main"], [aria-label*="calendar view"]')
            await asyncio.sleep(3)
            labels = await page.evaluate(
                """() => Array.from(document.querySelectorAll('[aria-label]'))
                        .map(e => e.getAttribute('aria-label') || '')""")
            store.kv_set("teams_session_ok", "1")
        finally:
            await ctx.close()
            await pw.stop()

    seen: set[str] = set()
    events: list[tuple[str, str]] = []
    for raw in labels:
        lab = raw.strip()
        if not (20 < len(lab) < 300):
            continue
        m = re.match(r"^(.+?),\s*(\d{1,2}:\d{2}\s?[AP]M)\s+to\s+(\d{1,2}:\d{2}\s?[AP]M),\s*(.*)$", lab)
        if not m:
            continue
        title, start, end, tail = m.groups()
        if re.match(r"^\d+\s+events?", title):  # hour-slot aggregate, not a meeting
            continue
        organizer = re.search(r"\bBy ([^,]+)", tail)
        status = re.search(r"\b(Busy|Free|Tentative|Out of office)\b", tail)
        line = f"{start}–{end}  {title.strip()}"
        if organizer:
            line += f" (by {organizer.group(1).strip()})"
        if status and status.group(1) == "Tentative":
            line += " [tentative]"
        key = f"{start}{title}"
        if key not in seen:
            seen.add(key)
            events.append({
                "start": start, "end": end, "line": line,
                "title": title.strip(),
                "organizer": organizer.group(1).strip() if organizer else "",
                "minutes": _clock_minutes(start),
            })

    return sorted(events, key=lambda e: e["minutes"])


def _clock_minutes(s: str) -> int:
    """'2:30 PM' -> minutes since midnight, for ordering and look-ahead."""
    t = re.match(r"(\d{1,2}):(\d{2})\s?([AP])M", re.sub(r"\s+", " ", s).strip(), re.I)
    if not t:
        return 0
    h, mm, ap = int(t.group(1)) % 12, int(t.group(2)), t.group(3).upper()
    return (h + (12 if ap == "P" else 0)) * 60 + mm


async def todays_meetings(structured: bool = False):
    """Today's calendar, earliest first. Formatted lines by default, or dicts
    with start times when a caller needs to reason about *when* — the
    pre-meeting watcher needs that, the brief only needs the text."""
    events = await _todays_events()
    return events if structured else [e["line"] for e in events]


async def watch_loop() -> None:
    """Poll the inbox and push NEW mail that looks like it needs Arun."""
    import os
    from . import notify
    poll = int(os.environ.get("OUTLOOK_POLL", str(POLL_SECONDS_DEFAULT)))
    if poll <= 0:
        return
    while True:
        await asyncio.sleep(poll)
        if not (teams_bridge.enabled() and teams_bridge.logged_in_once()
                and store.kv_get("teams_session_ok") != "0"):
            continue
        try:
            mails = await read_mail(20)
        except Exception:
            continue
        wanted = needs_attention(mails)
        keys = [f"{m['sender']}|{m['subject']}" for m in wanted]
        raw = store.kv_get(SEEN_KEY)
        seen: set[str] | None = set(_json.loads(raw)) if raw else None
        fresh = [] if seen is None else [m for m, k in zip(wanted, keys) if k not in seen]
        allk = keys + [k for k in (seen or set()) if k not in keys]
        store.kv_set(SEEN_KEY, _json.dumps(allk[:300]))
        if fresh:
            # Mail from a real person is addressed to him — goes out immediately
            # (the bulk/alert traffic never reaches here; needs_attention drops it).
            await notify.notify(
                "📧 Outlook — needs you:\n"
                + "\n".join("• " + fmt_mail(m, context=True) for m in fresh[:6]),
                "outlook", urgency="direct")

        # Alerts are held, not sent: only the ones still unrecovered after the
        # window survive. Self-healing IOM noise never reaches the phone at all.
        try:
            release, cancelled = triage_alerts(mails)
        except Exception:
            release, cancelled = [], []
        if release:
            await notify.notify(
                f"🚨 Still broken after {HOLD_MINUTES} min:\n"
                + "\n".join("• " + r["subject"] for r in release[:6]),
                "outlook", urgency="direct")


if __name__ == "__main__":
    store.init()
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if cmd == "mail":
            n = int(sys.argv[2]) if len(sys.argv) > 2 else 15
            for m in asyncio.run(read_mail(n)):
                print(fmt_mail(m))
        elif cmd == "attention":
            for m in needs_attention(asyncio.run(read_mail(25))):
                print(fmt_mail(m))
        elif cmd == "meetings":
            for line in asyncio.run(todays_meetings()):
                print(line)
        else:
            print(__doc__)
            print("Usage: python -m app.outlook mail [limit]|attention|meetings")
    except RuntimeError as exc:
        print(f"ERROR: {'session expired — rerun: python -m app.teams_bridge login' if 'SESSION_EXPIRED' in str(exc) else exc}")
        sys.exit(1)
