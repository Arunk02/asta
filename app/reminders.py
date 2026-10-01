"""Reminders & nudges — "remind me at 3pm to reply to Alex".

The brain (any model) converts natural language to a local ISO timestamp and
calls set_reminder / POST /api/reminders; this module just stores and fires.
Fires go through notify() → WhatsApp + Telegram + UI bell. Overdue reminders
(e.g. the laptop was asleep) fire on the next loop tick — never silently lost.

repeat: '' (one-shot) | daily | weekdays | weekly
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import time

from . import store

CHECK_SECONDS = 30
VALID_REPEATS = ("", "daily", "weekdays", "weekly")
#: A reminder whose text starts with this is work to carry out when it fires.
DO_PREFIX = "DO:"
#: …and this one is a message to send when it fires: JSON {to, text, to_group, approved}.
SEND_PREFIX = "SEND:"

#: When a fact he asked to remember is about doing something LATER.
_LATER = re.compile(
    r"\b(?P<when>tomorrow(?:\s+morning|\s+mrng|\s+mrg)?|tmrw|tmr|next\s+morning|"
    r"(?:in\s+the\s+|this\s+|next\s+)?(?:morning|mrng)|tonight|later\s+today|eod|"
    r"monday|tuesday|wednesday|thursday|friday)\b", re.I)
_TO_DO = re.compile(r"\b(?:check|review|do|update|send|give|reply|follow\s+up|post|share|"
                    r"comment|ping|remind|raise|merge|fix|look\s+at)\b", re.I)


def later_when(text: str, now: float | None = None) -> float | None:
    """When "tomorrow morning", "next morning", "EOD"… falls, or None."""
    m = _LATER.search(text or "")
    if not m or not _TO_DO.search(text or ""):
        return None
    now_d = dt.datetime.fromtimestamp(time.time() if now is None else now)
    w = m.group("when").lower()
    at10 = lambda d: d.replace(hour=10, minute=0, second=0, microsecond=0)  # noqa: E731
    if w.startswith(("tomorrow", "tmr", "next morning")):
        return at10(now_d + dt.timedelta(days=1)).timestamp()
    if "morning" in w or "mrng" in w:
        d = now_d if now_d.hour < 10 else now_d + dt.timedelta(days=1)
        return at10(d).timestamp()
    if w == "tonight":
        return now_d.replace(hour=20, minute=0, second=0, microsecond=0).timestamp()
    if w == "later today":
        return (now_d + dt.timedelta(hours=3)).timestamp()
    if w == "eod":
        return now_d.replace(hour=17, minute=30, second=0, microsecond=0).timestamp()
    days = ["monday", "tuesday", "wednesday", "thursday", "friday"].index(w)
    ahead = (days - now_d.weekday()) % 7 or 7
    return at10(now_d + dt.timedelta(days=ahead)).timestamp()


def _future(due: float, now: float) -> float:
    """"by EOD" said at 8pm means tomorrow's end of day, not three hours ago."""
    while due <= now:
        due += 86400
    return due


def schedule_work(text: str, now: float | None = None) -> dict | None:
    """A "do this later" he said, as a reminder that carries the work out."""
    due = later_when(text, now)
    if due is None:
        return None
    due = _future(due, time.time() if now is None else now)
    return store.create_reminder(f"{DO_PREFIX} {text.strip()[:600]}", due, "")


def parse_due(due_iso: str) -> float:
    """Local ISO timestamp ('2026-07-19T15:00' or with seconds/date-only) → epoch."""
    d = dt.datetime.fromisoformat(due_iso.strip())
    if d.tzinfo is not None:
        return d.timestamp()
    return d.replace(tzinfo=None).timestamp()


def create(text: str, due_iso: str, repeat: str = "") -> dict:
    if repeat not in VALID_REPEATS:
        raise ValueError(f"repeat must be one of {VALID_REPEATS}")
    due = parse_due(due_iso)  # raises ValueError on garbage
    if not text.strip():
        raise ValueError("reminder text is empty")
    return store.create_reminder(text.strip(), due, repeat)


def schedule_send(to: str, text: str, due: float, to_group: bool = False,
                  approved: bool = True) -> dict:
    """A message that GOES OUT when it is due — not a ping asking him to send it.

    1 Oct: "notify Vinish on Monday to get both merged" became three reminders,
    each of which would only have pinged him, and "goes out at 14:32, no further
    confirmation needed" had been said that afternoon with no reminder stored at
    all. One message per person per time: a newer one replaces what it updates."""
    import json
    if not (to or "").strip() or not (text or "").strip():
        raise ValueError("a scheduled message needs who and what")
    for r in store.list_reminders():
        if r["status"] == "pending" and abs(float(r["due_at"]) - due) < 3600 \
                and (r["text"].startswith(SEND_PREFIX) and _send_of(r).get("to", "").lower() == to.strip().lower()
                     or _first(to) and _first(to) in r["text"].lower() and not r["text"].startswith(DO_PREFIX)):
            store.update_reminder(r["id"], status="cancelled")
    body = json.dumps({"to": to.strip(), "text": text.strip(), "to_group": bool(to_group),
                       "approved": bool(approved)})
    return store.create_reminder(f"{SEND_PREFIX} {body}", due, "")


