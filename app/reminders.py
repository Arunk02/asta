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
