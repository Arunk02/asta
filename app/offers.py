"""Offers — "here is what I'd do next; shall I?" and then acting on a plain yes.

Between two bad extremes. Asta could stay silent until asked, which means a red
pipeline sits there all evening. Or it could act on everything it notices, which
burns tokens on things Arun already knows about and does not care about.

The middle is an OFFER: report the thing with enough context to decide, name what
it would do next, and wait. A bare "yes" from any channel then runs it. Cheap to
ask, bounded to act, and he stays the one who decides.

Offers chain, which is the point — each completed step offers the next:

    CI failed → "can I analyse?" → yes → analysis
              → "shall I raise the PR?"  → yes → PR raised
              → "where should I share the build?" → he names it → shared

That chain used to be the WHOLE mechanism: three hardcoded kinds, with the
instruction for each written into an if-chain in main. But the same shape is what
every other flow wants — implement this ticket, follow up on that thread, update
the status, do the thing you just described. So an offer now carries its OWN next
step, in one of two forms:

  action   a prompt for a brain, written by whoever proposed it (a recipe below,
           or the model itself via propose_next). Any flow, no new enum entry.
  op       a mechanical operation run in Python — post the comment, approve the
           PR. No brain, no tokens, no chance of the model paraphrasing what he
           approved into something else.

`op` is the important half. Outward writes are exactly where a confident wrong
move costs the most, so what he approves is a fixed record of the action, not an
instruction to go and perform it.

Unlike the conductor loop's in-memory state, offers are PERSISTED: the question
went to his phone, and he may answer twenty minutes later, from Telegram, after a
restart. An unanswered question that silently evaporates is how you train someone
to stop trusting the assistant. They do expire, though — a "yes" to something from
yesterday must not quietly kick off work he has forgotten proposing.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field, fields

from . import store

KEY = "pending_offer"


def ttl_seconds() -> int:
    """How long an offer stays answerable (default 6h). 0 = never expires."""
    try:
        return max(0, int(os.environ.get("ASTA_OFFER_TTL", "21600")))
    except ValueError:
        return 21600


@dataclass
class Offer:
    """A proposed next step, waiting on a yes."""

    id: str
    kind: str          # label for the flow: "analyse" | "raise_pr" | "jira_write" | "next" …
    subject: str       # the human label — "CI failed on main"
    context: str       # the precise detail he needs to decide, already gathered
    prompt: str        # the question as asked, so the yes is unambiguous
    created: float
    payload: dict = field(default_factory=dict)   # whatever the action needs
    action: str = ""   # instruction for a brain to run on yes ("" = use the kind's)
    op: dict = field(default_factory=dict)        # {"name": …, "args": {…}} run in Python

    def expired(self, now: float | None = None) -> bool:
        ttl = ttl_seconds()
        if ttl <= 0:
            return False
        return (time.time() if now is None else now) - self.created > ttl

    def mechanical(self) -> bool:
        """True when accepting runs code directly instead of asking a brain."""
        return bool(self.op.get("name"))

    def render(self) -> str:
        """What actually lands on his phone: context first, then the one question.

        Both answers are spelled out. "(reply yes)" alone made declining feel like
        it needed an explanation, so the honest no — the one that should be cheap —
        was the harder of the two to give.

        A QUEUED offer renders as a heads-up rather than a question. Every producer
        pushes `o.render()` the moment it stages, and once offers queue instead of
        clobbering, some of those are not the one a "yes" would answer. Inviting
        him to say yes to a question that is not the open one is precisely the
        ambiguity the single-slot design was protecting against — so the wording
        is decided HERE, by whether this offer is actually the one being asked,
        and every existing caller becomes correct without being touched.
        """
        body = f"{self.subject}\n{self.context}".strip()
        if not self.is_asked():
            head = pending()
            behind = f" behind “{head.subject}”" if head else ""
            return (f"{body}\n\n⏳ Queued{behind} — I'll ask when that one is "
                    f"answered. Say “{self.subject[:40]}” to jump to it.")
        return f"{body}\n\n▶ {self.prompt}\n   reply “yes” to go ahead, “no” to drop it."

    def is_asked(self) -> bool:
        """True when this is the offer a bare "yes" would accept."""
        head = pending()
        return head is not None and head.id == self.id


#: Offers waiting behind the one he was actually shown.
QUEUE_KEY = "offer_queue"

#: How many can wait. Past this the oldest is dropped: a backlog he will never
#: work through is not a queue, it is a way to lose the recent ones.
QUEUE_MAX = int(os.environ.get("ASTA_OFFER_QUEUE_MAX", "5"))


def offer(kind: str, subject: str, context: str, prompt: str,
          payload: dict | None = None, action: str = "",
          op: dict | None = None) -> Offer:
    """Record a proposal. QUEUES behind an unanswered one rather than replacing it.

    One is ASKED at a time, which is the property worth keeping: two open
    questions and a bare "yes" is ambiguous, and guessing is the confident wrong
    move that makes an assistant untrustworthy.

    But "one at a time" used to be implemented as one global slot that every new
    offer overwrote — and four background daemons stage offers (`refresh`,
    `ci_watch`, and two in `meetings`). So the sequence Arun hit was:

        12:59  "Ring Vinish on Teams?"        <- staged, shown to him
        ~13:00  a daemon offers something else <- silently takes the slot
        13:00  "Go ahead"                      <- lands on nothing, or worse,
                                                  on a question he never read

    He said yes three times and nothing rang. The brain re-staged each turn, was
    clobbered each turn, and eventually concluded approval must live in some
    other channel — a reasonable inference from what it could see, and wrong.

    The dangerous version of the same race is quieter: his yes accepts an outward
    write that replaced the one on screen. Whatever else is true, the thing he
    approves must be the thing he was shown.

    So the head is immutable while it is unanswered, and later offers wait.
    """
    o = Offer(id=uuid.uuid4().hex[:12], kind=kind, subject=subject, context=context,
              prompt=prompt, created=time.time(), payload=payload or {},
              action=action, op=op or {})
    head = pending()
    if head is None:
        store.kv_set(KEY, json.dumps(asdict(o)))
        return o
    # Re-proposing the question already on screen is a no-op, not a queue entry.
    # This is the exact loop from the transcript: the brain staged the call to
    # Vinish, Arun's yes did not reach it, and every following turn staged it
    # again. Without this the queue fills with copies of the very thing he is
    # being asked, and the id he was shown keeps changing underneath him.
    if _same_question(asdict(head), o):
        return head
    queued = _queue()
    # The same proposal twice is one proposal. Watchers re-detect the same state
    # on every pass — a stale context is still stale five minutes later — and
    # without this the queue fills with restatements of one thing and evicts the
    # offers that actually differ. Which is the failure mode a bounded queue is
    # supposed to prevent, arriving by another route.
    #
    # Replace rather than skip: the newer one carries fresher context ("21 days"
    # rather than "14"), and it is the same question either way.
    queued = [row for row in queued if not _same_question(row, o)]
    queued.append(asdict(o))
    # Newest wins when full. An offer he has not reached in five proposals is
    # stale anyway, and dropping the newest would hide what is happening NOW.
    store.kv_set(QUEUE_KEY, json.dumps(queued[-QUEUE_MAX:]))
    return o


def _same_question(row: dict, o: Offer) -> bool:
    """Two offers that would ask Arun the same thing.

    Compared on what the offer WOULD DO, not on its wording. Two staged calls to
    the same person are the same question however the sentence around them is
    phrased; two proposals about the same workspace's context are one piece of
    news. An id cannot answer this — every stage mints a new one.
    """
    if row.get("kind") != o.kind:
        return False
    row_op, new_op = row.get("op") or {}, o.op or {}
    if row_op.get("name") or new_op.get("name"):
        # A recorded call: identical name and arguments is the same act.
        return (row_op.get("name") == new_op.get("name")
                and row_op.get("args") == new_op.get("args"))
    return row.get("subject") == o.subject


def _queue() -> list[dict]:
    try:
        raw = store.kv_get(QUEUE_KEY)
        return json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return []


def waiting() -> list[Offer]:
    """Offers queued behind the current one, oldest first."""
    out = []
    for row in _queue():
        o = _load(json.dumps(row))
        if o is not None and not o.expired():
            out.append(o)
    return out


def _promote() -> Offer | None:
    """Move the next unexpired queued offer into the asked slot."""
    queued = _queue()
    while queued:
        row = queued.pop(0)
        o = _load(json.dumps(row))
        if o is not None and not o.expired():
            store.kv_set(KEY, json.dumps(asdict(o)))
            store.kv_set(QUEUE_KEY, json.dumps(queued))
            return o
    store.kv_set(QUEUE_KEY, json.dumps([]))
    return None


def _load(raw: str) -> Offer | None:
    """Rebuild an offer, tolerating rows written by an older or newer version.

    Unknown keys are dropped and missing ones take their defaults, so an upgrade
    mid-flight loses the extras rather than the question.
    """
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        known = {f.name for f in fields(Offer)}
        return Offer(**{k: v for k, v in data.items() if k in known})
    except Exception:
        return None


def pending() -> Offer | None:
    """The open offer, or None if there is none or it has aged out."""
    raw = store.kv_get(KEY)
    if not raw:
        return None
    o = _load(raw)
    if o is None:
        return None
    if o.expired():
        store.kv_set(KEY, "")
        return _promote()
    return o


def accept() -> Offer | None:
    """Consume the open offer — returns it exactly once, so a double "yes"
    cannot run the same work twice. The next queued offer takes its place."""
    o = pending()
    if o:
        clear()
        _promote()
    return o


def decline() -> Offer | None:
    o = pending()
    if o:
        clear()
        _promote()
    return o


def clear() -> None:
    """Drop the ASKED offer only. The queue behind it is untouched."""
    store.kv_set(KEY, "")


def drop_all() -> None:
    """Forget everything waiting — he changed the subject entirely."""
    store.kv_set(KEY, "")
    store.kv_set(QUEUE_KEY, json.dumps([]))


# --- proposing anything -----------------------------------------------------

def propose(subject: str, context: str, question: str, action: str,
            kind: str = "next", payload: dict | None = None) -> Offer:
    """The general form: any flow can name its own next step and wait for a yes.

    This is what makes the mechanism worth having beyond CI. Implementing a
    ticket, chasing a review, updating a status — none of them fit a fixed enum,
    and all of them want the same shape: here is where I got to, here is what I
    would do next, say the word.
    """
    return offer(kind, subject, context, question, payload=payload, action=action)


def staged_write(op_name: str, args: dict, subject: str, context: str,
                 question: str, kind: str = "write") -> Offer:
    """An outward write, held until he says yes, then run verbatim.

    The args ARE the approval. A brain re-reading its own instruction could
    reasonably post a differently-worded comment than the one he read and agreed
    to; running the recorded call cannot.
    """
    return offer(kind, subject, context, question,
                 payload={"op": op_name}, op={"name": op_name, "args": args})


# --- the CI recipe ----------------------------------------------------------
#
# Kept as named builders rather than inlined at the call site: the wording of a
# question he answers with one word is worth having in one place, and worth
# having a test on.

def for_ci_failure(repo: str, workflow: str, branch: str, url: str,
                   title: str = "") -> Offer:
    """A red pipeline: say precisely what broke, then offer to investigate."""
    detail = f"{workflow} on {branch}"
    if title:
        detail += f" — {title[:70]}"
    return offer(
        "analyse",
        f"🔴 CI failed: {repo.split('/')[-1]}",
        f"{detail}\n{url}",
        "Want me to analyse the failure?",
        {"repo": repo, "workflow": workflow, "branch": branch, "url": url})


def after_analysis(o: Offer, summary: str) -> Offer:
    """Analysis done — offer the fix, carrying the original context forward."""
    return offer(
        "raise_pr",
        f"🔍 Analysed: {o.subject.removeprefix('🔴 CI failed: ')}",
        summary,
        "Shall I fix it and raise the PR?",
        o.payload)


def after_pr(o: Offer, pr_url: str) -> Offer:
    """PR is up — the last step needs a destination, so this one asks for it
    rather than assuming where a build should go."""
    return offer(
        "share_build",
        "✅ PR raised",
        f"{pr_url}\nCI will run on it; I'll report back.",
        "Where should I share the build for approval? (name a person or channel)",
        {**o.payload, "pr_url": pr_url})
