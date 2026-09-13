"""Driving the graph: start a job, answer a gate, carry on after a stop.

Each call opens the checkpointer, runs the thread until it finishes or reaches a
gate, and closes it again. Nothing is held in memory between calls — the thread
IS its checkpoint — which is exactly what makes a restart harmless: the process
that resumes it does not need to be the one that started it.

The checkpoints live beside Asta's own database (`graphs.sqlite` next to
`asta.db`), so the isolation the test suite and the WorkWorld sandbox already
give the database covers the graph too, with nothing extra to remember.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path

from app import store

_TRUEY = ("1", "true", "yes", "on")


def enabled() -> bool:
    """New code tasks run on the graph when ASTA_GRAPH is on. Off by default."""
    return os.environ.get("ASTA_GRAPH", "").strip().lower() in _TRUEY


def manages(task_id: int) -> bool:
    """Was this task started on the graph? It finishes where it started."""
    return (store.kv_get(f"task_engine:{task_id}") or "") == "graph"


def db_path() -> Path:
    return Path(store.DB_PATH).with_name("graphs.sqlite")


def config(task_id: int) -> dict:
    return {"configurable": {"thread_id": f"task-{task_id}"}}


def _pending_key(task_id: int) -> str:
    return f"graph_answer:{task_id}"


async def _drive(task_id: int, inp) -> None:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from app import notify, tasks
    from .code_graph import Stopped, build
    try:
        async with AsyncSqliteSaver.from_conn_string(str(db_path())) as saver:
            app = build().compile(checkpointer=saver)
            if isinstance(inp, _Pending):
                inp = await _resolve_pending(app, task_id)
            await app.ainvoke(inp, config(task_id))
            # The run stopped cleanly — at a gate or at the end — so whatever
            # answer it was handed has been applied, and a stop at a gate is
            # shown as one. Without the second half, a resume that found no
            # answer waiting would leave the row saying "running" at a gate.
            store.kv_del(_pending_key(task_id))
            gate = _gate_of(await app.aget_state(config(task_id)))
            t = store.get_task(task_id) or {}
            if gate and t.get("status") == "running":
                store.update_task(task_id, status="awaiting_approval")
    except asyncio.CancelledError:
        raise                                   # cancel() owns the status
    except Stopped:
        return                                  # rejected between steps: quiet
    except tasks._LimitPaused as exc:
        store.kv_del(_pending_key(task_id))
        t = store.get_task(task_id) or {}
        if t.get("status") not in tasks.FINAL:
            await tasks._pause_task(task_id, t, exc)
    except Exception as exc:                    # noqa: BLE001
        t = store.get_task(task_id) or {}
        if t.get("status") in tasks.FINAL:
            return
        import time
        store.update_task(task_id, status="failed", error=str(exc)[:500],
                          finished_at=time.time())
        await notify.notify(f"❌ Task #{task_id} failed — {t.get('title', '')}: "
                            f"{str(exc)[:200]}", "task")


async def _resolve_pending(app, task_id: int):
    from langgraph.types import Command
    raw = store.kv_get(_pending_key(task_id))
    if raw and _gate_of(await app.aget_state(config(task_id))):
        with contextlib.suppress(ValueError):
            return Command(resume=json.loads(raw))
    return None


def _gate_of(snap) -> str:
    for t in getattr(snap, "tasks", None) or ():
        for it in getattr(t, "interrupts", ()) or ():
            value = getattr(it, "value", None) or {}
            if isinstance(value, dict) and value.get("gate"):
                return value["gate"]
    return ""


def _spawn(task_id: int, inp) -> asyncio.Task:
    from app import tasks
    job = asyncio.create_task(_drive(task_id, inp))
    tasks._running[task_id] = job
    job.add_done_callback(lambda _j, tid=task_id: tasks._running.pop(tid, None))
    return job


def start(task_id: int) -> asyncio.Task:
    """Begin a code task on the graph."""
    store.kv_set(f"task_engine:{task_id}", "graph")
    return _spawn(task_id, {"task_id": task_id})


def answer(task_id: int, value: dict) -> asyncio.Task:
    """Resume the thread at the gate it is waiting on, with what Arun said.

    Written down before it is applied. His "approve" arriving a moment before a
    restart is otherwise lost twice over: the process that would have applied it
    is gone, and the thread it was for is still waiting at the gate for it.
    """
    from langgraph.types import Command
    store.kv_set(_pending_key(task_id), json.dumps(value))
    return _spawn(task_id, Command(resume=value))


def revisit(task_id: int, text: str) -> asyncio.Task:
    """Follow-up on finished work: the same thread runs again, straight to building,
    keeping what it knew — the plan's repos, the notes, the session."""
    return _spawn(task_id, {"task_id": task_id,
                            "answer": {"approved": True, "text": text}})


def carry_on(task_id: int) -> asyncio.Task:
    """Pick up from the last checkpoint — after a usage limit or a restart.

    If an answer was given and never applied, the thread is still at that gate:
    it is applied now. Otherwise the thread continues from its last step, and a
    leg that was cut short knows to continue rather than start again.
    """
    return _spawn(task_id, _Pending(task_id))


class _Pending:
    """Marker resolved inside the job: the unapplied answer, or plain continue."""

    def __init__(self, task_id: int):
        self.task_id = task_id


async def waiting_at(task_id: int) -> str:
    """Which gate the thread is stopped at ('plan', 'context', 'verify'), or ''."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from .code_graph import build
    async with AsyncSqliteSaver.from_conn_string(str(db_path())) as saver:
        app = build().compile(checkpointer=saver)
        return _gate_of(await app.aget_state(config(task_id)))
