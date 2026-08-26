"""Turning "last night" into two timestamps.

Arun does not ask for messages "between 1786510800 and 1786532400". He asks what
Vinish said last night, or what came in yesterday, or what he missed this
morning. Something has to turn that into a window, and it should not be an LLM
call: the mapping is fixed, the cost of getting it wrong is a confidently wrong
answer about the wrong evening, and a deterministic function can be tested.

Everything here is local time, because that is the clock he is speaking in. "Last
night" means the evening that just ended, which is why it starts on the PREVIOUS
day and runs into the small hours of this one — a naive reading of "night" as
"yesterday's date" silently drops a message sent at 00:40.
"""

from __future__ import annotations

import datetime as dt
import re

#: When an evening is taken to begin and end. Deliberately generous at both ends:
#: the cost of a slightly wide window is one extra message in the answer, and the
#: cost of a narrow one is missing the message that prompted the question.
NIGHT_STARTS = 18
NIGHT_ENDS = 6

#: What "no idea what he meant" falls back to.
DEFAULT_HOURS = 24


def _midnight(d: dt.datetime) -> dt.datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def parse(phrase: str, now: dt.datetime | None = None) -> tuple[float, float, str]:
    """(since, until, label) as epoch seconds for a phrase like 'last night'.

    Unrecognised input falls back to the last 24 hours rather than raising — a
    window that is too wide still answers the question, where an error answers
    nothing. The label says which window was actually used, so the answer can
    tell him what it looked at instead of leaving him to assume.
    """
    now = now or dt.datetime.now()
    text = (phrase or "").strip().lower()
    today = _midnight(now)

    # "last 2 hours", "past 30 minutes", "since 3 days" — the explicit forms win
    # over the named ones, because someone who typed a number meant it.
    m = re.search(r"(\d+)\s*(minute|min|hour|hr|h|day|d|week|w)s?\b", text)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        seconds = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "h": 3600,
                   "day": 86400, "d": 86400, "week": 604800, "w": 604800}[unit]
        span = n * seconds
        return (now.timestamp() - span, now.timestamp(), f"last {n} {unit}(s)")

    if "last night" in text or "yesterday night" in text or "overnight" in text:
        # The evening that just ended. If he asks at 02:00, that evening began
        # yesterday and has not finished — so the window ends at "now", not at
        # a 06:00 that is still in the future.
        start = (today - dt.timedelta(days=1)).replace(hour=NIGHT_STARTS)
        end = today.replace(hour=NIGHT_ENDS)
        if now.hour < NIGHT_ENDS:
            start = (today - dt.timedelta(days=1)).replace(hour=NIGHT_STARTS)
            end = now
        return (start.timestamp(), end.timestamp(), "last night")

    if "yesterday" in text:
        start = today - dt.timedelta(days=1)
        return (start.timestamp(), today.timestamp(), "yesterday")

    if "this morning" in text or "morning" in text:
        return (today.timestamp(), today.replace(hour=12).timestamp(), "this morning")

    if "today" in text:
        return (today.timestamp(), now.timestamp(), "today")

    if "this week" in text or "last week" in text:
        start = today - dt.timedelta(days=7)
        return (start.timestamp(), now.timestamp(), "the last 7 days")

    if "while i was away" in text or "since i left" in text or "missed" in text:
        from . import wake
        when, gap = wake.last_gap()
        if when and gap:
            return (when - gap, now.timestamp(), "while the laptop was asleep")

    span = DEFAULT_HOURS * 3600
    return (now.timestamp() - span, now.timestamp(), f"the last {DEFAULT_HOURS}h")


def describe(since: float, until: float) -> str:
    """'Mon 11 Aug 18:00 → Tue 12 Aug 06:00' — so an answer can show its window."""
    fmt = "%a %d %b %H:%M"
    return (f"{dt.datetime.fromtimestamp(since):{fmt}} → "
            f"{dt.datetime.fromtimestamp(until):{fmt}}")
