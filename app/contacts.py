"""Who actually matters — learned from what Arun does, not from a regex.

`_BULK_SENDER` in outlook.py is a hand-written list of noisy senders. It works,
and it does not scale: every new newsletter is a code change, and every important
human is anonymous to it — the manager who needs an answer and a stranger's cold
email score exactly the same, because neither matches the pattern.

This is the prior that regex should have been. It costs arithmetic, learns from
labels Arun produces for free by dealing with things or not, and gets better
every week without anyone editing anything. The regex stays as the cold start:
on day one there is no history, and a known newsletter is still a newsletter.

Two safety rules make it safe to switch on, and they are the reason this can be
trusted before the numbers are large:

  - **It can quiet noise. It cannot silence a question.** Demotion only ever
    applies to something already ranked FYI. Someone actually ASKING him
    something is never muted by a statistic, however bad their history.
  - **It never touches breakage.** A P0 stays P0 whoever sent it. "The sender is
    usually noise" is not an argument about whether prod is down.

Off behind ASTA_CONTACTS; a no-op until flipped, and inert until there is enough
evidence to be worth anything.
"""

from __future__ import annotations

import os
import re

from . import store

_TRUEY = ("1", "true", "yes", "on")

#: Below this many labelled interactions a rate is noise, and the prior stays out
#: of the way. Five is deliberately small enough to become useful within a week
#: and large enough that one unlucky ignored mail cannot mute somebody.
MIN_EVIDENCE = 5


def enabled() -> bool:
    return os.environ.get("ASTA_CONTACTS", "").strip().lower() in _TRUEY


def min_evidence() -> int:
    try:
        return max(1, int(os.environ.get("ASTA_CONTACT_MIN_EVIDENCE", str(MIN_EVIDENCE))))
    except ValueError:
        return MIN_EVIDENCE


def high_rate() -> float:
    try:
        return float(os.environ.get("ASTA_CONTACT_HIGH", "0.7"))
    except ValueError:
        return 0.7


def low_rate() -> float:
    try:
        return float(os.environ.get("ASTA_CONTACT_LOW", "0.15"))
    except ValueError:
        return 0.15


# A display name and the same person's address must land on one row, or the
# history splits in two and neither half ever reaches the evidence bar.
_ADDR = re.compile(r"<([^>]+)>")
_PUNCT = re.compile(r"[\"']")


def normalise(who: str) -> str:
    """One identity per person: 'Sam Patel <sam@x.com>' and 'sam@x.com' agree."""
    who = (who or "").strip()
    if not who:
        return ""
    m = _ADDR.search(who)
    if m:
        who = m.group(1)
    return _PUNCT.sub("", who).strip().lower()[:120]


# --- learning ----------------------------------------------------------------

def record(who: str, outcome: str) -> None:
    """Fold one label from the attention ledger into what is known about a person."""
    who = normalise(who)
    if not who:
        return
    from . import attention
    if outcome in attention.ENGAGED:
        store.contact_bump(who, "engaged")
    elif outcome == "muted":
        store.contact_bump(who, "muted")
    elif outcome == "ignored":
        store.contact_bump(who, "ignored")


def seed_from_meetings(events: list[dict]) -> int:
    """Count the people he actually sits in meetings with.

    The one objective signal available before any learning: it needs no history,
    no judgement and no extra scraping — the organiser is already parsed out of
    the calendar for the pre-meeting heads-up. Someone he meets weekly is not
    bulk mail, and that fact protects them from ever being auto-muted.
    """
    seen = 0
    for ev in events or []:
        who = normalise(ev.get("organizer", ""))
        if who:
            store.contact_bump(who, "met")
            seen += 1
    return seen


# --- using it ----------------------------------------------------------------

def signal_rate(who: str) -> float | None:
    """Engaged / everything-labelled, or None when there is not enough to say."""
    row = store.contact_get(normalise(who))
    if not row:
        return None
    total = row["engaged"] + row["ignored"] + row["muted"]
    if total < min_evidence():
        return None
    return row["engaged"] / total


def known_human(who: str) -> bool:
    """Has he been in a meeting with them? Objective, and it blocks auto-muting."""
    row = store.contact_get(normalise(who))
    return bool(row and row["met"] > 0)


def adjust(priority: int, who: str) -> tuple[int, str]:
    """Nudge a rank by what is known about the sender. Returns (priority, why).

    Deliberately capped at one level in either direction. A prior is evidence,
    not a verdict — letting it move something two tiers would mean a statistic
    quietly overruling the message itself, which is exactly the failure mode that
    makes learned filters untrustworthy.
    """
    from . import attention
    if not enabled() or priority <= attention.P_NOW:
        return priority, ""          # breakage is never re-ranked by who sent it
    rate = signal_rate(who)
    if rate is None:
        return priority, ""          # not enough evidence — stay out of the way
    if rate >= high_rate():
        return max(attention.P_NOW, priority - 1), f"you usually act on {who}"
    if rate <= low_rate() and priority >= attention.P_FYI and not known_human(who):
        # Only ever quiets something already ranked FYI. Someone ASKING him
        # something is never silenced by their history, and someone he shares
        # meetings with is never silenced at all.
        return attention.P_MUTE, f"you never act on {who}"
    return priority, ""


def scoreboard(limit: int = 20) -> list[dict]:
    """Who Asta thinks matters, with the evidence — readable before it is trusted."""
    out = []
    for row in store.contacts_list(limit):
        total = row["engaged"] + row["ignored"] + row["muted"]
        out.append({**row, "total": total,
                    "rate": round(row["engaged"] / total, 2) if total else None,
                    "acting": total >= min_evidence()})
    return out


# --- which thread does a name actually mean? ---------------------------------
#
# "divya" resolves, in Teams' own search, to a 1:1 titled "Divya" — a real chat
# with no messages in it. The person Arun actually talks to is "Palikala Divya
# Maheswari". A message there would reach the wrong person and a CALL would ring
# them, and neither is undone by noticing afterwards.
#
# His own rail is the better authority than Teams' ranking, and Asta already has
# it: the threads it has read messages in are the conversations he really has.

def known_threads(limit: int = 800) -> list[str]:
    """Distinct chats Asta has actually read messages in, newest activity first."""
    from . import store
    try:
        rows = store.teams_messages(limit=limit)
    except Exception:                                          # noqa: BLE001
        return []
    seen: dict[str, None] = {}
    for r in reversed(rows):
        name = (r.get("chat") or "").strip()
        if name:
            seen.setdefault(name, None)
    return list(seen)


def resolve_name(name: str) -> tuple[str, list[str]]:
    """(the thread he means, all the threads it could be).

    An exact match wins. Otherwise a name that matches exactly one real
    conversation resolves to that one — "divya" is unambiguous among the people he
    talks to, even though Teams' search prefers a different chat with the shorter
    title. Anything matching several is returned undecided, because guessing which
    colleague he meant is the mistake that cannot be walked back.
    """
    wanted = (name or "").strip().lower()
    if not wanted:
        return "", []
    threads = known_threads()
    exact = [t for t in threads if t.lower() == wanted]
    if exact:
        return exact[0], exact
    near = [t for t in threads if wanted in t.lower()]
    return (near[0] if len(near) == 1 else ""), near
