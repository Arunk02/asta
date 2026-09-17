"""Driving any checkpointed graph — the parts `runner` proved on code tasks.

`runner` is the code task's own driver: its threads are `task-{id}`, its state is
a job, its gates are a plan review. The three graphs added in P7 — investigate,
follow through, draft and send — want none of that and all of this:

  * the thread IS the checkpoint, so the process that resumes a job need not be
    the one that started it (a restart mid-investigation is not a lost one);
  * an answer is WRITTEN DOWN before it is applied, because "approve" arriving a
    moment before a restart was otherwise lost twice over — the process that
    would have applied it is gone, and the thread is still waiting for it. That
    bug cost a real approval on 13 September;
  * a gate node does NOTHING but `interrupt()`, because a resumed node re-runs
    from its start and anything else in it would happen twice.

Same checkpoint file as the code graph, so the isolation the suite and the
sandbox already give the database covers these too.
"""

from __future__ import annotations

import contextlib
import json
import os

from app import store

from .runner import db_path

_TRUEY = ("1", "true", "yes", "on")


def enabled() -> bool:
    """The three P7 graphs. Off by default, like every engine change before it."""
    return os.environ.get("ASTA_GRAPHS", "").strip().lower() in _TRUEY


def thread(kind: str, key: str | int) -> dict:
    return {"configurable": {"thread_id": f"{kind}-{key}"}}


def _pending_key(kind: str, key: str | int) -> str:
    return f"graph_answer:{kind}:{key}"


def waiting_key(kind: str, key: str | int) -> str:
    """Which gate this thread is parked at, readable without opening the graph —
    the front desk answers "what is it waiting for?" from state, never a brain."""
    return f"graph_gate:{kind}:{key}"


async def drive(kind: str, key: str | int, builder, inp) -> dict:
    """Run a thread to its next gate or to the end. Returns the state it reached."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.types import Command
    async with AsyncSqliteSaver.from_conn_string(str(db_path())) as saver:
        app = builder().compile(checkpointer=saver)
        cfg = thread(kind, key)
        if inp is PENDING:
            inp = None
            raw = store.kv_get(_pending_key(kind, key))
            if raw and gate_of(await app.aget_state(cfg)):
                with contextlib.suppress(ValueError):
                    inp = Command(resume=json.loads(raw))
        out = await app.ainvoke(inp, cfg)
        snap = await app.aget_state(cfg)
        at = gate_of(snap)
        store.kv_set(waiting_key(kind, key), at)
        if not at:
            store.kv_set(_pending_key(kind, key), "")
        return dict(out or {}) | {"waiting_at": at}


class _Pending:
    """Marker: resume with whatever answer was written down, if any."""


PENDING = _Pending()


def gate_of(snap) -> str:
    for t in getattr(snap, "tasks", None) or ():
        for it in getattr(t, "interrupts", ()) or ():
            value = getattr(it, "value", None) or {}
            if isinstance(value, dict) and value.get("gate"):
                return value["gate"]
    return ""


def record_answer(kind: str, key: str | int, value: dict) -> None:
    """Write his answer down WITHOUT driving the graph.

    The front desk is synchronous and runs inside the server's event loop, so it
    cannot await a thread — and it must not have to. Recording is the half that
    matters: the answer is safe the moment he says it, and the next tick applies
    it. This is the 13 September lesson stated as an API rather than a comment.
    """
    store.kv_set(_pending_key(kind, key), json.dumps(value))


def answer_waiting(kind: str, key: str | int) -> dict | None:
    raw = store.kv_get(_pending_key(kind, key))
    if not raw:
        return None
    with contextlib.suppress(ValueError):
        return json.loads(raw)
    return None


async def answer(kind: str, key: str | int, builder, value: dict) -> dict:
    """His answer at a gate: recorded first, then applied.

    Awaitable rather than a fire-and-forget task. A caller that wants it in the
    background wraps it — but the default has to be the one that can be waited
    on, or a test (and a caller) gets a Task nobody awaits and an act that never
    happens, which is precisely the failure this graph exists to prevent."""
    from langgraph.types import Command
    store.kv_set(_pending_key(kind, key), json.dumps(value))
    return await drive(kind, key, builder, Command(resume=value))


async def resume(kind: str, key: str | int, builder) -> dict:
    """Pick a thread back up after a restart, applying any answer it missed."""
    return await drive(kind, key, builder, PENDING)
