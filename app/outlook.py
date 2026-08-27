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

from . import store, teams_bridge, wake

MAIL_URL = "https://outlook.office.com/mail/"
CALENDAR_URL = "https://outlook.office.com/calendar/view/day"
MAIL_LIST = '[role="listbox"][aria-label*="Message"]'

# How stale a notification is allowed to be. The hold window can promise "you hear
# within 5 minutes" only if the inbox is actually read that often — a 15-minute
# poll made every other timing decision in this file decorative.
POLL_SECONDS_DEFAULT = 300
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
#
# But that reasoning does NOT survive contact with a real outage, and holding
# everything for twenty minutes was the wrong conclusion drawn from it. If pods
# are down, twenty minutes of silence is the whole incident — and a late alert on
# a real outage costs far more than an extra ping he can ignore. So:
#
#   something is DOWN          → tell him now, no window at all
#   a warning that may settle  → a short window (5 min), then tell him
#   it recovered               → tell him that too, if he was told it broke
#
# The last line is the part that makes immediacy affordable. Holding existed to
# spare him the "never mind" — reporting the recovery instead means a flap costs
# two messages and he is never late on the one that matters.
HOLD_MINUTES = int(os.environ.get("ASTA_ALERT_HOLD_MINUTES", "5"))
_HOLD_KEY = "alert_hold"

# Breakage that is already real: no window, whatever the rest of the wording says.
# Deliberately narrow — everything here means "a thing that was serving traffic
# has stopped", not "a number moved".
# _CRITICAL moved to attention.looks_critical — one detector, both sources.
# It lived here, mail was its only caller, and the same outage posted in a
# Teams channel was filed as "FYI, nothing needed from you".


def is_critical(m: dict) -> bool:
    """Is this already breakage, rather than something that might settle?

    Checked against the body as well as the subject, because a monitoring subject
    names the channel and the words that matter — "pods down", "connection
    refused" — are in the mail itself.

    The detection itself now lives in `attention.looks_critical`, shared with the
    Teams path. It was here, mail was its only caller, and the result was that an
    outage interrupted him by email and was filed as FYI when it arrived in a
    channel.
    """
    from . import attention
    return attention.looks_critical(m.get("subject", ""), m.get("preview", ""))

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
    """What the alert is ABOUT, so a recovery joins to its own firing.

    The punctuation strip is load-bearing, not tidiness. Removing the recovery
    word from "RESOLVED: Error Reporting :: Booking" leaves a dangling colon, and
    that one character made the recovery key differ from the firing key — so
    nothing was ever cancelled, every self-healing alert survived the hold window,
    and Arun got "still broken after 20 min" about things that had healed in two.
    The whole mechanism was silently inverted by a leftover ":".
    """
    subj = m.get("subject", "")
    ids = _IDISH.findall(subj)
    if ids:
        return ids[0].upper()
    core = re.sub(r"\b(re|fw|fwd)\b[: ]*", "", subj, flags=re.I)
    core = _RECOVERY.sub(" ", core)
    core = re.sub(r"[0-9]{2,}", " ", core)         # timestamps, counts, run numbers
    core = re.sub(r"[^\w\s]+", " ", core)          # ALL punctuation, not some of it
    return " ".join(core.lower().split())[:60]


def _mail_instance(m: dict) -> str:
    """Identity of ONE mail within an incident, for counting without double-count.

    Deliberately not `mail_key`, which strips exactly the things that distinguish
    two mails of the same alert — that is its job for dedup, and the wrong job
    here. The arrival time plus a slice of the body separates "fired again at
    09:19" from the 09:14 mail, while staying identical across the polls that
    re-read the same inbox row every fifteen minutes.
    """
    return f"{m.get('when', '')}|{' '.join((m.get('preview') or '').split())[:80]}"


def _load_holds() -> dict:
    try:
        return _json.loads(store.kv_get(_HOLD_KEY) or "{}")
    except ValueError:
        return {}


