"""The conversation steward: a greeting is the start of something, not news.

Round 3, P9. The failure, in the plan's words: *"A greeting has nothing to
check, so it is forwarded as news. Nothing keeps the conversation open or waits
for the real ask."*

What actually happens on Teams is that a colleague types "Hi", waits for it to
send, and types the real question ten seconds later. Asta pushed the "Hi" to his
phone as something needing attention, then pushed the question as a second
thing. Two interruptions for one conversation, and the first of them carried no
information at all — the message it was about had not been written yet.

So a greeting with nothing else in it is HELD: no push, no investigation, and a
note that this person has started talking. The next thing they say is the real
ask, and it arrives carrying the greeting behind it, as one interruption.

Three rules keep the hold from becoming its own bug:

  A HOLD IS NOT A DROP. A colleague who says hi and then goes quiet still tried
  to reach him. After `STEWARD_WAIT` the hold expires into the digest — the
  quiet channel — rather than vanishing or turning into a push an hour late.

  ANYTHING THAT IS ALREADY AN ASK IS NEVER HELD. "Hi, can you check booking
  88271?" opens with a greeting and is not one. Holding it would delay the very
  thing the steward exists to get to him sooner.

  URGENCY OUTRANKS THE GREETING, ALWAYS. "Hi, prod is down" must never wait for
  a second message. The steward is not permitted to be the reason an outage sat
  in a hold.

One door, used by both callers: `chat_watch` decides what reaches his phone and
`responder` decides what gets investigated, and they must not disagree about
whether a message is a greeting — that split is exactly the shape of the bug in
[[asta-consistency-across-brains]].
"""

from __future__ import annotations

import os
import re
import time

#: How long a greeting waits for the message that explains it.
WAIT_SECONDS = float(os.environ.get("ASTA_STEWARD_WAIT", "1800"))

#: On unless turned off, like the front desk.
def enabled() -> bool:
    return os.environ.get("ASTA_STEWARD", "1").strip().lower() not in ("0", "false", "no")


#: Nothing but a greeting. Anchored at both ends: the whole message has to be
#: the greeting, because the moment anything follows it, that something is the
#: message and this is just how they opened it.
_GREETING = re.compile(
    r"^\W*(?:hi|hii+|hey+|hello+|helo|yo|hai|namaste|good\s+(?:morning|afternoon|evening)|"
    r"gm|gud\s+mrng)"
    r"(?:\s+(?:there|arun|bro|buddy|mate|sir|team|all))?"
    r"[\s\W]*$", re.I)

#: A greeting followed by nothing but a check that he is present. Still a
#: greeting: "are you there?" is not an ask, it is waiting for one.
_STILL_OPENING = re.compile(
    r"^\W*(?:hi|hey|hello|yo|hai)?[\s,]*"
    r"(?:are\s+you\s+(?:there|around|free|available)|you\s+(?:there|around|free)|"
    r"r\s+u\s+there|available\?|free\?|ping|need\s+(?:a\s+)?minute|quick\s+question)"
    r"[\s\W]*$", re.I)

#: Words that mean this cannot wait for a second message, whatever it opens with.
_URGENT = re.compile(
    r"\b(?:prod(?:uction)?|outage|down|sev\s?\d|p1|p2|incident|urgent|asap|critical|"
    r"blocked|blocker|failing|broken|customer|escalat)", re.I)


def is_greeting(text: str) -> bool:
    """Is this message ONLY an opening — nothing to act on yet?"""
    said = (text or "").strip()
    if not said or _URGENT.search(said):
        return False
    return bool(_GREETING.match(said) or _STILL_OPENING.match(said))


def _key(who: str) -> str:
    return f"steward:{(who or '').strip()}"


def consider(who: str, text: str) -> dict:
    """What to do with this message from `who`.

    Returns {"hold": bool, "text": str, "opened_with": str}. `text` is what the
    rest of Asta should treat as the message — for a released hold that is the
    greeting and the ask together, because they are one conversation and the
    notification he reads should show it that way.
    """
    from . import store
    if not enabled():
        return {"hold": False, "text": text or "", "opened_with": ""}
    waiting = _waiting(who)
    if is_greeting(text):
        if not waiting:
            store.kv_set(_key(who), f"waiting|{time.time():.0f}|{(text or '').strip()[:120]}")
        # A second "hello?" while already waiting is the same person still
        # waiting. It does not restart the clock and it does not push.
        _record("held", who, text)
        return {"hold": True, "text": text or "", "opened_with": ""}
    if waiting:
        store.kv_set(_key(who), "")
        _record("released", who, text)
        return {"hold": False, "text": text or "", "opened_with": waiting}
    _record("straight", who, text)
    return {"hold": False, "text": text or "", "opened_with": ""}


def _record(route: str, who: str, text: str) -> None:
    """Every message's route, the way the front desk records its own.

    Without this the steward is invisible: nobody can say how many
    interruptions it saved, and a scenario cannot see it work at all.
    """
    import contextlib

    from . import store
    with contextlib.suppress(Exception):
        store.record_outcome("steward", route, detail=f"{who}: {(text or '')[:120]}")


def _waiting(who: str) -> str:
    """The greeting this person is waiting on, or '' — expired holds do not count."""
    from . import store
    row = store.kv_get(_key(who)) or ""
    if not row.startswith("waiting|"):
        return ""
    try:
        _, at, said = row.split("|", 2)
    except ValueError:
        return ""
    if time.time() - float(at) > WAIT_SECONDS:
        return ""
    return said


def holding() -> list[dict]:
    """Everyone who has said hello and not yet said why. Newest first."""
    from . import store
    out = []
    for key, value in (store.kv_like("steward:%") or {}).items():
        if not str(value).startswith("waiting|"):
            continue
        try:
            _, at, said = str(value).split("|", 2)
        except ValueError:
            continue
        out.append({"who": key.split(":", 1)[1], "at": float(at), "said": said,
                    "expired": time.time() - float(at) > WAIT_SECONDS})
    return sorted(out, key=lambda r: -r["at"])


def expired() -> list[dict]:
    """Holds that have waited long enough to be worth mentioning quietly.

    They go in the digest and are then forgotten. A push an hour after somebody
    said "hi" is the interruption this module exists to avoid, arriving late.
    """
    from . import store
    out = [r for r in holding() if r["expired"]]
    for row in out:
        store.kv_set(_key(row["who"]), "")
    return out


def line_for(rows: list[dict]) -> str:
    """One digest line for the people who said hello and nothing else."""
    if not rows:
        return ""
    names = ", ".join(r["who"] for r in rows[:4])
    more = f" and {len(rows) - 4} more" if len(rows) > 4 else ""
    return (f"👋 {names}{more} said hello but never said why — "
            f"nothing to act on, mentioning it in case you want to ask.")
