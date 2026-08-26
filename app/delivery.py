"""When to actually say it — quiet hours, one message instead of four, and chasing.

`notify.notify` decides WHETHER something reaches his phone. This decides WHEN,
and the three gaps it closes are all things the old pipeline could not express:

  - **Quiet hours.** A ServiceNow ticket assigned to an L2 queue is real work and
    is correctly ranked as such — at two in the morning it is still real work, and
    it can still wait until seven. Only genuine breakage earns the night.
  - **One message, not four.** Mail, Teams, CI and Jira poll independently and
    each pushes on its own. Four buzzes in one five-minute window is four
    interruptions carrying one interruption's worth of news.
  - **Chasing.** A thing he was told about and never dealt with currently just
    fades. The ledger knows it is still owed, so it can be raised once — once —
    before the day ends.

Off behind ASTA_DELIVERY; every function is a no-op or a pass-through until
flipped. Zero model tokens: clocks and a queue.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time

from . import store

_TRUEY = ("1", "true", "yes", "on")

PENDING_KEY = "delivery_pending"
LAST_SENT_KEY = "delivery_last_sent"
PENDING_MAX = 20


def enabled() -> bool:
    return os.environ.get("ASTA_DELIVERY", "").strip().lower() in _TRUEY


# --- quiet hours ---------------------------------------------------------------

_WINDOW = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def quiet_window() -> tuple[int, int] | None:
    """(start, end) as minutes since midnight, or None when unset/unparseable.

    Unparseable means OFF rather than a guessed default: silently inventing a
    window would mute him on a config typo, and being wrongly silent is the one
    failure this whole subsystem exists to avoid.
    """
    m = _WINDOW.match(os.environ.get("ASTA_QUIET_HOURS", ""))
    if not m:
        return None
    start = int(m.group(1)) * 60 + int(m.group(2))
    end = int(m.group(3)) * 60 + int(m.group(4))
    return (start, end) if start != end else None


def in_quiet_hours(now: float | None = None) -> bool:
    """Windows wrap midnight — 22:00-07:00 is the normal case, not the edge one."""
    window = quiet_window()
    if not window:
        return False
    stamp = dt.datetime.fromtimestamp(time.time() if now is None else now)
    minute = stamp.hour * 60 + stamp.minute
    start, end = window
    return start <= minute or minute < end if start > end else start <= minute < end


def quiet_now(now: float | None = None) -> bool:
    """Is the night guard actually in force right now?

    `in_quiet_hours` answers only "is it late", which is not the same question —
    ASTA_QUIET_HOURS can be set while ASTA_DELIVERY is off, and a guard that
    consulted the clock alone would then suppress a flush that `notify` had
    already decided to deliver, and report it as delivered. Every caller that
    withholds something must ask THIS one.
    """
    return enabled() and in_quiet_hours(now)


def hold_for_quiet(urgency: str, priority: int | None, now: float | None = None) -> bool:
    """Should this wait for morning?

    Deliberately narrow. Only something whose rank is KNOWN gets held, plus the
    ambient traffic that was never urgent by definition. An unranked direct push
    — a reminder he set, a task finishing, a question Asta is asking — goes out
    exactly as it always did, because silencing calls whose meaning is not
    understood is how a quiet-hours feature starts eating things that mattered.
    """
    if not enabled() or not in_quiet_hours(now):
        return False
    from . import attention
    if priority is not None:
        return priority > attention.P_NOW      # breakage still earns the night
    return urgency == "ambient"


# --- one message instead of four ------------------------------------------------

def coalesce_seconds() -> int:
    """0 disables batching entirely."""
    try:
        return max(0, int(os.environ.get("ASTA_COALESCE_SECONDS", "120")))
    except ValueError:
        return 120


def _pending() -> list[str]:
    try:
        raw = json.loads(store.kv_get(PENDING_KEY) or "[]")
    except ValueError:
        return []
    return [t for t in raw if isinstance(t, str) and t]


def buffer(text: str) -> None:
    store.kv_set(PENDING_KEY, json.dumps((_pending() + [text])[-PENDING_MAX:]))


def take_buffered() -> list[str]:
    out = _pending()
    store.kv_set(PENDING_KEY, "[]")
    return out


def note_sent(now: float | None = None) -> None:
    store.kv_set(LAST_SENT_KEY, str(time.time() if now is None else now))


def should_batch(priority: int | None, now: float | None = None) -> bool:
    """True when something just went out and this can ride along with the next flush.

    Breakage never waits. Everything else is worth two minutes if it means one
    buzz instead of four — the news is the same either way, and he reads it once.
    """
    from . import attention
    if not enabled() or coalesce_seconds() <= 0:
        return False
    if priority is not None and priority <= attention.P_NOW:
        return False
    try:
        last = float(store.kv_get(LAST_SENT_KEY) or 0)
    except ValueError:
        return False
    return (time.time() if now is None else now) - last < coalesce_seconds()


def render_batch(texts: list[str]) -> str:
    head = f"📬 {len(texts)} updates:\n\n" if len(texts) > 1 else ""
    return head + "\n\n".join(texts)


# --- chasing what he never answered ---------------------------------------------

def chase_due(now: float | None = None) -> list[dict]:
    """Things he was told about, still owed, and now past their moment.

    Chased ONCE — `chased_at` is set the moment one goes out, and nothing here
    ever picks it up again. A second automated nudge is nagging, and an assistant
    that nags gets muted, which costs him the first nudge too.
    """
    from . import attention
    now = time.time() if now is None else now
    eod = dt.datetime.fromtimestamp(now).replace(
        hour=attention.eod_hour(), minute=0, second=0, microsecond=0).timestamp()
    out = []
    for row in store.attention_open(limit=200, max_priority=attention.P_TODAY):
        if row.get("state") != "notified" or row.get("chased_at"):
            continue
        due = row.get("due_at")
        if due is not None and now >= float(due):
            out.append(row)
        elif due is None and now >= eod:
            out.append(row)
    return out


def mark_chased(rows: list[dict], now: float | None = None) -> None:
    for row in rows:
        store.attention_set(row["key"], chased_at=time.time() if now is None else now)


def render_chase(rows: list[dict]) -> str:
    lines = []
    for row in rows[:6]:
        who = row.get("who") or ""
        lines.append(f"• {row.get('what') or row.get('key')}" + (f" — {who}" if who else ""))
    return ("⏳ Still waiting on you"
            + (f" ({len(rows)})" if len(rows) > 1 else "") + ":\n\n" + "\n".join(lines))


async def chase_loop() -> None:
    """Hourly: raise what is owed and overdue, once each."""
    import asyncio
    from . import notify
    while True:
        await asyncio.sleep(3600)
        try:
            if not enabled():
                continue
            rows = chase_due()
            if not rows:
                continue
            mark_chased(rows)
            # The rank travels with it, so the night guard applies. Without it a
            # chase is an unranked direct push — and this loop runs hourly past
            # end of day, so "still waiting on you" would arrive at 2am about
            # something that had already waited eight hours.
            from . import attention
            await notify.notify(render_chase(rows), "attention",
                                urgency="direct", priority=attention.P_TODAY)
        except Exception:
            pass


async def flush_loop() -> None:
    """Drain the coalescing buffer so a batched message cannot sit for ever."""
    import asyncio
    from . import notify
    while True:
        await asyncio.sleep(max(30, coalesce_seconds()))
        try:
            # `deliver` is the raw send, deliberately below the policy — so this
            # loop has to apply the night guard itself. Buffered items are never
            # P0 (should_batch refuses those), so nothing here has earned the
            # night, and leaving them queued means they go out in the morning.
            if not enabled() or quiet_now():
                continue
            texts = take_buffered()
            if texts:
                await notify.deliver(render_batch(texts))
        except Exception:
            pass
