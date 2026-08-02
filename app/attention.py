"""One ledger of everything that wants Arun, and ONE function that decides.

The failure this closes is structural, not a bug. `notify.notify()` is called from
thirty-four places, and every one of them decides alone whether to interrupt him,
then forgets immediately. Three consequences follow directly from that, and all
three are things he has actually complained about:

  - **Nothing is deduplicated across sources.** One incident can arrive as mail,
    as a Teams mention and as a CI alert, and each path pushes it. `goes_to_hold`
    in outlook.py exists because exactly that collision was found by hand, once.
    A key that is unique across every source is the general answer.
  - **Nothing is remembered.** A push that arrived while he was away is gone. The
    thing still wants him; Asta has simply stopped knowing.
  - **Nothing can be ranked**, because ranking needs state — "the third time
    someone has chased this" is not visible to a function that sees one poll.

So every source records into ONE table and asks ONE policy whether to push. Zero
model tokens: this is arithmetic and a SQL row. Off behind ASTA_ATTENTION, and a
pure no-op until flipped — `consider()` returns True when disabled, which is
exactly what every caller did before it existed.

The freshness heartbeat below is deliberately NOT behind the flag. A watcher that
dies silently is indistinguishable from a quiet week, and that is the one failure
here that can hurt him today.
"""

from __future__ import annotations

import os
import re
import time

from . import store

_TRUEY = ("1", "true", "yes", "on")

#: Priorities, low number = more urgent, so SQL `ORDER BY priority` reads right.
P_NOW = 0        # interrupt him: someone is blocked, or something is broken
P_TODAY = 1      # owed today: a real ask, no immediate deadline
P_FYI = 2        # worth knowing, wants nothing
P_MUTE = 3       # recorded, never pushed — the audit trail for a suppression

SETTLED = ("acted", "dropped")


def enabled() -> bool:
    return os.environ.get("ASTA_ATTENTION", "").strip().lower() in _TRUEY


# --- what makes two arrivals the same thing ----------------------------------
#
# A unique key column gives cross-source dedup only if the sources actually AGREE
# on the key, and left to themselves they do not: mail keys on sender+subject
# ("servicenow inc4471 booking service down") while the Teams feed keys on the
# rendered row ("priya mentioned you in platform inc4471 is down"). Same
# incident, two keys, no dedup — the table would have been right and the feature
# still absent.
#
# An incident or ticket id is the one identity both carry verbatim, so when there
# is one it wins. With no id, each source falls back to its own text and the keys
# differ — which is the honest outcome: things are collapsed when they can be
# PROVED to be the same, never on a resemblance.
#
# Deliberately not reused for the per-source `seen` sets. Those are what makes
# the disabled path byte-identical to the old behaviour, and re-keying them would
# quietly change what an unflagged Asta does.
_IDISH = re.compile(r"\b((?:INC|CHG|PRB|ALERT)[0-9]{4,}|[A-Z][A-Z0-9]{1,9}-\d+)\b")


def key_for(*parts: str) -> str:
    """The ledger identity for an arrival: a real id if one is present, else text."""
    blob = " ".join(p for p in parts if p)
    ids = _IDISH.findall(blob)
    if ids:
        return ids[0].upper()
    from . import triage
    return triage.stable_key(blob)


# --- the one decision --------------------------------------------------------

def consider(source: str, key: str, who: str = "", what: str = "",
             priority: int = P_FYI, why: str = "", due_at: float | None = None,
             now: float | None = None) -> bool:
    """Record that something wants him, and answer: push it, yes or no?

    Returns True when disabled, so a caller that has not been taught about the
    ledger behaves exactly as it did before. That is the whole no-op contract —
    the flag changes what Asta REMEMBERS, and only then what it decides.
    """
    if not enabled() or not key:
        return True
    row = store.attention_upsert(key, source, who=who, what=what,
                                 priority=priority, why=why, due_at=due_at, now=now)
    if not should_push(row):
        return False
    store.attention_set(key, state="notified", notified_at=time.time() if now is None else now)
    return True


def should_push(row: dict) -> bool:
    """The single policy. Deliberately boring, and deliberately in one place.

    A settled thing is settled — he dealt with it, or it was dropped, and saying
    it again is the noise the ledger exists to end. Anything already announced
    stays quiet too: re-announcing is what the per-source `seen` sets were each
    solving separately, and separately is how two of them disagreed.

    Re-surfacing something he never answered is a real need, but it is a DELIVERY
    decision (when to chase, how often, not at 2am) rather than a discovery one,
    so it lives with the rest of the delivery policy and not here.
    """
    if row.get("state") in SETTLED:
        return False
    if int(row.get("priority", P_FYI)) >= P_MUTE:
        return False
    return row.get("state") != "notified"


def mark_acted(key: str, now: float | None = None) -> None:
    """He dealt with it — by replying, by asking Asta to, or elsewhere entirely."""
    if not key:
        return
    store.attention_set(key, state="acted", acted_at=time.time() if now is None else now)


def mark_dropped(key: str) -> None:
    """It stopped mattering without him doing anything (an alert that recovered)."""
    if key:
        store.attention_set(key, state="dropped")


def open_items(limit: int = 50, max_priority: int = P_FYI) -> list[dict]:
    """Everything still owed, most urgent first — "what's on my plate", as a query.

    The morning brief currently answers this by re-scraping the whole inbox and
    calendar in a fresh Playwright session, ~20s, for data a watcher read five
    minutes earlier. Once things are recorded, that read is free.
    """
    return store.attention_open(limit=limit, max_priority=max_priority)


def purge(days: int = 14, now: float | None = None) -> int:
    now = time.time() if now is None else now
    return store.attention_purge(now - days * 86400)


# --- freshness heartbeat (NOT behind the flag, on purpose) -------------------
#
# Both watchers swallow every exception and continue — correct, because a
# transient DOM hiccup must not kill the loop. But it means a selector that
# breaks permanently produces SILENCE, and silence is what a quiet week looks
# like too. He would experience a dead inbox watcher as "nothing much happened".
#
# So a successful scrape stamps the clock, and a source that has worked before
# and has now gone quiet for too long becomes a health problem with a name.

_SCRAPE_KEY = "attention_scrape:"


def stale_after_minutes() -> int:
    try:
        return max(0, int(os.environ.get("ASTA_STALE_AFTER_MINUTES", "90")))
    except ValueError:
        return 90


def note_scrape(source: str, now: float | None = None) -> None:
    """Record that `source` successfully read its surface just now."""
    if source:
        store.kv_set(_SCRAPE_KEY + source, str(time.time() if now is None else now))


def last_scrape(source: str) -> float:
    try:
        return float(store.kv_get(_SCRAPE_KEY + source) or 0)
    except ValueError:
        return 0.0


def stale_sources(sources: tuple[str, ...] = ("outlook", "teams"),
                  now: float | None = None) -> dict[str, int]:
    """Sources that used to work and have now gone quiet — {source: minutes}.

    A source that has NEVER reported is not stale, it is switched off; alarming
    about a Teams bridge Arun never enabled would be the boy who cried wolf on
    day one. Only something that was working and stopped is worth a word.
    """
    now = time.time() if now is None else now
    limit = stale_after_minutes()
    if limit <= 0:
        return {}
    out: dict[str, int] = {}
    for source in sources:
        last = last_scrape(source)
        if last <= 0:
            continue
        minutes = int((now - last) // 60)
        if minutes >= limit:
            out[source] = minutes
    return out