def _first(name: str) -> str:
    parts = (name or "").strip().lower().split()
    return parts[0] if parts and len(parts[0]) > 2 else ""


def _send_of(r: dict) -> dict:
    import json
    try:
        return json.loads(r["text"][len(SEND_PREFIX):])
    except (ValueError, KeyError, TypeError):
        return {}


def describe(r: dict) -> str:
    """How a reminder reads to him: a scheduled message says what goes to whom."""
    if r["text"].startswith(SEND_PREFIX):
        s = _send_of(r)
        how = "sends by itself" if s.get("approved") else "asks your yes first"
        return f"message to {s.get('to', '?')} ({how}): “{(s.get('text') or '')[:160]}”"
    return r["text"]


async def _send_due(r: dict, late_note: str) -> None:
    """Send it now — or, if he never said so, or they are manager and above,
    put it in front of him for his yes."""
    from . import answers, notify, ops, senior
    s = _send_of(r)
    to, text = s.get("to", ""), s.get("text", "")
    if not s.get("approved") or (not s.get("to_group") and senior.is_senior(to)):
        await answers.present(who=to, need="scheduled message", chat=to,
                              group=bool(s.get("to_group")), analysis=f"⏰ Scheduled for now{late_note}.",
                              reply=text)
        return
    try:
        line = await ops.run({"name": "teams_send", "args": {
            "to": to, "text": text, "to_group": bool(s.get("to_group"))}})
        await notify.notify(f"⏰ Scheduled message sent{late_note}: {line}\n> {text[:300]}",
                            "reminder", asked=True)
    except Exception as exc:                                    # noqa: BLE001
        await notify.notify(f"⚠️ Scheduled message to {to} not sent — {exc}. "
                            f"Staging it for your send.", "reminder", asked=True)
        await answers.present(who=to, need="scheduled message", chat=to,
                              group=bool(s.get("to_group")), analysis="", reply=text)


def cancel(reminder_id: int) -> None:
    r = store.get_reminder(reminder_id)
    if not r or r["status"] != "pending":
        raise ValueError(f"no pending reminder #{reminder_id}")
    store.update_reminder(reminder_id, status="cancelled")


def _next_due(due_at: float, repeat: str) -> float:
    d = dt.datetime.fromtimestamp(due_at)
    if repeat == "weekly":
        d += dt.timedelta(days=7)
    else:
        d += dt.timedelta(days=1)
        if repeat == "weekdays":
            while d.weekday() >= 5:  # Sat/Sun
                d += dt.timedelta(days=1)
    return d.timestamp()


async def fire_due() -> int:
    """Fire everything due; returns how many fired. Status flips BEFORE notify so a
    notify crash can't cause a double-fire storm."""
    from . import notify
    fired = 0
    for r in store.due_reminders(time.time()):
        if r["repeat"]:
            store.update_reminder(r["id"], fired_at=time.time(),
                                  due_at=_next_due(r["due_at"], r["repeat"]))
        else:
            store.update_reminder(r["id"], status="done", fired_at=time.time())
        late = time.time() - r["due_at"]
        late_note = f" (was due {int(late // 60)} min ago)" if late > 120 else ""
        if r["text"].startswith(SEND_PREFIX):
            try:
                await _send_due(r, late_note)
            except Exception:                                  # noqa: BLE001
                pass
            fired += 1
            continue
        # His own ask coming back to him: it rings through his quiet time too.
        await notify.notify(f"⏰ Reminder: {r['text']}{late_note}", "reminder", asked=True)
        if r["text"].startswith(DO_PREFIX):
            # Work he asked to be done later is DONE later, not only mentioned.
            # 30 Sep: "check Komal's PR tomorrow morning and update" was filed as
            # a note; at 11:19 he asked whether it was done, and it was not.
            try:
                from . import main
                await main.run_on_phone(r["text"][len(DO_PREFIX):].strip()
                                        + "\n\n(This is work Arun asked for earlier, "
                                          "scheduled for now. Do it, then tell him the result.)")
            except Exception:                                  # noqa: BLE001
                pass
        fired += 1
    return fired


async def loop() -> None:
    while True:
        try:
            await fire_due()
        except Exception:
            pass
        await asyncio.sleep(CHECK_SECONDS)
