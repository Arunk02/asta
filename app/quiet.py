"""Swallowing an exception without losing the fact that it happened.

Ninety-two places in this codebase deliberately ignore an error, and nearly all of
them are right to: a caption read must not end a call, a failed learning
extraction must not fail the work it learned from, a dead WhatsApp bridge must not
stop the catch-up scan. The intent is sound. What was missing is any trace.

So a selector that quietly stopped matching, a store write that quietly failed, a
notification that quietly reached nobody — each of those degrades Asta a little,
and none of them produced a single record anywhere. The system got worse in ways
nobody could see, which is the same failure mode as every finding in the August
review: not a crash, a silence.

`swallow` keeps the behaviour and adds the record. Nothing propagates, nothing
blocks, and the count is queryable — so "Teams reads have been failing for three
days" becomes a question with an answer instead of a thing Arun eventually
notices.
"""

from __future__ import annotations

import contextlib
import time
from collections import Counter

from . import store

#: In memory, because this is a diagnostic rather than a fact about his work. A
#: restart clearing it is correct: what matters is whether things are failing NOW.
_seen: Counter = Counter()
_last: dict[str, dict] = {}

#: Above this many of the same swallowed error, it stops being noise and starts
#: being a fault worth his attention.
LOUD_AFTER = 20


@contextlib.contextmanager
def swallow(where: str, *, expect: type[BaseException] | tuple = Exception):
    """Ignore a failure here, but remember that it happened.

    `where` is a stable name for the site — "teams.read_activity", not a message —
    so repeats aggregate instead of each looking new.
    """
    try:
        yield
    except expect as exc:                              # noqa: BLE001
        note(where, exc)


def note(where: str, exc: BaseException) -> None:
    """Record one swallowed failure."""
    _seen[where] += 1
    _last[where] = {"at": time.time(), "error": f"{type(exc).__name__}: {exc}"[:300],
                    "count": _seen[where]}
    if _seen[where] == LOUD_AFTER:
        # Recorded once, at the threshold. Not a notification: this is for the
        # health endpoint and the daily brief to read, and a push per swallowed
        # exception would be exactly the noise the attention ledger exists to end.
        with contextlib.suppress(Exception):
            store.record_outcome("swallowed", "recurring", subject=where,
                                 detail=_last[where]["error"])


def counts() -> dict[str, dict]:
    """Every site that has swallowed something, worst first."""
    return {k: _last[k] for k, _ in _seen.most_common()}


def loud() -> list[dict]:
    """The ones that have failed often enough to mean something."""
    return [{"where": k, **v} for k, v in counts().items()
            if v["count"] >= LOUD_AFTER]


def summary() -> str:
    """One line for the health endpoint."""
    bad = loud()
    if not bad:
        total = sum(_seen.values())
        return f"{total} handled errors, none recurring" if total else "no handled errors"
    worst = ", ".join(f"{b['where']} ×{b['count']}" for b in bad[:3])
    return f"{len(bad)} site(s) failing repeatedly: {worst}"


def reset() -> None:
    _seen.clear()
    _last.clear()
