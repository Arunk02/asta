"""ask_user: one question to Arun's phone, without stopping the pipeline.

Asta's gates are all-or-nothing — the whole pipeline halts, and resuming a code
task after a re-plan cost a measured +26 calls and +500k tokens. That is the
right price for "approve this plan". It is far too much for "which of these two
repos did you mean?".

This is the cheap path: the caller asks one question, Arun gets it on his phone,
and the caller resumes with his answer in place. Nothing restarts.

Answers arrive from whichever channel he replies on — the web UI, WhatsApp,
Telegram — and the first one wins. Questions are persisted so the UI can show
them, and expired at startup: a waiter that died with the process can never be
answered, and a stale question would otherwise swallow his next message.
"""

from __future__ import annotations

import asyncio
import time

from . import store

#: How long a caller waits before giving up. Long enough for Arun to notice a
#: phone push, short enough that a forgotten question cannot hold a worker open
#: for the rest of the day.
DEFAULT_TIMEOUT = 15 * 60

#: A question older than this is no longer "the thing he is replying to", so a
#: bare message stops being read as its answer.
AUTO_ANSWER_WINDOW = 30 * 60

#: How long an answer he has already given stands for. Ask the same question
#: again inside this and his earlier answer is reused instead of buzzing him.
#:
#: This is the cheap half of a real complaint: a question was asked, he answered
#: it, the work was done and the PR raised — and hours later the same question
#: arrived again. The main cause was the ledger chasing Asta's own outbound
#: question (see attention.SELF_SOURCE); this is the guard that holds even when
#: the caller genuinely asks twice, which a retried or resumed task will do.
REPEAT_WINDOW = 6 * 3600


def _same(a: str, b: str) -> bool:
    """Whitespace and case differ between two renderings of one question; the
    words do not. Deliberately NOT fuzzy — reusing his answer to a question he
    was never asked would be worse than asking once too often."""
    return " ".join((a or "").split()).casefold() == " ".join((b or "").split()).casefold()


def recent_answer(question: str, now: float | None = None) -> str:
    """His answer to this exact question if he gave one inside REPEAT_WINDOW."""
    since = (time.time() if now is None else now) - REPEAT_WINDOW
    for row in store.answered_questions(since):
        if _same(row.get("text", ""), question):
            return row.get("answer") or ""
    return ""


NO_ANSWER = "NO ANSWER — Arun did not reply in time. Proceed on your best judgement " \
            "and say clearly what you assumed."

_waiters: dict[int, asyncio.Future] = {}


def open_questions() -> list[dict]:
    return store.open_questions()


def expire_stale() -> int:
    """Startup hook — see the module docstring."""
    _waiters.clear()
    return store.expire_open_questions()


async def ask(question: str, source: str = "", timeout: float = DEFAULT_TIMEOUT) -> str:
    """Put one question to Arun and wait for his answer.

    Returns his answer, or NO_ANSWER on timeout — never raises, because a caller
    that asked a clarifying question should degrade to its best guess rather than
    fail the work it was doing.
    """
    text = (question or "").strip()
    if not text:
        return NO_ANSWER
    # He already answered this. Asking again is not diligence — it reads as not
    # having listened, and it is the thing he called the worst of the lot.
    prior = recent_answer(text)
    if prior:
        store.record_outcome("ask", "reused", detail=text[:200])
        return prior
    # The same question already in flight waits on the answer he is ALREADY being
    # asked for, rather than putting the same sentence on his phone twice.
    for row in store.open_questions():
        fut = _waiters.get(row["id"])
        if fut is not None and not fut.done() and _same(row.get("text", ""), text):
            try:
                return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
            except asyncio.TimeoutError:
                return NO_ANSWER
    from . import notify
    q = store.create_question(text, source)
    qid = q["id"]
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _waiters[qid] = fut
    where = f" ({source})" if source else ""
    await notify.notify(f"❓ Question{where}:\n\n{text}\n\nJust reply — or "
                        f"'answer {qid} <your reply>' if several are open.", "action",
                        urgency="direct")
    try:
        reply = await asyncio.wait_for(fut, timeout=timeout)
        store.record_outcome("ask", "answered", subject=str(qid), detail=text[:200])
        return reply
    except asyncio.TimeoutError:
        store.close_question(qid, "", status="timeout")
        store.record_outcome("ask", "timeout", subject=str(qid), detail=text[:200])
        return NO_ANSWER
    finally:
        _waiters.pop(qid, None)


def answer(qid: int, text: str) -> bool:
    """Deliver an answer. False when there is no open question with that id."""
    q = store.get_question(qid)
    if not q or q["status"] != "open":
        return False
    store.close_question(qid, text)
    fut = _waiters.get(qid)
    if fut and not fut.done():
        fut.set_result(text)
    return True


def pending_for_reply() -> dict | None:
    """The one open question a bare message should be read as answering.

    Only when exactly one is open and it is recent — with two open, guessing
    would put the answer on the wrong question, and on a phone channel that is
    invisible until it has already gone wrong.
    """
    rows = [q for q in store.open_questions()
            if time.time() - q["created_at"] <= AUTO_ANSWER_WINDOW]
    return rows[0] if len(rows) == 1 else None
