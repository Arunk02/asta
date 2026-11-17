"""The week's spend as one table — by kind, costliest runs, unused answers."""
from __future__ import annotations

import json
import time

from app import store, usage_week


def _leg(tid, run, new, cached=0, out=0, at=None):
    store.kv_set(f"worker_usage:{tid}:{run}", json.dumps({
        "task_id": tid, "run_id": run, "started_at": at or time.time() - 3600,
        "usage": {"new_tokens": new, "cache_read": cached, "out_tokens": out, "calls": 3}}))


def test_spend_by_kind_and_the_costliest_runs():
    a = store.create_task("check booking", "analysis", "p", "booking")["id"]
    c = store.create_task("add field", "code", "p", "booking")["id"]
    test = store.create_task("calc: add subtract", "code", "p", "e2e")["id"]
    _leg(a, "r1", 30_000, 200_000, 2_000)
    _leg(c, "r1", 400_000, 3_000_000, 20_000)
    _leg(c, "r2", 900_000, 6_000_000, 40_000)
    _leg(test, "r1", 7_700_000)                                  # a rehearsal: not counted
    _leg(a, "old", 8_800_000, at=time.time() - 9 * 86400)        # last week: not counted
    store.record_outcome("answer", "presented", subject=str(a))
    store.record_outcome("answer", "he answered", subject=str(a))
    out = usage_week.table()
    assert "code" in out and "1.3M" in out and "analysis" in out
    assert f"#{c} code 1.3M in 2 leg(s)" in out
    assert "7.7M" not in out and "8.8M" not in out
    assert "1 shown · 1 you answered first" in out
