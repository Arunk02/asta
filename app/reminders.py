"""Reminders & nudges — "remind me at 3pm to reply to Vinish".

The brain (any model) converts natural language to a local ISO timestamp and
calls set_reminder / POST /api/reminders; this module just stores and fires.
Fires go through notify() → WhatsApp + Telegram + UI bell. Overdue reminders
(e.g. the laptop was asleep) fire on the next loop tick — never silently lost.

repeat: '' (one-shot) | daily | weekdays | weekly
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time

from . import store

CHECK_SECONDS = 30
VALID_REPEATS = ("", "daily", "weekdays", "weekly")


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
        await notify.notify(f"⏰ Reminder: {r['text']}{late_note}", "reminder")
        fired += 1
    return fired


async def loop() -> None:
    while True:
        try:
            await fire_due()
        except Exception:
            pass
        await asyncio.sleep(CHECK_SECONDS)
