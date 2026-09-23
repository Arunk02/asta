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

import datetime as dt
import contextlib
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

#: Things Asta itself said. An announcement is not an obligation — a health
#: report, a finished task, a meeting reminder and a question Asta asked are all
#: things Asta PRODUCED, and none of them is owed back to anyone.
#:
#: Filing them under the same source as an arriving mail made the ledger read
#: Asta's own speech as Arun's backlog. And because a chase message is itself a
#: push, each chase was filed and then chased in turn: "still waiting on you (2)"
#: whose single item was "still waiting on you (3)" whose single item was "still
#: waiting on you (13)". Live rows, not a hypothetical.
#:
#: They are still RECORDED — the audit trail is worth having, and an identical
#: repeat is still suppressed. They are simply never owed.
SELF_SOURCE = "asta"


def self_originated(row: dict) -> bool:
    """True when everything that produced this row was Asta itself.

    Checks the accumulated `sources`, not just the first one, so the judgement
    survives a merge in either direction: a real arrival landing on a key Asta
    happened to create makes the row owed again, which is the honest answer.
    """
    joined = row.get("sources") or row.get("source") or ""
    sources = [s for s in joined.split(",") if s]
    return bool(sources) and all(s == SELF_SOURCE for s in sources)


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


# --- when is it wanted by? ---------------------------------------------------
#
# The words were already being matched — triage's `_ASK` has `eod|asap|urgent`
# in it — and then thrown away, because the only thing they could affect was a
# bool that was already true. Parsing them into an actual time is what lets
# "approve by EOD, we ship tonight" outrank "any update?", which is the whole
# complaint. Free: regex and arithmetic, no model.

def eod_hour() -> int:
    try:
        return min(23, max(0, int(os.environ.get("ASTA_EOD_HOUR", "18"))))
    except ValueError:
        return 18


def urgent_within_hours() -> float:
    """A deadline closer than this makes something interrupt-now rather than today."""
    try:
        return max(0.0, float(os.environ.get("ASTA_URGENT_HOURS", "4")))
    except ValueError:
        return 4.0


