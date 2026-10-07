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

#: A greeting followed by nothing but a check that he is present, or by a
#: request for help that does not say what for. Still a greeting: "are you
#: there?" is not an ask, and neither is "need ur help" — both are waiting for
#: one.
#:
#: The help half was missing, and it cost the module its whole point. Navya's
#: "Hi Arunkumar, need ur help" matched neither pattern, so it was pushed to his
#: phone in red, carrying no information, and he had to drive the rest by hand.
#: A vocabulary that knows "quick question" but not "need your help" is the same
#: failure as a review that greps for `"type": "enum"`.
#:
#: Anchored at both ends, like _GREETING. "need ur help with booking 88271" HAS
#: the ask in it and must never be held — the trailing `[\s\W]*$` is what keeps
#: that true, so any new phrase added here inherits the guarantee.
_STILL_OPENING = re.compile(
    r"^\W*(?:hi+|hey+|hello+|yo|hai)?[\s,]*"
    # How they addressed him — "Hi Arunkumar, …". A name and a comma, nothing
    # more: without this the real message that started all of it, "Hi Arunkumar,
    # need ur help", failed on the name alone.
    r"(?:[\w.'-]{2,20}\s*,\s*)?"
    r"(?:are\s+you\s+(?:there|around|free|available)|you\s+(?:there|around|free)|"
    r"r\s+u\s+there|available\?|free\?|ping|need\s+(?:a\s+)?(?:minute|min|sec|second)|"
    r"quick\s+question|quick\s+one|"
    # "somebody wants something" with the something left out
    r"(?:i\s+)?need\s+(?:a\s+little|a\s+bit\s+of|ur|your|some|an?)?\s*"
    r"(?:help|favou?r|assistance)|"
    r"(?:can|could|cud)\s+(?:you|u)\s+(?:pls\s+|please\s+)?help(?:\s+me)?|"
    r"(?:got|have)\s+(?:a|1)\s+(?:minute|min|sec|second)|"
    r"small\s+help|need\s+help)"
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


#: The one outward act in Asta that does not wait for his yes.
#:
#: His decision, 28 Sep: *"once u recieved send short message get wht they want,
#: once you got u have to analyse and come back with the actual result and ask
#: me check once approve will send, so only final result approval should come to
#: user"*. So the clarifying question goes on its own, and the ANSWER — the part
#: that commits him to something — is staged for approval exactly as before.
#:
#: The justification is narrow and does not generalise: a question promises
#: nothing, states nothing on his behalf, and cannot be acted on by the person
#: receiving it. `responder`'s "auto-analyse, never auto-reply" still holds for
#: every other message; this is one sentence, to someone who has already started
#: talking to him, asking them to finish.
#:
#: Off unless switched on, like everything else that acts outward.
def ask_back_enabled() -> bool:
    return os.environ.get("ASTA_ASK_BACK", "1").strip().lower() in ("1", "true", "yes", "on")


#: Deliberately a question and nothing else. No "sure", no "I'll look at it" —
#: a commitment he never approved is exactly the thing this is not allowed to be.
#:
#: And courteous, because it goes out in HIS name to a colleague who has just
#: asked him for something. His rule: *"always use bit polite tone, not rude"* —
#: short is the house style, but short must not arrive as curt. "What do you
#: need?" is the shortest correct sentence here and reads like a ticket form.
#: "Sure, happy to help" would be warmer and is not available: it promises, in
#: his name, that he will help — and he has not seen the message yet. Courtesy
#: has to come from HOW it asks, not from agreeing to something on his behalf.
#:
#: "…what you need help with?" was the line until 1 Oct, and he called it out:
#: not everyone who writes to him is asking for help, and it reads as if they
#: were. "Yes, could you share a bit more on this?" asks the same thing politely.
ASK_BACK = os.environ.get(
    "ASTA_ASK_BACK_LINE", "Yes, could you share a bit more on this?")


#: The answer to a ping ("Bro", "Hi Arun"). It says he is there and asks nothing
#: about a subject — a ping has none, and guessing one from the earlier chat is
#: answering a conversation they did not start.
PING_BACK = "Yes, tell me"


def ack_line(chat: str, text: str = "") -> str:
    """A short receipt for the subject being checked, without claiming a result."""
    try:
        from . import writing
        terms = [t for t in writing.address_terms(chat) if t.lower() in ("bro", "da", "machi")]
    except Exception:                                          # noqa: BLE001
        terms = []
    from . import chat_watch
    body = chat_watch.clean_message(text).lower()
    from . import responder
    port = responder.counterpart(text or "")
    if port:
        line = (f"Got it, checking what {port['side']} needs for this" if port["side"]
                else "Got it, checking what our side needs for this")
    elif re.search(r"/pull/\d+|\b(?:pr|pull request)\b", body):
        line = "Looking into the PR details"
    elif re.search(r"\b(?:build|pipeline|ci)\b", body):
        line = "Checking the build"
    elif re.search(r"\b(?:booking|order|shipment)\b", body):
        line = "Looking into the booking"
    elif re.search(r"\b(?:ticket|jira)\b", body):
        line = "Checking the ticket details"
    elif re.search(r"\b(?:failed?|failing|error|logs?|issue)\b", body):
        line = "Looking into what went wrong"
    else:
        line = "Let me look into this"
    return f"{line}, {terms[0]}" if terms else line


def ping_back(chat: str) -> str:
    """The ping answer, with the term he actually uses for this person, if any."""
    try:
        from . import writing
        terms = [t for t in writing.address_terms(chat) if t.lower() in ("bro", "da", "machi")]
    except Exception:                                          # noqa: BLE001
        terms = []
    line = PING_BACK.strip() or "Yes, tell me"
    if terms and "," in line:
        head, rest = line.split(",", 1)
        return f"{head} {terms[0]},{rest}"
    return line


def opener_line(chat: str, who: str = "") -> str:
    """The answer to "hi Arunkumar": "hi Shabda, yes tell me".

    His words for it, 1 Oct — "ideally we should have asked yes please tell
    something kind of, not like what is the issue?". Their first name, and the
    term he uses with them when he has one; nothing about a subject."""
    first = (who or "").split()[0] if (who or "").split() else ""
    try:
        from . import writing
        terms = [t for t in writing.address_terms(chat) if t.lower() in ("bro", "da", "machi")]
    except Exception:                                          # noqa: BLE001
        terms = []
    name = terms[0] if terms else first
    return f"hi {name}, yes tell me" if name else "hi, yes tell me"


def _asked_key(who: str) -> str:
    return f"steward:asked:{(who or '').strip()}"


def ask_back_line(who: str, text: str) -> str:
    """The question to send this person, or '' to send nothing.

    '' whenever anything at all is uncertain: the flag is off, the message
    already carries the ask, it is urgent, or this person has already been asked
    and has not come back yet. A second "what do you need?" is not attentiveness.
    """
    from . import store
    if not (enabled() and ask_back_enabled()):
        return ""
    if not is_greeting(text):
        return ""                       # they already said what they want
    if (store.kv_get(_asked_key(who)) or "").strip():
        return ""                       # asked once; the ball is theirs
    return ASK_BACK


def note_asked_back(who: str) -> None:
    """Remember that this person has been asked, and record it.

    Recorded because this is the only send that nobody approved — an unapproved
    outward act that is also invisible is not something he could ever audit.
    """
    import contextlib

    from . import store
    store.kv_set(_asked_key(who), f"{time.time():.0f}")
    with contextlib.suppress(Exception):
        store.record_outcome("steward", "asked back", subject=(who or "")[:80],
                             detail=ASK_BACK)


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
