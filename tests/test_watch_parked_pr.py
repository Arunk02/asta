"""A shipped task waiting on his yes to a fix plan keeps its PR watched (5 Nov).

Synthetic: the repo, PR number and task are made up.
"""
from __future__ import annotations

import json

import pytest

from app import store, tasks

URL = "https://github.com/acme/svc-booking/pull/4242"


def _parked(pr_state="red/REVIEW_REQUIRED"):
    t = store.create_task("Skip the tracker call", "code", "p", "ws")
    store.update_task(t["id"], status="awaiting_approval", pr_urls=f"svc-booking: {URL}",
                      pr_state=pr_state)
    store.kv_set(f"task_gate:{t['id']}", "change")
    store.kv_set(f"task_proposal:{t['id']}", json.dumps(
        {"feedback": tasks.CI_FIX_BRIEF, "status": "pr_ci_failed", "plan": "CAUSE: unknown"}))
    return t["id"]


def _pr(monkeypatch, *, checks="FAILURE", decision="REVIEW_REQUIRED", state="OPEN", calls=None):
    async def pr(url):
        if calls is not None:
            calls.append(url)
        return {"state": state, "reviewDecision": decision,
                "statusCheckRollup": [{"name": "cicd / Build", "conclusion": checks}]}
    monkeypatch.setattr(tasks, "_pr_state", pr)

    async def no(*a, **k):
        pytest.fail("a parked task must not be re-run, re-planned or merged")
    monkeypatch.setattr(tasks, "rerun_ci", no)
    monkeypatch.setattr(tasks, "propose_change", no)


def test_a_parked_task_is_still_on_the_watch_list():
    tid = _parked()
    assert tid in tasks.open_prs()
    store.kv_del(f"task_gate:{tid}")              # an ordinary plan gate, not a fix plan
    assert tid not in tasks.open_prs()


@pytest.mark.asyncio
async def test_still_red_is_not_news_and_nothing_is_driven(monkeypatch):
    tid = _parked()
    calls: list = []
    _pr(monkeypatch, calls=calls)
    assert await tasks.check_pr(tid) is None
    assert calls == [URL], "it was actually looked at"
    t = store.get_task(tid)
    assert t["status"] == "awaiting_approval" and store.kv_get(f"task_gate:{tid}") == "change"


@pytest.mark.asyncio
async def test_going_green_drops_the_plan_it_no_longer_needs(monkeypatch):
    tid = _parked()
    _pr(monkeypatch, checks="SUCCESS")
    note = await tasks.check_pr(tid)
    assert "CI green" in note and "not needed now" in note
    assert store.get_task(tid)["status"] == "shipped"
    assert not store.kv_get(f"task_gate:{tid}") and not store.kv_get(f"task_proposal:{tid}")
    assert tid in tasks.open_prs(), "and it is watched until it merges"


@pytest.mark.asyncio
async def test_a_review_change_is_told_and_the_plan_keeps_waiting(monkeypatch):
    tid = _parked()
    _pr(monkeypatch, decision="CHANGES_REQUESTED")
    note = await tasks.check_pr(tid)
    assert "changes requested" in note and "still waiting on you" in note
    assert store.get_task(tid)["status"] == "awaiting_approval"


@pytest.mark.parametrize("state, status", [("MERGED", "merged"), ("CLOSED", "pr_closed")])
@pytest.mark.asyncio
async def test_merged_or_closed_ends_it_and_the_plan_with_it(monkeypatch, state, status):
    tid = _parked()
    _pr(monkeypatch, state=state)
    assert await tasks.check_pr(tid)
    assert store.get_task(tid)["status"] == status and not store.kv_get(f"task_gate:{tid}")


@pytest.mark.asyncio
async def test_no_to_the_fix_plan_keeps_the_task_and_its_watch():
    tid = _parked()
    said = await tasks.reject(tid, why="the cause is the quality gate")
    assert "dropped the proposed change" in said and f"cancel {tid}" in said
    t = store.get_task(tid)
    assert t["status"] == "pr_ci_failed", "back where it was before the plan"
    assert tid in tasks.open_prs()


@pytest.mark.asyncio
async def test_rejecting_a_task_at_its_first_plan_still_rejects_it(monkeypatch):
    t = store.create_task("Add a flag", "code", "p", "ws")
    store.update_task(t["id"], status="awaiting_approval")
    store.kv_set(f"task_gate:{t['id']}", "plan")

    async def cancel(task_id, status="cancelled", why=""):
        return False
    monkeypatch.setattr(tasks, "cancel", cancel)
    await tasks.reject(t["id"])
    assert store.get_task(t["id"])["status"] == "rejected"
