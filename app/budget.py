"""How many times Asta may interrupt him in a day.

The plan's number is 20, against about 70 measured. A budget is blunt on its
own, so it is spent in the right order: anything that breaks or blocks is never
counted against it, and everything ordinary that arrives once the day's budget
is gone goes to the digest instead of his pocket.

Counted at the delivery door — one buzz, one unit — so a batch of four items in
one message costs one, which is exactly the behaviour worth encouraging.
"""

from __future__ import annotations

import os
import time

from . import store


def cap() -> int:
    """Pushes a day. 0 disables the budget entirely."""
    try:
        return max(0, int(os.environ.get("ASTA_PUSH_BUDGET", "20")))
    except ValueError:
        return 20


def _key(now: float | None = None) -> str:
    return "pushes:" + time.strftime("%Y-%m-%d", time.localtime(now or time.time()))


def spent(now: float | None = None) -> int:
    try:
        return int(store.kv_get(_key(now)) or 0)
    except ValueError:
        return 0


def note_push(now: float | None = None) -> int:
    n = spent(now) + 1
    store.kv_set(_key(now), str(n))
    return n


def left(now: float | None = None) -> int:
    return max(0, cap() - spent(now)) if cap() else 999


def allows(priority: int | None, urgency: str = "direct", now: float | None = None) -> bool:
    """May this interrupt him now? Breakage and things he is blocking on always may.

    Deliberately generous in one direction: the budget delays what can wait, and
    never swallows what cannot. A day that legitimately needs thirty interrupts
    still gets thirty of the right ones — the twenty-first ordinary item is what
    moves to the digest.
    """
    from . import attention
    if not cap():
        return True
    if priority is not None and int(priority) <= attention.P_NOW:
        return True
    return spent(now) < cap()
