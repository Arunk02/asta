"""Round 5, PR G: a follow-up on finished work starts a fresh session from a
short handoff; work in progress keeps its own session."""
from __future__ import annotations

import subprocess

import pytest

from app import store, tasks


def _task(status: str, **kw) -> dict:
    t = store.create_task("Validate rail ETA like vessel ETA", "code", "ABC-1 mirror the check",
                          workspace=None)
    store.update_task(t["id"], status=status, **kw)
    return store.get_task(t["id"])


@pytest.mark.parametrize("status", ["done", "shipped", "failed", "pr_changes_requested", "merged"])
def test_finished_work_gets_a_fresh_session(status, monkeypatch):
    monkeypatch.delenv("ASTA_FRESH_FOLLOWUP", raising=False)
    t = _task(status)
    tasks.mark_fresh_followup(t["id"])
    assert store.kv_get(f"task_fresh_next:{t['id']}") == "1"


@pytest.mark.parametrize("status", ["running", "paused", "awaiting_approval", "queued"])
def test_work_in_progress_keeps_its_session(status):
    t = _task(status)
    tasks.mark_fresh_followup(t["id"])
    assert not store.kv_get(f"task_fresh_next:{t['id']}")


def test_the_switch_turns_it_off(monkeypatch):
    monkeypatch.setenv("ASTA_FRESH_FOLLOWUP", "0")
    t = _task("shipped")
    tasks.mark_fresh_followup(t["id"])
    assert not store.kv_get(f"task_fresh_next:{t['id']}")


def test_the_handoff_carries_plan_branch_and_pr(tmp_path):
    repo = tmp_path / "svc"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    run("init", "-q", "-b", "ABC-1-rail-eta")
    (repo / "A.java").write_text("class A {}\n")
    run("add", "-A")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "Mirror the ETA check")
    t = _task("shipped", pr_urls="svc: https://example.test/pr/1")
    store.kv_set(f"task_plan:{t['id']}", "STRUCTURE\n  RailEtaValidator  new check")
    text = tasks.handoff(t["id"], str(tmp_path))
    assert "RailEtaValidator" in text
    assert "ABC-1-rail-eta" in text and "Mirror the ETA check" in text
    assert "https://example.test/pr/1" in text
    assert "ABC-1 mirror the check" in text
    assert len(text) < 8000


def test_the_next_leg_drops_the_old_session_and_starts_from_the_handoff(monkeypatch):
    import asyncio

    from app import claude_cli
    t = _task("shipped")
    tid = t["id"]
    store.kv_set(f"task_session:{tid}:claude", "old-session-id")
    store.kv_set(f"task_plan:{tid}", "STRUCTURE\n  RailEtaValidator  new check")
    seen = {}

    async def _cli(task_id, executor, prompt, **kw):
        seen.update(kw, prompt=prompt)
        return "ok"

    monkeypatch.setattr(tasks, "_observed_cli", _cli)
    monkeypatch.setattr(tasks, "task_tools", lambda *a, **k: "")
    monkeypatch.setattr(tasks, "leg_executor", lambda task_id: "claude")
    tasks.mark_fresh_followup(tid)
    asyncio.run(tasks._code_leg(tid, "push the fix", "/tmp", resume=True, effort="high"))
    assert seen["session_id"] != "old-session-id"
    assert seen["resume"] is False
    assert seen["prompt"].startswith("HANDOFF")
    assert "RailEtaValidator" in seen["prompt"]
    assert not store.kv_get(f"task_fresh_next:{tid}")
    # The leg after it resumes the NEW session, as any multi-step leg does.
    seen.clear()
    asyncio.run(tasks._code_leg(tid, "and the test", "/tmp", resume=True, effort="high"))
    assert seen["resume"] is True and not seen["prompt"].startswith("HANDOFF")
    assert claude_cli  # imported for the executor table
