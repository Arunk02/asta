"""A code task far over the usual spend is said once, with its leg count."""
from __future__ import annotations

import asyncio
import json
import time

from app import notify, spend_alert, store


def _leg(tid, run, new):
    store.kv_set(f"worker_usage:{tid}:{run}", json.dumps({
        "task_id": tid, "run_id": run, "started_at": time.time() - 600,
        "usage": {"new_tokens": new, "cache_read": 0, "out_tokens": 0, "calls": 1}}))


def _usual(n=5, each=60_000):
    for i in range(n):
        tid = store.create_task(f"usual {i}", "code", "p", "booking")["id"]
        _leg(tid, "r", each)


def test_a_task_over_three_times_usual_is_told_once(monkeypatch):
    told = []

    async def push(text, *a, **k):
        told.append(text)
    monkeypatch.setattr(notify, "notify", push)
    _usual()
    big = store.create_task("add field across repos", "code", "p", "booking")["id"]
    for i in range(4):
        _leg(big, f"r{i}", 60_000)

    async def run():
        spend_alert.after_leg(big)
        spend_alert.after_leg(big)
        await asyncio.sleep(0)
    asyncio.run(run())
    assert len(told) == 1
    assert f"#{big}" in told[0] and "240k" in told[0] and "4 leg(s)" in told[0]


def test_no_alert_within_usual_or_without_history():
    big = store.create_task("x", "code", "p", "booking")["id"]
    _leg(big, "r", 900_000)
    assert spend_alert.over(big) == ""          # no history to call usual yet
    _usual()
    ok = store.create_task("y", "code", "p", "booking")["id"]
    _leg(ok, "r", 150_000)
    assert spend_alert.over(ok) == ""           # 2.5x — within bounds


def test_analysis_and_rehearsals_are_not_alerted():
    _usual()
    a = store.create_task("check", "analysis", "p", "booking")["id"]
    _leg(a, "r", 900_000)
    assert spend_alert.over(a) == ""
