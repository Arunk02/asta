"""25 Nov, live (rehearsal #751): the plan PDF takes seconds to draw, and an
approve that landed in them reached a workflow not yet waiting at its gate —
lost, and the plan announced twice. The task is marked waiting for him only
once the plan has been sent."""
from __future__ import annotations

import asyncio

from app import notify, store, tasks

PLAN = "STRUCTURE\n  calc.py  subtract(a, b)\n\nRISK: none\n\nPLAN READY"


def test_the_task_waits_for_him_only_after_the_plan_is_sent(monkeypatch):
    t = store.create_task("Add subtract to calc.py", "code", "p", None)
    store.update_task(t["id"], status="running")
    seen = {}

    async def slow_send(task_id, task, plan):
        # While the PDF is being drawn, an answer cannot be taken yet.
        seen["during"] = store.get_task(task_id)["status"]
        await asyncio.sleep(0)
        return "📎 The whole plan is attached as plan.pdf (3 lines).\n"

    async def quiet(*a, **k):
        return None
    monkeypatch.setattr(tasks, "_send_full_plan", slow_send)
    monkeypatch.setattr(notify, "notify", quiet)
    asyncio.run(tasks.announce_plan(t["id"], store.get_task(t["id"]), PLAN))
    assert seen["during"] == "running"
    after = store.get_task(t["id"])
    assert after["status"] == "awaiting_approval" and "subtract" in after["result"]
    assert store.kv_get(f"task_gate:{t['id']}") == "plan"
