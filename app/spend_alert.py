"""Tell him once when a code task costs far more than code tasks usually do.

17 Nov: the two costliest code tasks of the month (#601, #567) ran leg after leg
and nothing said so until the usage limit did. Measured spend only (the worker
ledger, app/worker_usage.py); zero model tokens. Code tasks only: analysis runs
vary too much — over 3x their median is 28% of them, which would be noise.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import statistics
import time

from . import store

#: Over this many times the usual code task, he is told — once per task.
FACTOR = 3.0
#: The usual is the median of real code tasks over this window…
WINDOW = 30 * 86400
#: …and only once there are enough of them to call it usual.
MIN_SAMPLES = 5
_TOLD = "spend_told:"


def _totals(since: float) -> dict[int, list[int]]:
    """task id -> [new tokens, legs] over the window."""
    per: dict[int, list[int]] = {}
    for raw in store.kv_like("worker_usage:%").values():
        with contextlib.suppress(ValueError, TypeError):
            row = json.loads(raw)
            if float(row.get("started_at") or 0) < since:
                continue
            t = per.setdefault(int(row["task_id"]), [0, 0])
            t[0] += int((row.get("usage") or {}).get("new_tokens") or 0)
            t[1] += 1
    return per


def _real_code(tid: int) -> bool:
    from . import rehearsal
    t = store.get_task(tid) or {}
    return t.get("kind") == "code" and not rehearsal.is_rehearsal(tid)


def over(task_id: int, now: float | None = None) -> str:
    """The line for him when this task is over FACTOR x usual, else ""."""
    now = time.time() if now is None else now
    if store.kv_get(_TOLD + str(task_id)) or not _real_code(task_id):
        return ""
    per = _totals(now - WINDOW)
    mine = per.get(task_id)
    others = [n for tid, (n, _) in per.items() if tid != task_id and n and _real_code(tid)]
    if not mine or len(others) < MIN_SAMPLES:
        return ""
    usual = statistics.median(others)
    if mine[0] <= FACTOR * usual:
        return ""
    t = store.get_task(task_id) or {}
    return (f"💸 Task #{task_id} ({(t.get('title') or '')[:60]}) has used "
            f"{mine[0] / 1000:,.0f}k new tokens in {mine[1]} leg(s) — "
            f"{mine[0] / usual:.0f}× a usual code task ({usual / 1000:,.0f}k). "
            f"Say \"stop #{task_id}\" if it is going round in circles.")


def after_leg(task_id: int) -> None:
    """Called when a worker leg ends; tells him at most once per task."""
    line = over(task_id)
    if not line:
        return
    store.kv_set(_TOLD + str(task_id), str(time.time()))
    store.record_outcome("spend", "costly task", subject=str(task_id), detail=line[:200])
    from . import notify
    with contextlib.suppress(RuntimeError):
        asyncio.get_running_loop().create_task(
            notify.notify(line, "task", urgency="direct", considered=True))
