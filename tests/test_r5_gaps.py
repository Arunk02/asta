"""Two gaps the 25–26 Nov live runs left:

1. "In calc.py add subtract(a, b) and a test" was not routed as code work — the
   verb was not first — and the chat brain answered it in chat.
2. An approved amendment showed "done" for a moment before its build started:
   a message then would be filed as a follow-up on finished work, and the build
   opened yet another fresh session instead of keeping the amendment's."""
from __future__ import annotations

import asyncio
import json

import pytest

from app import store, tasks, work_intent

REPOS = ("calc", "telikos-booking-service")


@pytest.mark.parametrize("text", [
    "In calc.py add subtract(a, b) and a test for it in test_calc.py",
    "In the booking service, add a null check in the mapper",
    "In calc add a power function with a test",
    "In telikos-booking-service fix the retry timeout",
    "For AP repo, handle the missing vessel ETA field",
])
def test_a_code_location_before_the_verb_is_still_work(text):
    assert work_intent.is_work_assignment(text, REPOS)


@pytest.mark.parametrize("text", [
    "In the meeting, add me to the invite",
    "In my opinion fix the code later",
    "In general, add more tests",
    "In calc.py what does add do?",
    "In calc.py, how do I add a test?",
    "On Monday update the schema",
])
def test_a_place_that_is_not_code_or_a_question_is_not(text):
    assert not work_intent.is_work_assignment(text, REPOS)


# --- 2: the approved amendment ------------------------------------------------------

def _awaiting_amendment():
    t = store.create_task("Add subtract to calc.py", "code", "p", None)
    tid = t["id"]
    store.kv_set(f"task_plan:{tid}", "STRUCTURE\n  calc.py  subtract")
    store.kv_set(f"task_proposal:{tid}", json.dumps(
        {"feedback": "add a docstring", "status": "done", "amend": True,
         "plan": "AMENDMENT:\n~ calc.py → docstring on subtract"}))
    store.kv_set(f"task_gate:{tid}", "change")
    store.update_task(tid, status="awaiting_approval")
    return tid


def test_an_approved_amendment_is_running_never_done(monkeypatch):
    tid = _awaiting_amendment()
    seen = {}
    statuses = []
    real = store.update_task

    def watch(task_id, **kw):
        if task_id == tid and "status" in kw:
            statuses.append(kw["status"])
        return real(task_id, **kw)

    async def fake_refine(task_id, brief, **kw):
        seen.update(kw, status=store.get_task(task_id)["status"],
                    fresh=store.kv_get(f"task_fresh_next:{task_id}"))
        return "ok"
    monkeypatch.setattr(store, "update_task", watch)
    monkeypatch.setattr(tasks, "refine", fake_refine)

    async def go():
        msg = tasks.reply(tid, f"approve task {tid}")
        await asyncio.sleep(0.05)
        return msg
    msg = asyncio.run(go())
    assert "implementing now" in msg
    assert "done" not in statuses and statuses[0] == "running"
    assert seen["status"] == "running" and seen["resume_from"] == "done"
    assert seen["approved_plan"] is True


def _quietly(coro):
    async def go():
        try:
            await coro
        except Exception:                                   # noqa: BLE001
            pass
        await asyncio.sleep(0)
    asyncio.run(go())


def test_the_approved_build_keeps_the_amendment_session(monkeypatch):
    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(tasks, "ensure_checkout", nothing)
    monkeypatch.setattr(tasks, "_resume_worker", nothing)
    t = store.create_task("Add subtract to calc.py", "code", "p", None)
    store.update_task(t["id"], status="running")
    _quietly(tasks.refine(t["id"], "build it", code_change=True,
                          approved_plan=True, resume_from="done"))
    assert not store.kv_get(f"task_fresh_next:{t['id']}")
    # Control: an ordinary follow-up on finished work still starts fresh.
    monkeypatch.setattr(tasks, "propose_change", nothing)
    u = store.create_task("Add subtract to calc.py", "code", "p", None)
    store.update_task(u["id"], status="done")
    _quietly(tasks.refine(u["id"], "add a docstring to subtract", code_change=True))
    assert store.kv_get(f"task_fresh_next:{u['id']}") == "1"


def test_if_the_build_cannot_start_the_task_goes_back(monkeypatch):
    tid = _awaiting_amendment()
    told = []

    async def broken(*a, **k):
        raise ValueError("checkout gone")

    async def push(text, *a, **k):
        told.append(text)
    from app import notify
    monkeypatch.setattr(tasks, "refine", broken)
    monkeypatch.setattr(notify, "notify", push)

    async def go():
        tasks.reply(tid, f"approve task {tid}")
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert store.get_task(tid)["status"] == "done"
    assert told and "did not start" in told[0]
