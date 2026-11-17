"""A live rehearsal is Asta being tested, not Asta working.

Tasks in a workspace whose name starts with "e2e" (scripts/live_check.py makes
one) run for real — real brains, real checkouts — but leave nothing behind
that would change how Asta works for him: no learned skills, no token-audit
trend, no phone pushes (the UI bell still records each one).

17 Nov: scratch "add subtract to calc.py" runs had promoted four arithmetic
skills into his live skill index and pushed plans for them to his phone.
"""
from __future__ import annotations

from contextvars import ContextVar

from . import store

PREFIX = "e2e"

#: Set when a rehearsal task's run starts; asyncio copies it into every task
#: that run creates, so a push made anywhere below it is seen as a rehearsal's.
_ACTIVE: ContextVar[bool] = ContextVar("asta_rehearsal", default=False)


def workspace_is(name: str | None) -> bool:
    return (name or "").strip().lower().startswith(PREFIX)


def is_rehearsal(task_id: int | str | None) -> bool:
    try:
        t = store.get_task(int(task_id)) if task_id else None
    except (TypeError, ValueError):
        return False
    return bool(t) and workspace_is(t.get("workspace"))


def enter(task_id: int) -> bool:
    """Called where a task's run starts. True when it is a rehearsal."""
    on = is_rehearsal(task_id)
    _ACTIVE.set(on)
    return on


def active() -> bool:
    return _ACTIVE.get()
