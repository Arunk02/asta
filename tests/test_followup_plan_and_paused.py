"""11 Nov, task 521: a follow-up came back as a PLAN and was announced "already in
place, nothing to change"; a follow-up sent while the task was paused was
refused. Synthetic tasks."""
from __future__ import annotations

import asyncio
import json
import types

import pytest

from app import go, notify, store, tasks

PLAN = ("Found the gaps. Plan below — not implemented (this work is new and the brief "
        "asks for a plan gate).\n\nSTRUCTURE\n  P1a guard the reaffirm path\n  P2a skip in the activity")


@pytest.fixture
def told(monkeypatch):
    lines = []

    async def notify_(text, *a, **k):
        lines.append(text)
    monkeypatch.setattr(notify, "notify", notify_)
    return lines


def _task(status="shipped"):
    t = store.create_task("Skip the tracker call", "code", "p", "ws")
    store.update_task(t["id"], status=status)
    return t


def test_a_plan_is_recognised_and_a_real_no_change_is_not():
    assert tasks._RETURNED_PLAN.search(PLAN)
    assert not tasks._RETURNED_PLAN.search("Already pushed — the PR is updated.")


def test_a_returned_plan_waits_at_the_plan_gate(told):
    t = _task()
    asyncio.run(tasks._stage_returned_plan(t["id"], t, PLAN, "shipped"))
    row = store.get_task(t["id"])
    assert row["status"] == "awaiting_approval" and store.kv_get(f"task_gate:{t['id']}") == "change"
    assert "proposed change" in told[0] and "P2a skip" in told[0] and "nothing to change" not in told[0]
    assert json.loads(store.kv_get(f"task_proposal:{t['id']}"))["status"] == "shipped"


def test_after_his_go_the_returned_plan_is_implemented(told, monkeypatch):
    t = _task()
    go.grant(t["id"], "Once u done plan go ahead and implement and raise PR", ship=True)
    approved = []

    async def approve(task_id, *a, **k):
        approved.append(task_id)
        return ""
    monkeypatch.setattr(tasks, "approve", approve)
    asyncio.run(tasks._stage_returned_plan(t["id"], t, PLAN, "shipped"))
    assert approved == [t["id"]] and "implementing it now" in told[0]


def test_the_no_change_path_stages_a_plan_instead_of_announcing_it_done(told, monkeypatch, tmp_path):
    t = _task()
    store.kv_set(tasks._FOLLOWUP + str(t["id"]), json.dumps(
        {"status": "shipped", "heads": {str(tmp_path): "abc"}}))

    async def git(repo, *args, **k):
        return 0, ("abc" if "rev-parse" in args else "")

    async def pushed(*a, **k):
        return False
    monkeypatch.setattr(tasks.repo_ops, "git", git)
    monkeypatch.setattr(tasks, "_already_pushed", pushed)
    assert asyncio.run(tasks._followup_changed_nothing(t["id"], t, PLAN)) is True
    assert store.get_task(t["id"])["status"] == "awaiting_approval"
    assert all("nothing to change" not in x for x in told)


def test_a_follow_up_to_a_paused_task_is_kept_and_resumes_it(monkeypatch):
    t = _task(status="paused")
    resumed = []

    async def resume(task_id, switch_to=""):
        resumed.append(task_id)
        return f"Task #{task_id}: resuming."
    monkeypatch.setattr(tasks, "resume_task", resume)
    out = asyncio.run(tasks.refine(t["id"], "also skip it for grey pool orders"))
    assert "cannot be continued" not in out and resumed == [t["id"]]
    assert "grey pool" in tasks.note_waiting(t["id"])


def test_the_resume_carries_what_he_said_while_paused(monkeypatch):
    t = _task(status="paused")
    store.kv_set(f"task_addenda:{t['id']}", "also skip it for grey pool orders")
    seen = {}

    async def worker(task_id, prompt, approved=False):
        seen["prompt"] = prompt

    async def checkout(task_id):
        return None
    monkeypatch.setattr(tasks, "_resume_worker", worker)
    monkeypatch.setattr(tasks, "ensure_checkout", checkout)
    monkeypatch.setattr(tasks, "_graph", lambda: types.SimpleNamespace(manages=lambda tid: False))

    async def go_():
        out = await tasks.resume_task(t["id"])
        await asyncio.sleep(0)
        return out
    asyncio.run(go_())
    assert "grey pool" in seen["prompt"] and tasks.note_waiting(t["id"]) == ""