# The line in an alert body that actually says what broke. Monitoring subjects
# name the CHANNEL ("Error Reporting :: Booking side work"), never the fault, so
# the subject alone is unactionable — which is exactly the complaint this answers.
# A sentence carrying a number, a status code, a service or an error word is the
# one worth quoting; a greeting or a "view in Grafana" footer is not.
_DIAGNOSTIC = re.compile(
    r"\b\d+(\.\d+)?\s*(%|ms|s|m|req|rps|errors?|failures?|times?)\b"
    r"|\b[45]\d{2}\b"
    r"|\b(threshold|exceeded|breach(ed)?|timeout|timed out|refused|unavailable|"
    r"exception|stack ?trace|null|deadlock|out of memory|oom|5xx|4xx|"
    r"connection|latency|error rate|failed to)\b",
    re.I)
_ALERT_NOISE = re.compile(
    r"^\s*(view (it )?in|open in|click here|unsubscribe|this is an automated|"
    r"do not reply|sent by|powered by)\b", re.I)


def alert_detail(preview: str, limit: int = 160) -> str:
    """The one line from the alert body that says what actually broke.

    Prefers a sentence with real evidence in it — a rate, a status code, a
    threshold, an exception — over the first sentence, because the first sentence
    of a monitoring mail is usually a restatement of the subject he has already
    read. Returns "" when the body carries nothing diagnostic, so the caller can
    say so honestly instead of padding the alert with a footer.
    """
    body = " ".join((preview or "").split())
    if not body:
        return ""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?;])\s+|\s*\|\s*", body) if s.strip()]
    usable = [s for s in sentences if not _ALERT_NOISE.match(s) and len(s) > 8]
    best = next((s for s in usable if _DIAGNOSTIC.search(s)), None) or (
        usable[0] if usable else "")
    if len(best) > limit:
        best = best[:limit].rsplit(" ", 1)[0] + "…"
    return best


