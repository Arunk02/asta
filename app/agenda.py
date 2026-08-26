"""Reasoning about a day of meetings: which one, do you need it, and when to warn.

`meetings.py` can join a call, and `outlook.todays_meetings()` can list them, and
until now nothing connected the two: `join()` required a URL that nothing ever
produced, so "join my 3pm" named a capability that could not actually be reached.
This is the layer in between — picking WHICH meeting someone means, and deciding
how much warning each one deserves.

Everything here is a pure function over the event dicts the calendar scrape
already returns. That is deliberate: the browser half cannot be tested without a
live Outlook, so all the judgement lives on this side of the line where it can be.

Off behind ASTA_MEET2 for the parts that change behaviour; the resolvers are
pure and always available.
"""

from __future__ import annotations

import os
import re

_TRUEY = ("1", "true", "yes", "on")


def enabled() -> bool:
    return os.environ.get("ASTA_MEET2", "").strip().lower() in _TRUEY


# --- which meeting does he mean? ----------------------------------------------

_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", re.I)
_BARE_HOUR = re.compile(r"\b(?:my|the)\s+(\d{1,2})(?::(\d{2}))?\b", re.I)


def _wanted_minutes(phrase: str) -> int | None:
    """'my 3pm' / '15:30' / 'the 9' → minutes since midnight, or None."""
    m = _CLOCK.search(phrase or "")
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
        return hour * 60 + int(m.group(2) or 0)
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", phrase or "")
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = _BARE_HOUR.search(phrase or "")
    if m:
        hour = int(m.group(1))
        # A bare "my 3" in a working day means the afternoon. Mornings get said
        # as "my 9" and land before noon anyway, so only 1-7 need the nudge.
        return (hour + 12 if 1 <= hour <= 7 else hour) * 60 + int(m.group(2) or 0)
    return None


def pick(events: list[dict], phrase: str, now_minutes: int | None = None) -> dict | None:
    """The one meeting `phrase` means, or None when it is not one meeting.

    Returning None for AMBIGUOUS is the whole point, and it is why this is not a
    fuzzy match that picks a best guess. Joining the wrong call puts him in a
    room he did not mean to be in, in front of people who can see him arrive —
    an error that cannot be quietly undone. When it is unclear, the caller asks.
    """
    events = list(events or [])
    if not events:
        return None
    phrase = (phrase or "").strip()

    if re.search(r"\b(next|upcoming|my next)\b", phrase, re.I) and now_minutes is not None:
        later = [e for e in events if e.get("minutes", 0) >= now_minutes]
        return later[0] if later else None

    wanted = _wanted_minutes(phrase)
    if wanted is not None:
        at_time = [e for e in events if e.get("minutes") == wanted]
        if len(at_time) == 1:
            return at_time[0]
        return None                      # nothing then, or two things then

    words = [w for w in re.findall(r"[a-z0-9']+", phrase.lower())
             if w not in ("join", "my", "the", "a", "meeting", "call", "please", "in")]
    if not words:
        return None
    hits = [e for e in events
            if all(w in e.get("title", "").lower() for w in words)]
    return hits[0] if len(hits) == 1 else None


# --- do you actually need to be there? -----------------------------------------

#: Meetings whose whole point is that Arun says something.
SPEAKING = re.compile(
    r"\b(stand[- ]?up|scrum|daily|sync|retro(spective)?|refinement|grooming|"
    r"planning|review|demo|1[:-]?1|one[- ]on[- ]one|catch[- ]?up|weekly)\b", re.I)

#: Broadcasts. He is an audience member, and the recording exists.
BROADCAST = re.compile(
    r"\b(all[- ]hands|town ?hall|company (update|meeting)|webinar|"
    r"broadcast|ask me anything|ama|newsletter|briefing session)\b", re.I)


def attendance(ev: dict) -> tuple[bool, str]:
    """(is he really needed, why). Never suppresses — it changes the volume.

    Deliberately advisory. Skipping the heads-up for a meeting Asta guessed was
    optional is the one mistake here that he finds out about by missing it, so
    the answer only ever moves a ping from interrupting to ambient.
    """
    status = (ev.get("status") or "").strip().lower()
    title = ev.get("title") or ""
    if status in ("free", "tentative"):
        return False, f"your calendar says {status}"
    if BROADCAST.search(title):
        return False, "a broadcast — there will be a recording"
    return True, ""


# --- how much warning does it deserve? ------------------------------------------

DEFAULT_LEAD = 30

#: A 1:1 needs a moment to gather a thought, not half an hour of runway; a
#: standup needs long enough for the draft to be worth reading before it starts.
LEADS = (
    (re.compile(r"\b(stand[- ]?up|scrum|daily)\b", re.I), 30),
    (re.compile(r"\b(1[:-]?1|one[- ]on[- ]one|catch[- ]?up)\b", re.I), 15),
    (re.compile(r"\b(review|demo|planning|refinement|grooming|retro)\b", re.I), 20),
)


def lead_minutes(ev: dict, default: int = DEFAULT_LEAD) -> int:
    """How long before it starts to say something."""
    title = ev.get("title") or ""
    if BROADCAST.search(title):
        return 5                      # a nudge, not runway — there is no prep to do
    for pattern, minutes in LEADS:
        if pattern.search(title):
            return minutes
    return default


# --- what the day looks like ------------------------------------------------------

def conflicts(events: list[dict]) -> list[tuple[dict, dict]]:
    """Pairs that overlap in time — double-bookings he has to choose between.

    Worth surfacing precisely because the calendar does not: both invites were
    accepted at different moments, and the collision only becomes visible on the
    day, usually about four minutes beforehand.
    """
    out = []
    ordered = sorted([e for e in events or [] if e.get("ends")],
                     key=lambda e: e.get("minutes", 0))
    for i, first in enumerate(ordered):
        for second in ordered[i + 1:]:
            if second["minutes"] >= first["ends"]:
                break
            out.append((first, second))
    return out


def back_to_back(events: list[dict], gap: int = 0) -> list[tuple[dict, dict]]:
    """Pairs with no real break between them — where the day stops being feasible."""
    out = []
    ordered = sorted([e for e in events or [] if e.get("ends")],
                     key=lambda e: e.get("minutes", 0))
    for first, second in zip(ordered, ordered[1:]):
        if first["ends"] <= second["minutes"] <= first["ends"] + gap:
            out.append((first, second))
    return out


def day_warnings(events: list[dict]) -> list[str]:
    """The lines worth putting in the morning brief, or none at all."""
    lines = []
    for first, second in conflicts(events):
        lines.append(f"⚠️ Clash: {first['title']} ({first['start']}) overlaps "
                     f"{second['title']} ({second['start']})")
    runs = back_to_back(events)
    if runs:
        # Named by when the NEXT one starts, not when the previous one ends. Both
        # describe the same instant, but only the start time is guaranteed to be
        # populated — an event row with no parsed end rendered "first at " and
        # said nothing at all.
        lines.append(f"⏱️ {len(runs)} back-to-back handover(s) with no gap — "
                     f"first at {runs[0][1]['start']}")
    return lines