_ASAP = re.compile(r"\b(asap|as soon as possible|urgent(ly)?|immediately|right away)\b", re.I)
_EOD = re.compile(r"\b(eod|end of (the )?day|cob|close of business)\b", re.I)
_BY_TIME = re.compile(r"\bby\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)
_TODAY = re.compile(r"\b(today|this afternoon|this evening|tonight)\b", re.I)
_TOMORROW = re.compile(r"\b(tomorrow|first thing tomorrow)\b", re.I)
_BY_WEEKDAY = re.compile(r"\bby\s+(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", re.I)
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def deadline(text: str, now: float | None = None) -> float | None:
    """The time this is wanted by, or None. The EARLIEST candidate wins.

    A deadline already in the past is returned as-is rather than rolled forward
    to tomorrow: "by 3pm" read at 4pm means he is late, and late is the most
    urgent state there is — quietly reinterpreting it as tomorrow would hide
    exactly the thing worth telling him.
    """
    if not text:
        return None
    now = time.time() if now is None else now
    base = dt.datetime.fromtimestamp(now)
    end = eod_hour()
    found: list[float] = []

    def at(day_offset: int, hour: int, minute: int = 0) -> float:
        stamp = (base + dt.timedelta(days=day_offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        return stamp.timestamp()

    if _ASAP.search(text):
        found.append(now)
    if _EOD.search(text) or _TODAY.search(text):
        found.append(at(0, end))
    if _TOMORROW.search(text):
        found.append(at(1, end))
    m = _BY_TIME.search(text)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        found.append(at(0, hour, int(m.group(2) or 0)))
    w = _BY_WEEKDAY.search(text)
    if w:
        want = _WEEKDAYS.index(w.group(1).lower())
        found.append(at((want - base.weekday()) % 7, end))
    return min(found) if found else None


# --- how much does it want him? ----------------------------------------------

#: How many sightings of an unanswered thing before it counts as being chased.
def chase_at() -> int:
    try:
        return max(2, int(os.environ.get("ASTA_CHASE_AT", "3")))
    except ValueError:
        return 3


#: Already breakage, rather than something that might settle. Lives HERE, next to
#: `score`, because it is a ranking decision and ranking has exactly one home.
#:
#: It used to live in `outlook` and `outlook` was its only caller, so an outage
#: that arrived by mail interrupted him and the SAME outage posted in a Teams
#: channel scored "no ask detected" — nobody asks anything when prod falls over —
#: and went down the ambient path, which presence suppresses while he is at the
#: laptop. Per-source drift, the same shape as every other per-source constant
#: that has bitten this codebase.
_CRITICAL = re.compile(
    r"\b(down|outage|unavailable|unreachable|not responding|no longer responding|"
    r"crash ?loop|crashloopbackoff|oom ?killed|out of memory|"
    r"pods? (are )?(down|restarting|failing|not ready|unavailable)|"
    r"service (is )?(down|unavailable|degraded)|"
    r"cannot connect|connection refused|refusing connections|"
    r"p1|sev ?1|severity ?1|critical|data loss|all requests failing)\b", re.I)


def looks_critical(*parts: str) -> bool:
    """Whether this text describes something already broken.

    Takes loose parts so a caller can pass subject and body, or one rendered feed
    row, without either of them having to know the other's shape.
    """
    return bool(_CRITICAL.search(" ".join(p for p in parts if p)))


def score(action: bool, text: str, *, addressed: bool = False, critical: bool = False,
          key: str = "", who: str = "", now: float | None = None
          ) -> tuple[int, str, float | None]:
    """Rank one arrival: (priority, why, due_at).

    The rules are ordered by how OBJECTIVE the signal is, the same discipline the
    relevance gate uses — something that is provably broken outranks something
    that merely reads as urgent, which outranks a judgement about wording.

    `key` is optional and only used to look up how many times this has already
    been seen unanswered. That lookup is why ranking needed the ledger: a third
    chase is a fact about history, and no amount of reading one message reveals
    it.
    """
    now = time.time() if now is None else now
    due = deadline(text, now)

    if critical:
        return P_NOW, "something is broken", due
    if action and due is not None and due <= now + urgent_within_hours() * 3600:
        base, why = P_NOW, "asked for, and due within hours"
    elif action:
        base, why = P_TODAY, "asks you directly" if addressed else "asks for something"
    elif addressed:
        base, why = P_FYI, "addressed to you, no ask"
    else:
        base, why = P_FYI, "no ask detected"

    # Who sent it is the LAST word, never the first: it can nudge a rank the
    # message already earned, and it is capped at one tier so a statistic can
    # never quietly overrule the message itself.
    from . import contacts
    adjusted, note = contacts.adjust(base, who)
    return adjusted, (f"{why} · {note}" if note else why), due


#: Queues and groups Arun is ON. Work assigned to one of these is assigned to
#: HIM — "assigned to group OH - TELIKOS - L2" is not a broadcast, it is a
#: ticket landing in his queue. Configured rather than guessed: nothing can infer
#: which rotas somebody is on, and guessing wrong in the quiet direction is how a
#: real incident goes unread.
MY_GROUPS = tuple(g.strip().lower() for g in
                  os.environ.get("ASTA_MY_GROUPS", "").split(",") if g.strip())


def assigned_to_him(text: str) -> bool:
    """Whether this lands in a queue he is on, however it is worded."""
    low = (text or "").lower()
    return bool(MY_GROUPS) and any(g in low for g in MY_GROUPS)


#: An acknowledgement, not a request. "X reacted to your message" carries the
#: "in chat with you" marker like every other row, so without this every emoji
#: somebody leaves on his message would become a direct push — the noise that
#: gets a notifier muted, and a muted notifier loses the real ones too.
_REACTION = re.compile(r"\b(reacted to|liked|hearted)\b.{0,24}\byour\b", re.I)


def is_reaction(text: str) -> bool:
    return bool(_REACTION.search(text or ""))


#: Broadcasts. "Everyone please review PR for the fix of …" is addressed to a
#: channel, and "Action Required: Review Confluence & Jira Space Permissions"
#: went to the whole company — neither is somebody waiting on Arun.
_BROADCAST = re.compile(
    r"\b(everyone|all|team|folks|guys|hi all|dear (colleagues?|all|team))\b"
    r"\s*(please|,|:|-)|\baction required\b|\bdo not reply\b|\bno[- ]reply\b",
    re.I)

#: A message that OPENS by addressing the room. The clause above needed the room
#: word to be followed by punctuation or "please", which "Everyone all the new
#: development and bug fixes are getting deployed to MDP" is not — and that one
#: reached his chase list. Anchored to the start, because "we told everyone" in
#: the middle of a sentence is somebody talking TO him about a room.
_ROOM_OPENER = re.compile(
    r"^\s*(hi|hello|hey)?\s*(everyone|all|team|folks|guys|channel|"
    r"dear (colleagues?|all|team))\b", re.I)

#: Senders that address a room by their nature — a meeting bot, a reminder feed.
#: Narrow on purpose: over-filtering costs him a real message, which is the worse
#: failure. "Facilitator" is the one that actually reached him.
_ROOM_SENDER = re.compile(r"^(facilitator|moderator|reminders?|announcements?|"
                          r"notifications?|bot|polls?|surveys?|forms?)\b", re.I)

#: A poll or survey posted to a room. "Poll: Do you plan to attend the OHP AL
#: Days event from office tomorrow?" is a question, so every ask-detector says
#: yes, and it is addressed to a room of forty people. It reached his chase list
#: as something he personally owed an answer to.
_POLL = re.compile(r"^\s*(poll|survey)\b\s*[:\-]|\bnames not recorded\b|"
                   r"\bresults shared\b", re.I)


def from_bulk_sender(who: str) -> bool:
    """Senders that broadcast by nature — the list the mail path already keeps,
    so a name added there is understood here too."""
    from . import outlook
    return bool(who) and bool(outlook._BULK_SENDER.search(who))


def addressed_to_a_room(who: str, text: str) -> bool:
    """The RANKING half of "is this a broadcast" — what they wrote, not who sent it.

    Deliberately not `is_broadcast`. That one also asks `from_bulk_sender`, and
    the bulk-sender list contains `servicenow` — so ranking on it demoted an
    assigned incident ("INC4471 booking service down") to FYI and it stopped
    reaching him. That exact bug is already recorded a few lines below, in the
    comment about ServiceNow's exemption not surviving to the ranking layer;
    this is the version that does not re-create it.
    """
    return (bool(_ROOM_SENDER.search((who or "").strip()))
            or bool(_POLL.search(text or ""))
            or bool(_ROOM_OPENER.search(text or ""))
            or bool(_BROADCAST.search(text or "")))


def is_broadcast(who: str, text: str) -> bool:
    """Addressed to a room rather than to him.

    Lived in `responder`, where it decided whether to spend a turn investigating
    — and nothing else ever asked. So a deployment announcement ("Everyone all
    the new development and bug fixes are getting deployed to MDP only so please
    use the MDP server") was not investigated, and was still ranked as something
    he personally owed an answer to, and still appeared under "Still waiting on
    you". He is not even in that conversation: "im not even in the contest but it
    still asking it waiting for me".

    A ranking signal belongs in the ranking policy, where every source gets it.
    """
    return (from_bulk_sender(who)
            or bool(_ROOM_SENDER.search((who or "").strip()))
            or bool(_ROOM_OPENER.search(text or ""))
            or bool(_BROADCAST.search(text or "")))


def rank(action: bool, text: str, *, addressed: bool = False, key: str = "",
         who: str = "", now: float | None = None) -> tuple[int, str, float | None]:
    """The WHOLE per-arrival ranking: criticality, score, chase escalation.

    One function because there is one policy. Every caller that assembled these
    three steps by hand assembled them slightly differently — the mail path passed
    `critical`, the Teams path did not, and an outage in a channel was filed under
    "FYI, nothing needed from you" as a result. The bench then reimplemented the
    same three steps a third time and measured its own copy, which passed while
    the product was wrong.

    So: callers rank, they do not compose. Adding a signal here reaches every
    source at once, which is the only version of this that stays true.
    """
    mine = assigned_to_him(text)
    pri, why, due = score(action or mine, text, addressed=addressed or mine,
                          critical=looks_critical(text), key=key, who=who, now=now)
    if mine:
        # A ticket in his own queue must never be silenced by what the SENDER
        # usually sends. IT Service Desk sends mostly noise, so its act-rate is
        # low, so `contacts.adjust` dropped every incident to P_MUTE — recorded,
        # never pushed. `outlook.needs_attention` already exempts ServiceNow from
        # the bulk filter; the exemption simply did not survive to the ranking
        # layer, which is the same shape as every other per-layer drift here.
        pri = min(pri, P_TODAY)
        why = f"assigned to your group · {why}"
    elif addressed_to_a_room(who, text) and not looks_critical(text):
        # Never floored up to "today". A room-wide announcement lands in the same
        # group he was talking in five minutes ago, so the engaged-window half of
        # `addressed` says yes to it — which is exactly how these reached the
        # chase list. Being in the room is not being asked.
        #
        # An outage announced to a room is still an outage, so criticality wins:
        # the cost of being wrong here is him not hearing that production is
        # down, against the cost of one more line on his phone.
        pri = max(pri, P_FYI)
        why = f"announcement to the room · {why}"
    elif addressed and not is_reaction(text):
        # Somebody used his name. That IS the ask, whether or not the sentence
        # parses as one — "hi Arunkumar K" contains no ask verb and is
        # unmistakably someone wanting him. Ranked P_FYI it went out ambient, and
        # ambient is held while he is at the laptop, so on 28 August four people
        # tagged him between 11:58 and 13:04 and not one reached his phone. He had
        # already said the rule: "if they tag me u have to respond to me".
        pri = min(pri, P_TODAY)
        why = f"they tagged you · {why}"
    pri, chased = escalate_for_chase(pri, key, now=now)
    return pri, (chased or why), due


def escalate_for_chase(priority: int, key: str, now: float | None = None) -> tuple[int, str]:
    """Bump something being chased. Returns (priority, why-suffix).

    Someone asking a third time is a stronger signal than anything in the wording
    of the first message, and it is invisible without the ledger. Only unanswered
    things escalate — a thing he dealt with is not a chase, it is a thank-you.
    """
    if not key or priority <= P_NOW:
        return priority, ""
    row = store.attention_get(key)
    if not row or row.get("state") in SETTLED:
        return priority, ""
    seen = int(row.get("seen_count") or 0)
    if seen + 1 < chase_at():
        return priority, ""
    return priority - 1, f"chased {seen + 1}×"


LABELS = {P_NOW: "now", P_TODAY: "today", P_FYI: "FYI", P_MUTE: "muted"}

#: One mark per level, and the ONLY place a priority becomes a symbol.
#:
#: The ledger has always ranked four ways. The message on his phone showed two:
#: `"🔴" if priority <= 1 else "·"`. P_TODAY is the biggest bucket by far — 1,211
#: rows against 239 P_NOW — so almost everything arrived red, and 365 of the last
#: 571 Teams pushes carried the same mark. "same red tag for all image can u
#: prioritise does it on priority or not". A ladder that cannot tell an outage
#: from a question is not a priority, it is decoration.
MARKS = {P_NOW: "🚨", P_TODAY: "🔴", P_FYI: "🟡", P_MUTE: "·"}


def marker(priority: int | None) -> str:
    """The symbol for a rank — "·" for unranked, which claims nothing."""
    if priority is None:
        return "·"
    return MARKS.get(max(P_NOW, min(P_MUTE, int(priority))), "·")


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
    # A source he keeps ignoring is read, not buzzed. Learned from his own
    # reactions; he can overrule it in either direction at any time.
    reason = to_digest(source, who, priority, now=now)
    if reason:
        from . import digest
        digest.add(what or key, source=who or source, why=reason)
        store.attention_set(key, state="notified",
                            notified_at=time.time() if now is None else now)
        store.record_outcome("attention", "digested", subject=str(key)[:80], detail=reason[:200])
        notice = note_demoted(source, who, reason)
        if notice:
            import asyncio
            from . import notify as notify_mod
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop()
                asyncio.ensure_future(notify_mod.notify(notice, "attention",
                                                        urgency="direct", considered=True))
        return False
    store.attention_set(key, state="notified", notified_at=time.time() if now is None else now)
    return True


# --- what he has actually done with this source before -------------------------------
#
# R6: attention was rule-based, never learned. His ledger holds 2,115 reactions —
# the top ignored source was ignored 122 times in 30 days — and none of it fed
# back into the decision to buzz him. It does now, in the one place that decides.

#: How many items from a source before its record means anything.
MIN_SEEN = 10
#: Ignored at least this often, and it stops interrupting and starts digesting.
IGNORE_SHARE = 0.8
#: How long an announced item may sit untouched before it counts as ignored.
IGNORED_AFTER = 24 * 3600


def history(source: str, who: str = "", days: int = 30,
            now: float | None = None) -> tuple[int, int]:
    """(handled, ignored) for this source — from his own reactions, not a rule."""
    now = time.time() if now is None else now
    handled = ignored = 0
    for row in store.attention_history(source, who, since=now - days * 86400):
        if row.get("acted_at") or row.get("state") == "acted":
            handled += 1
        elif row.get("state") == "dropped":
            ignored += 1
        elif row.get("state") == "notified" and row.get("notified_at") \
                and now - float(row["notified_at"]) > IGNORED_AFTER:
            ignored += 1
    return handled, ignored


def forced(source: str, who: str = "") -> str:
    """'push' or 'digest' when he has said so himself, else ''.

    He names what he SEES — "push IT Service Desk", the sender — while the code
    holds a source and a person. Both spellings are looked up, or his undo
    silently does nothing.
    """
    for name in (f"{source}:{who}" if who else "", who, source):
        if name:
            value = store.kv_get(f"attention_force:{name.strip().lower()}") or ""
            if value:
                return value
    return ""


def set_force(name: str, how: str) -> str:
    store.kv_set(f"attention_force:{name.strip().lower()}", how)
    return (f"“{name}” will interrupt you again." if how == "push"
            else f"“{name}” goes to the digest from now on.")


def learns() -> bool:
    """Whether a source may be moved to the digest on its own record."""
    return os.environ.get("ASTA_ATTENTION_LEARN", "1").strip().lower() not in ("0", "false", "no", "off")


def to_digest(source: str, who: str = "", priority: int = P_FYI,
              now: float | None = None) -> str:
    """Should this go to the digest instead of his phone? The reason, or ''.

    Never for breakage, never for a source he has told Asta to keep pushing, and
    never on a thin record — ten items at least, and four in five ignored.
    """
    if int(priority) <= P_NOW:
        return ""
    how = forced(source, who)
    if how == "push":
        return ""
    if how == "digest":
        return "you asked for this one in the digest"
    if not learns():
        return ""
    from . import settings
    min_seen = int(settings.effective("ASTA_ATTENTION_MIN_SEEN", MIN_SEEN))
    share = settings.effective("ASTA_ATTENTION_IGNORE_SHARE", IGNORE_SHARE)
    # Two things his ledger cannot see, both found on his real data (16 Sep):
    #
    #   ASTA'S OWN VOICE sits at 226 of 226 "ignored" — nothing he does with a
    #   plan gate is recorded as acting on it. Digesting that would silence the
    #   gate itself.
    #
    #   A PERSON he answers IN TEAMS looks ignored too: four colleagues sit at
    #   100%, because the reply happens where Asta cannot watch. Burying a
    #   colleague's ask on that evidence is the one failure this must not have.
    #
    # So the record only ever demotes a feed that broadcasts by nature — the
    # ticket desk, a no-reply notifier. A person is demoted only if HE says so.
    if source == SELF_SOURCE or not from_bulk_sender(who or source):
        return ""
    handled, ignored = history(source, who, now=now)
    seen = handled + ignored
    if seen >= min_seen and ignored / seen >= share:
        return f"you ignored {ignored} of the last {seen} from {who or source}"
    return ""


def note_demoted(source: str, who: str, why: str) -> str:
    """Tell him ONCE that a source moved to the digest, and how to undo it. ''
    after the first time — a notice that repeats is the noise it is preventing."""
    name = (who or source).strip()
    key = f"attention_demoted:{name.lower()}"
    if store.kv_get(key):
        return ""
    store.kv_set(key, "1")
    store.record_outcome("attention", "demoted", subject=name[:80], detail=why[:200])
    return (f"🔕 Moving *{name}* to the digest — {why}. "
            f"Say “push {name}” to have it interrupt you again.")


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


# --- did the interruption earn its place? ------------------------------------
#
# `quality.py` scores plans, tasks, drafts, verification and relevance — and not
# this, the thing that actually interrupts him. So the accuracy of every filter
# above was a feeling rather than a number, and "it pushes too much" could only
# ever be argued about.
#
# The labels cost nothing because Arun already produces them: he deals with a
# thing, or he does not. Both are recorded, and NEITHER changes what Asta does
# yet — measure first, act on the data later, the same order the relevance gate
# was built in. What this buys immediately is per-tier precision: of the things
# ranked P0, how many did he actually touch, is the number that says whether P0
# means anything.

def _label(row: dict, outcome: str) -> None:
    store.record_outcome(
        "attention", outcome, subject=str(row.get("key") or "")[:80],
        detail=f"p{row.get('priority')} source={row.get('sources') or row.get('source')} "
               f"seen={row.get('seen_count')}")
    # The same label is what a person's reputation is made of. Recorded even when
    # the contacts prior is switched off, so flipping it on later starts with real
    # history instead of an empty table that has to earn its evidence from zero.
    from . import contacts
    contacts.record(str(row.get("who") or ""), outcome)


def mark_acted(key: str, now: float | None = None, why: str = "acted") -> None:
    """He dealt with it — by replying, by asking Asta to, or elsewhere entirely."""
    if not key:
        return
    row = store.attention_get(key)
    if not row or row.get("state") in SETTLED:
        return
    store.attention_set(key, state="acted", acted_at=time.time() if now is None else now)
    if row.get("state") == "notified":
        # Only a thing he was actually TOLD about can score the telling. Something
        # settled before it was ever announced says nothing about the filter.
        _label(row, why)


def settle_with(who: str, why: str = "he replied", now: float | None = None) -> int:
    """Everything this person was owed is settled — he has now answered them.

    The gap this closes is the one Arun hit hardest. Nothing in the live product
    ever called `mark_acted`: the only caller was the bench. So an item entered
    the ledger as `notified` and stayed there for ever, and "⏳ Still waiting on
    you" listed questions he had answered hours earlier — including ones Asta had
    itself sent the reply to. 1,779 rows sat in `notified` against 90 `acted`.

    Settling by PERSON rather than by key is deliberate. A reply in a thread
    answers the conversation, not one sentence of it: Alex asked three things
    in four minutes and one "yes" covers all three. Settling only the exact key
    would have left the other two chasing him at end of day.

    Returns how many it settled, so a caller can say so rather than guess.
    """
    n = 0
    for row in store.attention_open_from(who):
        if self_originated(row):
            continue
        mark_acted(row["key"], now=now, why=why)
        n += 1
    return n


def reconcile(now: float | None = None) -> int:
    """Settle everything he has demonstrably already answered. Returns the count.

    A repair, not a policy. Nothing in the live product had ever called
    `mark_acted`, so the ledger accumulated 1,779 rows in `notified` against 90
    `acted` — every question anyone had ever asked him, permanently owed. The
    rules above stop it happening again; this clears what the old behaviour left
    behind, using the same evidence: a message of his, in that thread, later than
    the ask.
    """
    from . import store as _store
    settled = 0
    for row in _store.attention_open(limit=2000, max_priority=P_MUTE):
        if self_originated(row):
            mark_dropped(row["key"])          # Asta's own notice, never his to answer
            settled += 1
            continue
        who = (row.get("who") or "").strip()
        # Today's rules applied to yesterday's rows — the point of a repair. A
        # room-wide announcement was never his to answer, whenever it arrived.
        #
        # The stored summary is "<sender>: <message>", and the room-opener rule is
        # anchored to the start of what they WROTE — so the prefix has to come off
        # or "Marlowe Kumar Singh: Everyone all the new development…" reads as an
        # ordinary sentence. Only the row's own sender is stripped, never an
        # arbitrary prefix.
        said = row.get("what") or ""
        if who and said.lower().startswith(f"{who.lower()}:"):
            said = said[len(who) + 1:].lstrip()
        if addressed_to_a_room(who, said):
            mark_dropped(row["key"])
            settled += 1
            continue
        if he_replied_since(row):
            mark_acted(row["key"], now=now, why="he replied")
            settled += 1
    return settled


def he_replied_since(row: dict) -> bool:
    """Is there a message of his in this person's thread, later than the ask?

    The evidence `reconcile` uses, callable for one row: the reply-grace in
    app/notify.py asks it before a held Teams message goes to his phone, because
    nothing else settles an ask he answered by typing in Teams himself — the
    watcher skips his own messages, and they only land in the stored thread.
    """
    from . import chat_watch
    who = (row.get("who") or "").strip()
    if not who:
        return False
    seen_at = row.get("first_seen") or 0
    try:
        # Windowed on the row's own timestamp — see `answered_by_him` for why
        # an unwindowed read of a long thread can never contain his reply.
        msgs = store.teams_messages(chat=who, since=seen_at, limit=100)
    except Exception:                                          # noqa: BLE001
        return False
    return any(chat_watch.is_from_him(m.get("sender", "")) and m.get("sent_at")
               and m["sent_at"] > seen_at for m in msgs)


def mark_dropped(key: str, why: str = "") -> None:
    """It stopped mattering without him doing anything (an alert that recovered).

    Not a label either way: he neither engaged nor ignored it, the world moved on.
    Scoring it as noise would punish the filter for a recovery it reported
    correctly.
    """
    if not key:
        return
    row = store.attention_get(key)
    if not row or row.get("state") in SETTLED:
        return
    store.attention_set(key, state="dropped")
    if why:
        _label(row, why)


def note_read(key: str) -> None:
    """He opened it somewhere else — on his phone, in Outlook, in Teams.

    The cheapest honest engagement signal there is, and it was already being
    scraped and thrown away: the activity feed reports `unread`, and a mail row
    says whether it is still bold. If he read it, the interruption did its job.
    """
    mark_acted(key, why="read_elsewhere")


def mute(key: str, row: dict | None = None) -> None:
    """He said stop telling him about this. Recorded, so 'why didn't you say'
    has an answer — a silent drop cannot explain itself later."""
    if not key:
        return
    row = row or store.attention_get(key) or {"key": key}
    store.attention_set(key, state="dropped", priority=P_MUTE)
    _label(row, "muted")


def settle_stale(days: int = 7, now: float | None = None) -> int:
    """Anything announced and never dealt with is, in the end, noise.

    Deliberately generous. A week is long enough that "he was busy" or "he was on
    leave" has been ruled out, so what is left is the honest reading: Asta chose
    to interrupt him and he never cared. That is the label the filter needs, and
    the one it has never had.
    """
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    settled = 0
    for row in store.attention_open(limit=500, max_priority=P_FYI):
        if row.get("state") != "notified" or self_originated(row):
            # Labelling Asta's own announcement "ignored" would teach the filter
            # that its own voice is noise, and the precision numbers that decide
            # whether a tier means anything would be measuring the wrong thing.
            continue
        if float(row.get("notified_at") or 0) > cutoff:
            continue
        store.attention_set(row["key"], state="dropped")
        _label(row, "ignored")
        settled += 1
    return settled


#: Outcomes that mean the interruption was worth making. `read_elsewhere` counts:
#: he dealt with it on his phone rather than through Asta, which says the thing
#: mattered — only whether ASTA was the one to handle it is different.
ENGAGED = ("acted", "read_elsewhere")

_TIER_IN_DETAIL = re.compile(r"\bp(\d)\b")


def precision(days: int = 7) -> dict:
    """Of what Asta interrupted him with, how much did he engage? Per tier.

    The per-tier split is the point. One overall number cannot tell "P0 is
    miscalibrated" from "there is simply a lot of FYI", and those want opposite
    fixes — one is a ranking bug, the other is working as intended.
    """
    since = time.time() - days * 86400
    tiers: dict[str, dict[str, int]] = {}
    for row in store.recent_outcomes(1000):
        if row.get("kind") != "attention" or float(row.get("created_at") or 0) < since:
            continue
        m = _TIER_IN_DETAIL.search(row.get("detail") or "")
        tier = LABELS.get(int(m.group(1)), "?") if m else "?"
        counts = tiers.setdefault(tier, {"engaged": 0, "ignored": 0, "muted": 0})
        outcome = row.get("outcome") or ""
        if outcome in ENGAGED:
            counts["engaged"] += 1
        elif outcome == "muted":
            counts["muted"] += 1
        else:
            counts["ignored"] += 1
    out = {}
    for tier, counts in tiers.items():
        total = sum(counts.values())
        out[tier] = {**counts, "total": total,
                     "rate": round(counts["engaged"] / total, 2) if total else 0.0}
    return out


def open_items(limit: int = 50, max_priority: int = P_FYI) -> list[dict]:
    """Everything still owed, most urgent first — "what's on my plate", as a query.

    The morning brief currently answers this by re-scraping the whole inbox and
    calendar in a fresh Playwright session, ~20s, for data a watcher read five
    minutes earlier. Once things are recorded, that read is free.
    """
    return [r for r in store.attention_open(limit=limit, max_priority=max_priority)
            if not self_originated(r)]


def purge(days: int = 14, now: float | None = None) -> int:
    now = time.time() if now is None else now
    return store.attention_purge(now - days * 86400)


async def sweep_loop() -> None:
    """Hourly: label what he never dealt with, then drop old settled rows.

    Runs whatever the flag says. With the ledger off there is nothing in the
    table, so this is a no-op query — and leaving it unconditional means turning
    the flag on for a week and off again does not strand a pile of rows that
    never get their label or their cleanup.
    """
    import asyncio
    while True:
        await asyncio.sleep(3600)
        try:
            settle_stale()
            purge()
        except Exception:
            pass


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


_WATCH_KEY = "attention_watch_started:"


def note_scrape(source: str, now: float | None = None) -> None:
    """Record that `source` successfully read its surface just now.

    A success also CLEARS the last error. Without that the reason string outlives
    the fault it described: the Teams watcher failed once with "Teams app did not
    load within 75s", recovered on the next poll, and kept that sentence forever —
    so the next time it went stale for an unrelated reason, health would have
    quoted the old cause and sent Arun to look in the wrong place.

    Same rule as everywhere else here: report the current state, not the last
    state anyone happened to write down.
    """
    if source:
        store.kv_set(_SCRAPE_KEY + source, str(time.time() if now is None else now))
        store.kv_set(_ERROR_KEY + source, "")


def note_watching(source: str, now: float | None = None) -> None:
    """Record that a watcher for `source` is RUNNING and expects to succeed.

    Without this the heartbeat has a hole exactly where it is needed most. "A
    source that never reported is switched off" is right for a bridge Arun never
    enabled — and wrong for a scrape that broke on its FIRST poll after a deploy,
    which then never reports, and so is never called broken. That is the shape
    the Teams activity watcher was found in: enabled, session healthy, silently
    failing every poll while Outlook beside it ran fine.

    Stamped once when the loop starts, and never overwritten while the process
    lives, so "started an hour ago and has still never read anything" is a
    question the ledger can answer.
    """
    if source and not store.kv_get(_WATCH_KEY + source):
        store.kv_set(_WATCH_KEY + source, str(time.time() if now is None else now))


def clear_watching(source: str) -> None:
    """Forget the start marker — used when a watcher stops on purpose."""
    if source:
        store.kv_del(_WATCH_KEY + source)


def last_scrape(source: str) -> float:
    try:
        return float(store.kv_get(_SCRAPE_KEY + source) or 0)
    except ValueError:
        return 0.0


def watching_since(source: str) -> float:
    try:
        return float(store.kv_get(_WATCH_KEY + source) or 0)
    except ValueError:
        return 0.0


def stale_sources(sources: tuple[str, ...] = ("outlook", "teams"),
                  now: float | None = None) -> dict[str, int]:
    """Sources that should be reading and are not — {source: minutes}.

    Two ways to qualify, and the second is the one that matters:
      - it worked before and has now gone quiet;
      - it has been RUNNING for longer than the window and has never once
        succeeded, which is a watcher that was broken from its first poll.

    A source that is neither running nor has ever reported is switched off, and
    alarming about a bridge Arun never enabled would be crying wolf on day one.
    """
    now = time.time() if now is None else now
    limit = stale_after_minutes()
    if limit <= 0:
        return {}
    out: dict[str, int] = {}
    for source in sources:
        last = last_scrape(source)
        if last > 0:
            minutes = int((now - last) // 60)
            if minutes >= limit:
                out[source] = minutes
            continue
        started = watching_since(source)
        if started > 0:
            minutes = int((now - started) // 60)
            if minutes >= limit:
                out[source] = minutes
    return out


def never_succeeded(source: str) -> bool:
    """True when the watcher is running but has not once managed to read."""
    return last_scrape(source) <= 0 and watching_since(source) > 0


_ERROR_KEY = "attention_scrape_error:"


def note_scrape_error(source: str, exc: BaseException) -> None:
    """Keep WHY a scrape failed, so a dead watcher can be fixed and not just found.

    Both loops catch every exception and continue, which is right — a transient
    DOM hiccup must not kill a watcher. But the reason was discarded, so the
    handler ran silently every five minutes and the only evidence a watcher had
    died was an absence. Knowing it is broken says to look; knowing it raised a
    selector timeout on the activity list says where.
    """
    if not source:
        return
    store.kv_set(_ERROR_KEY + source,
                 f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
                 if str(exc) else type(exc).__name__)


def last_error(source: str) -> str:
    return store.kv_get(_ERROR_KEY + source) or ""
