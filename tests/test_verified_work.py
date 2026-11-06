"""Cost per VERIFIED code task, not per chat turn (roadmap section 9).

Synthetic tasks: a merged one with two stops and measured usage, a merged one on
a brain that reports nothing, and one that never merged.
"""
from __future__ import annotations

import json
import sqlite3
import time

from app import scorecard, store


def _task(title, *, merged_after=None, gates=0, usage=None):
    t = store.create_task(title, "code", title, "ws")
    now = time.time()
    with sqlite3.connect(store.DB_PATH) as c:
        c.execute("UPDATE tasks SET created_at=? WHERE id=?", (now - 7200, t["id"]))
        for _ in range(gates):
            c.execute("INSERT INTO task_events(task_id, kind, detail, created_at) VALUES (?,?,?,?)",
                      (t["id"], "gate", "approved: PLAN APPROVED", now - 3600))
        if merged_after is not None:
            c.execute("INSERT INTO task_events(task_id, kind, detail, created_at) VALUES (?,?,?,?)",
                      (t["id"], "status", "shipped → merged", now - 7200 + merged_after))
    if usage is not None:
        store.kv_set(f"worker_usage:{t['id']}:r1", json.dumps({
            "run_id": "r1", "task_id": t["id"], "started_at": now, "status": "returned",
            "usage": usage, "usage_state": "provider_reported" if usage else "unavailable"}))
    return t["id"]


def test_cost_and_stops_are_per_merged_task_and_unmeasured_is_never_zero():
    _task("measured", merged_after=3600, gates=2,
          usage={"calls": 3, "new_tokens": 1000, "cache_read": 4000, "out_tokens": 500})
    _task("copilot", merged_after=1800, gates=1, usage=None)
    _task("never merged", gates=5)
    now = time.time()
    vw = scorecard.verified_work(now - 86400, now + 1)
    assert vw["verified"] == 2, "only merged work counts as verified"
    assert vw["stops_avg"] == 1.5
    assert vw["tokens_per_verified"] == 5500 and vw["measured"] == 1 and vw["unmeasured"] == 1
    assert 1700 < vw["latency_p50_s"] < 3700


def test_the_row_says_unmeasured_instead_of_inventing_a_number():
    _task("copilot only", merged_after=600, gates=1, usage=None)
    row = next(r for r in scorecard.compute(days=1)["rows"] if r["key"] == "verified_work")
    assert "1 unmeasured" in row["note"] and "never counted as zero" in row["note"]
    assert "tokens per merged task —" in row["note"]


def test_no_merged_work_is_not_good():
    row = next(r for r in scorecard.compute(days=1)["rows"] if r["key"] == "verified_work")
    assert row["state"] == "na"