def triage_alerts(mails: list[dict], now: float | None = None) -> tuple[list[dict], list[str]]:
    """Split alert-class mail into (release now, cancelled as self-healed).

    Firing alerts are parked. A later recovery for the same key drops BOTH — the
    firing never reaches Arun at all, which is the whole point. Anything still
    unrecovered once the window passes is released.

    What gets PARKED is the evidence, not just the subject. The ledger used to
    keep `subject` alone, so an escalation could only ever repeat a line naming
    the alert channel — "Error Reporting :: Booking side work" — which says
    nothing about what broke. The sender, the diagnostic line and how many times
    it has fired are all already in hand when the mail is read; throwing them away
    and then asking him to open Outlook is the avoidable half of the interruption.
    """
    now = now or time.time()
    holds = _load_holds()
    release, cancelled = [], []

    for m in mails:
        if not goes_to_hold(m):
            continue
        subj = m.get("subject", "")
        key = alert_key(m)
        if _RECOVERY.search(subj):
            info = holds.get(key)
            if info and info.get("released"):
                # He was told this broke. Telling him it is fixed is the other
                # half of that promise — and it is what makes reporting breakage
                # immediately affordable, because "never mind" arrives by itself
                # instead of him having to go and check.
                release.append({
                    "kind": "recovered", "key": key,
                    "subject": info.get("subject", key),
                    "sender": info.get("sender", ""),
                    "detail": alert_detail(m.get("preview", "")),
                    "minutes": int((now - info.get("first", now)) // 60),
                })
                holds[key] = {"first": now, "done": True}
            elif info and not info.get("done"):
                holds.pop(key, None)
                cancelled.append(key)          # healed inside the window: never told
            else:
                # Recovery for something never held — nothing to tell him about.
                holds[key] = {"first": now, "done": True}
            m["_alert"] = "recovery"
            continue
        info = holds.get(key)
        if info is None:
            holds[key] = {
                "first": now, "subject": subj[:120],
                "sender": (m.get("sender") or "")[:60],
                "detail": alert_detail(m.get("preview", "")),
                "seen": [_mail_instance(m)],
                "critical": is_critical(m),
            }
            m["_alert"] = "held"
            continue
        if info.get("done"):
            continue
        # A repeat of an alert already parked. Count it — one mail and fifteen
        # mails in twenty minutes are different problems, and the count is the
        # cheapest signal of which one this is.
        mk = _mail_instance(m)
        seen = info.setdefault("seen", [])
        if mk not in seen:
            seen.append(mk)
            del seen[:-20]
            # A later mail in the same incident usually carries better detail than
            # the first ("now at 40%"), so take it when the first had none.
            if not info.get("detail"):
                info["detail"] = alert_detail(m.get("preview", ""))
            # It started as a warning and has since become an outage. Escalate the
            # remaining wait away rather than making him serve out a window that
            # was set when the situation looked milder.
            if not info.get("critical") and is_critical(m):
                info["critical"] = True
        m["_alert"] = "held"

    for key, info in list(holds.items()):
        if info.get("done"):
            if now - info.get("first", now) > 6 * 3600:
                holds.pop(key, None)               # stop the ledger growing
            continue
        wait = 0 if info.get("critical") else HOLD_MINUTES * 60
        if now - info.get("first", now) >= wait:
            if not info.get("released"):
                info["released"] = True
                release.append({
                    "kind": "broken",
                    "subject": info.get("subject", key), "key": key,
                    "sender": info.get("sender", ""),
                    "detail": info.get("detail", ""),
                    "critical": bool(info.get("critical")),
                    "count": len(info.get("seen") or []) or 1,
                    "minutes": int((now - info.get("first", now)) // 60),
                })
        if now - info.get("first", now) > 24 * 3600:
            holds.pop(key, None)

    store.kv_set(_HOLD_KEY, _json.dumps(holds))
    return release, cancelled


def fmt_alert(r: dict) -> str:
    """One released alert, written so he can decide without opening Outlook.

    Four facts, in the order he needs them: what it is, what actually broke, who
    said so, and how long/how often. The middle one is the whole point — a subject
    line naming a monitoring channel told him nothing, and "still broken" without
    it is an interruption that can only be answered by going and looking.
    """
    lines = [f"• {r['subject']}"]
    detail = (r.get("detail") or "").strip()
    if r.get("kind") == "recovered":
        # No "open Outlook" prompt on a recovery: there is nothing for him to do,
        # and asking him to go and look would undo the point of telling him.
        if detail:
            lines.append(f"  {detail}")
        mins = r.get("minutes", 0)
        lines.append(f"  {r['sender']} · was broken {mins} min" if r.get("sender")
                     else f"  was broken {mins} min")
        return "\n".join(lines)
    lines.append(f"  {detail}" if detail
                 else "  (the mail carries no detail beyond the subject — open Outlook)")
    facts = []
    if r.get("sender"):
        facts.append(r["sender"])
    n = int(r.get("count") or 1)
    facts.append(f"{n} mails" if n > 1 else "1 mail")
    age = r.get("minutes", 0)
    facts.append("just now" if age < 1 else f"first seen {age} min ago")
    lines.append("  " + " · ".join(facts))
    return "\n".join(lines)


def alert_message(release: list[dict]) -> tuple[str, str]:
    """Render a batch of released alerts as (text, urgency).

    Breakage and recovery are separated because they ask different things of him,
    and an outage headline sitting above a "back to normal" line reads as though
    both are still open. Urgency follows the worst thing in the batch: a recovery
    on its own is ambient — it is good news, and good news does not need to
    interrupt.
    """
    broken = [r for r in release if r.get("kind") != "recovered"]
    healed = [r for r in release if r.get("kind") == "recovered"]
    parts = []
    if broken:
        critical = [r for r in broken if r.get("critical")]
        head = ("🚨 Broken now" if critical
                else f"⚠️ Still broken after {HOLD_MINUTES} min")
        if len(broken) > 1:
            head += f" ({len(broken)})"
        parts.append(head + ":\n\n" + "\n\n".join(fmt_alert(r) for r in broken[:6]))
    if healed:
        head = "🟢 Recovered" + (f" ({len(healed)})" if len(healed) > 1 else "")
        parts.append(head + ":\n\n" + "\n\n".join(fmt_alert(r) for r in healed[:6]))
    return "\n\n".join(parts), ("direct" if broken else "ambient")


def is_alerty(m: dict) -> bool:
    """Is this alert-class mail at all?

    Anything CRITICAL counts by definition, and that clause is not redundant: the
    word list below wants "failed" and "failure" but not "failing", so a subject
    reading "P1: all requests failing" matched nothing and never entered the
    pipeline — the single most urgent shape of mail was the one shape that fell
    straight through. Deriving it from the critical set means the two can't
    disagree again.
    """
    subj = m.get("subject", "")
    return bool(_ALERTY.search(subj) or _RECOVERY.search(subj) or is_critical(m))


def goes_to_hold(m: dict) -> bool:
    """Does this mail belong in the hold window at all?

    The one predicate both paths agree on, because the alternative was a mail
    landing in BOTH and being announced twice by different mechanisms. The live
    ledger showed exactly that: `[…/telikos-booking-service] run failed` sitting
    there as a held alert, having already been pushed by the CI watcher — which
    knows the run, the branch and the recovery, and is strictly the better report.
    Same for a ServiceNow incident assigned to his group: `needs_attention` keeps
    it as real work, and the hold window was independently re-announcing it twenty
    minutes later as "still broken".

    Kept next to `needs_attention` and sharing its tests, so the two can never
    drift into disagreeing about who owns a mail.
    """
    subj, sender = m.get("subject", ""), m.get("sender", "")
    if not is_alerty(m):
        return False
    if _CI_MAIL.search(subj):
        return False                    # ci_watch owns this one, with better detail
    if _SERVICENOW.search(sender + " " + subj) and not SUPPRESS_SERVICENOW:
        return False                    # needs_attention keeps it: work, not a blip
    return True


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
        # ServiceNow is checked FIRST because it is the deliberate exception, and
        # an exception tested after the rule that swallows it is dead code. That
        # is what this was: `_BULK_SENDER` matches "servicenow", so every incident
        # was dropped here and the branch below never ran. They still reached him
        # — as a "still broken after 20 min" alert with nothing but a subject,
        # twenty minutes late — which is the worst of both paths.
        if _SERVICENOW.search(sender + " " + subj):
            # A ticket ASSIGNED to Arun's group is work, not a self-healing blip.
            if not SUPPRESS_SERVICENOW:
                out.append(m)
            continue
        if _BULK_SENDER.search(sender) or _BULK_SUBJECT.search(subj):
            continue
        if _CI_MAIL.search(subj):
            continue                               # ci_watch already pushed it
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
                "ends": _clock_minutes(end),
                # Parsed all along and then thrown away apart from one "[tentative]"
                # tag. It is the calendar's own statement of how much he is
                # expected at a thing, which is most of "do I need to be there".
                "status": status.group(1) if status else "",
                "join_url": join_url_in(raw),
            })

    return sorted(events, key=lambda e: e["minutes"])


