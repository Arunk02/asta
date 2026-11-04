"""Measured usage of every one-shot model call that is not a task worker leg.

worker_usage covers the legs of a delegated task. Everything else that calls a
CLI headlessly (memory consolidation, the reader, reviews, context refresh, the
call brain, understanding a corpus) went unrecorded, so "where did the quota go"
could only be answered for tasks. One row per call here, with the same rule as
worker_usage: usage comes from the executor's own session log or it is marked
unavailable. Nothing is estimated, priced or counted as zero.
"""
from __future__ import annotations

import contextlib
import sys
import time
import uuid

from . import store, worker_usage

_SELF = ("app.call_ledger", "app.claude_cli", "app.copilot_cli")


def caller() -> str:
    """The first app frame above the CLI wrapper, as 'module.function'.

    A call started through create_task or wait_for has no app frame left above
    it; that call is 'unattributed', never guessed.
    """
    frame = sys._getframe(1)
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if module.startswith("app.") and module not in _SELF:
            return f"{module[4:]}.{frame.f_code.co_name}"
        frame = frame.f_back
    return "unattributed"


def inside_worker() -> bool:
    """A task leg is already measured by worker_usage; recording it again doubles it."""
    return worker_usage.ACTIVE.get()


@contextlib.contextmanager
def call(executor: str, cwd: str, session_id: str, *, purpose: str, model: str = ""):
    """One real CLI invocation outside a task. Accounting never fails the call."""
    run_id = uuid.uuid4().hex
    before = worker_usage._snapshot(executor, cwd, session_id)
    with contextlib.suppress(Exception):
        store.add_call_usage(run_id, purpose=purpose, executor=executor,
                             model=model or "default", session_id=session_id)
    status, error_type = "returned", ""
    try:
        yield run_id
    except BaseException as exc:
        status = "cancelled" if type(exc).__name__ == "CancelledError" else "failed"
        error_type = type(exc).__name__
        raise
    finally:
        after = worker_usage._snapshot(executor, cwd, session_id)
        usage, state = worker_usage._delta(before, after)
        with contextlib.suppress(Exception):
            store.finish_call_usage(run_id, status=status, error_type=error_type,
                                    usage_state=state, usage=usage)


def report(hours: float = 24) -> dict:
    """Known subtotal per purpose, with coverage; an unmeasured call is not free."""
    rows = store.call_usage_since(time.time() - hours * 3600)
    fields = worker_usage._FIELDS
    purposes: dict[str, dict] = {}
    for row in rows:
        p = purposes.setdefault(row["purpose"], {"calls": 0, "measured": 0,
                                                 "usage": dict.fromkeys(fields, 0)})
        p["calls"] += 1
        if row["usage_state"] == "provider_reported":
            p["measured"] += 1
            for field in fields:
                p["usage"][field] += row[field] or 0
    measured = sum(p["measured"] for p in purposes.values())
    return {"hours": hours, "calls": len(rows), "measured_calls": measured,
            "purposes": dict(sorted(purposes.items(),
                                    key=lambda kv: -kv[1]["usage"]["new_tokens"] - kv[1]["usage"]["out_tokens"])),
            "monetary_cost": None,
            "note": "One-shot calls outside task workers. New input includes cache creation. "
                    "Unmeasured calls (Copilot, missing logs) are counted, never as zero."}


def summary(hours: float = 24) -> str:
    data = report(hours)
    if not data["calls"]:
        return f"No one-shot model calls outside tasks in the last {hours:g}h."
    lines = [f"One-shot model calls outside tasks, last {hours:g}h: {data['measured_calls']}/"
             f"{data['calls']} measured. Monetary cost is unavailable."]
    for purpose, p in data["purposes"].items():
        u = p["usage"]
        known = (f"{u['new_tokens']:,} new input, {u['cache_read']:,} cached, "
                 f"{u['out_tokens']:,} output" if p["measured"] else "usage unavailable, not zero")
        lines.append(f"  {purpose}: {p['calls']} calls ({p['measured']} measured): {known}")
    return "\n".join(lines)
