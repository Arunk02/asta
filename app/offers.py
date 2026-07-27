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
        """
        body = f"{self.subject}\n{self.context}".strip()
        return f"{body}\n\n▶ {self.prompt}\n   reply “yes” to go ahead, “no” to drop it."


def offer(kind: str, subject: str, context: str, prompt: str,
          payload: dict | None = None, action: str = "",
          op: dict | None = None) -> Offer:
    """Record a proposal and return it. Replaces any earlier pending one.

    One at a time on purpose: two open questions and a bare "yes" is ambiguous,
    and guessing which one he meant is exactly the kind of confident wrong move
    that makes an assistant untrustworthy.
    """
    o = Offer(id=uuid.uuid4().hex[:12], kind=kind, subject=subject, context=context,
              prompt=prompt, created=time.time(), payload=payload or {},
              action=action, op=op or {})
    store.kv_set(KEY, json.dumps(asdict(o)))
    return o


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
        clear()
        return None
    return o


def accept() -> Offer | None:
    """Consume the open offer — returns it exactly once, so a double "yes"
    cannot run the same work twice."""
    o = pending()
    if o:
        clear()
    return o


def decline() -> Offer | None:
    o = pending()
    clear()
    return o


def clear() -> None:
    store.kv_set(KEY, "")


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