# The link that actually joins a Teams meeting. `meetings.join()` has always
# required one and nothing ever produced one, so "join my 3pm" could not be
# executed end to end — the capability existed with no way to reach it.
_JOIN_URL = re.compile(
    r"https://teams\.microsoft\.com/l/meetup-join/[^\s\"'<>]+", re.I)


def join_url_in(text: str) -> str:
    """The Teams join link inside a blob of calendar text, or "".

    Best effort by design: some Outlook builds put the link in the row's
    aria-label and some do not. When it is absent the caller falls back to
    driving the calendar UI, which is why this returns "" rather than raising —
    a missing link is a normal day, not a failure.
    """
    m = _JOIN_URL.search(text or "")
    return m.group(0).rstrip(".,);") if m else ""


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


def mail_key(m: dict) -> str:
    """Dedup identity for a mail — deliberately NOT the rendered row.

    Sender + subject with the volatile parts stripped, so the same mail keys the
    same however Outlook chooses to render its age this time round.
    """
    from . import triage
    return triage.stable_key(f"{m.get('sender', '')} {m.get('subject', '')}")


async def _push_mail(notify, fresh: list[dict]) -> None:
    """One message, asks first, FYI collapsed to a quiet line each.

    Every mail used to go out identically with a 180-character preview attached,
    so a colleague's passing thought interrupted exactly like a blocker. Now each
    is judged: only real asks are marked as needing him, and the rest are simply
    stated once so he has the context without being asked for anything.
    """
    from . import attention, triage
    verdicts = []
    for m in fresh[:12]:
        v = triage.classify(m.get("sender", ""), m.get("subject", ""),
                            m.get("preview", ""))
        v = await triage.refine(v, m.get("sender", ""),
                                m.get("subject", ""), m.get("preview", ""))
        # The ledger gets the last word on whether this is worth saying. When it
        # is off, `consider` waves everything through and this is the old path
        # exactly; when it is on, something another source already announced is
        # dropped here rather than announced twice in two different words.
        led_key = attention.key_for(m.get("sender", ""), m.get("subject", ""))
        blob = f"{m.get('subject', '')} {m.get('preview', '')}"
        pri, why, due = attention.score(v.action, blob, critical=is_critical(m),
                                        key=led_key, who=m.get("sender", ""))
        pri, chased = attention.escalate_for_chase(pri, led_key)
        if not attention.consider("outlook", led_key, who=m.get("sender", ""),
                                  what=v.one_line, why=chased or why,
                                  priority=pri, due_at=due):
            continue
        verdicts.append(v.ranked(pri, why, due) if attention.enabled() else v)
    text, needs = triage.summarize(verdicts, "📧 Outlook")
    if not text:
        return
    # Only a genuine ask earns an immediate interrupt. Pure FYI rides the ambient
    # path, so it waits for a natural moment instead of buzzing his pocket. The
    # rank of the most urgent thing in the batch travels with it, so delivery can
    # tell "prod is down" from "someone asked a question" at three in the morning.
    ranks = [v.priority for v in verdicts if v.priority is not None]
    await notify.notify(text, "outlook", urgency="direct" if needs else "ambient",
                        priority=min(ranks) if ranks else None,
                        considered=True)   # attention.consider ran above


