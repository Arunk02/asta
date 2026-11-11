"""A paused task resumes on a brain that can work (task 535, Nov).

Its pinned brain was out for the month; every resume tried it again, failed the
same way and paused. Synthetic task ids only.
"""
from __future__ import annotations

import asyncio

from app import store, tasks


def test_sticky_executor_switches_only_when_unavailable(monkeypatch):
    store.kv_set("task_executor:71", "copilot")
    store.kv_set("task_session:71:copilot", "old-session")
    monkeypatch.setattr(tasks, "_can_take_over", lambda ex: ex == "claude")
    assert tasks._resume_executor(71) == ("claude", True)
    assert store.kv_get("task_executor:71") == "claude"
    assert store.kv_get("task_session:71:copilot") == "old-session"
    assert not store.kv_get("task_session:71:claude")


def test_available_sticky_executor_is_preserved(monkeypatch):
    store.kv_set("task_executor:71", "copilot")
    monkeypatch.setattr(tasks, "_can_take_over", lambda ex: True)
    assert tasks._resume_executor(71) == ("copilot", False)


def test_with_nothing_better_it_keeps_its_own_brain(monkeypatch):
    store.kv_set("task_executor:71", "copilot")
    monkeypatch.setattr(tasks, "_can_take_over", lambda ex: False)
    assert tasks._resume_executor(71) == ("copilot", False)
    assert store.kv_get("task_executor:71") == "copilot"


def test_a_brain_out_for_the_month_is_not_retried_on_his_task(monkeypatch):
    """The cool-down has passed, so it looks usable — but its last word was
    'monthly quota exceeded' and nothing has worked on it since."""
    store.kv_set("task_executor:71", "copilot")
    store.kv_set("copilot_quota_exhausted", "1")
    monkeypatch.setattr(tasks, "_can_take_over", lambda ex: True)
    assert tasks._resume_executor(71) == ("claude", True)


def test_once_a_call_works_again_the_month_flag_no_longer_moves_it(monkeypatch):
    from app import agent
    store.kv_set("task_executor:71", "copilot")
    store.kv_set("copilot_quota_exhausted", "1")
    agent.mark_quota_ok("copilot")
    monkeypatch.setattr(tasks, "_can_take_over", lambda ex: True)
    assert tasks._resume_executor(71) == ("copilot", False)


def test_resume_with_no_brain_available_still_resumes_as_before(monkeypatch):
    t = store.create_task("Analyse the logs", "analysis", "p", None)
    store.update_task(t["id"], status="paused")
    monkeypatch.setattr(tasks, "_can_take_over", lambda ex: False)

    async def idle(task_id):
        return None
    monkeypatch.setattr(tasks, "_worker", idle)

    async def go():
        return await tasks.resume_task(t["id"])
    said = asyncio.run(go())
    assert "running it again" in said and store.get_task(t["id"])["status"] == "running"


def test_the_switch_is_said(monkeypatch):
    t = store.create_task("Analyse the logs", "analysis", "p", None)
    store.update_task(t["id"], status="paused")
    store.kv_set(f"task_executor:{t['id']}", "copilot")
    monkeypatch.setattr(tasks, "_can_take_over", lambda ex: ex == "claude")

    async def idle(task_id):
        return None
    monkeypatch.setattr(tasks, "_worker", idle)
    said = asyncio.run(tasks.resume_task(t["id"]))
    assert "on claude" in said