async def watch_loop() -> None:
    """Poll the inbox and push NEW mail that looks like it needs Arun."""
    import os
    from . import attention, notify
    poll = int(os.environ.get("OUTLOOK_POLL", str(POLL_SECONDS_DEFAULT)))
    if poll <= 0:
        return
    attention.note_watching("outlook")   # running, and expected to succeed
    while True:
        # Interruptible by a wake, so mail that arrived overnight is read when
        # the machine comes back rather than up to `poll` seconds afterwards.
        await wake.sleep(poll)
        if not (teams_bridge.enabled() and teams_bridge.logged_in_once()
                and store.kv_get("teams_session_ok") != "0"):
            continue
        try:
            mails = await read_mail(20)
        except Exception as exc:
            attention.note_scrape_error("outlook", exc)
            continue
        # Stamped only on a SUCCESSFUL read, so the heartbeat measures what it
        # claims to: that the inbox is actually being seen. The `continue` above
        # is why this matters — a permanently broken selector loops quietly for
        # ever, and nothing downstream can tell that apart from an empty inbox.
        attention.note_scrape("outlook")
        # A mail that is no longer bold is one he has dealt with, wherever he did
        # it. Free, already in hand, and the only engagement signal that does not
        # require him to tell Asta anything.
        for m in mails:
            if not m.get("unread"):
                attention.note_read(attention.key_for(m.get("sender", ""), m.get("subject", "")))
        wanted = needs_attention(mails)
        keys = [mail_key(m) for m in wanted]
        raw = store.kv_get(SEEN_KEY)
        seen: set[str] | None = set(_json.loads(raw)) if raw else None
        fresh = [] if seen is None else [m for m, k in zip(wanted, keys) if k not in seen]
        allk = keys + [k for k in (seen or set()) if k not in keys]
        store.kv_set(SEEN_KEY, _json.dumps(allk[:300]))
        if fresh:
            await _push_mail(notify, fresh)

        # Alerts are held, not sent: only the ones still unrecovered after the
        # window survive. Self-healing IOM noise never reaches the phone at all.
        try:
            release, _healed = triage_alerts(mails)
        except Exception:
            release = []
        if release:
            text, urgency = alert_message(release)
            await notify.notify(text, "outlook", urgency=urgency)


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
