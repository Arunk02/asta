"""A delegated task that hits a usage limit is PAUSED, not lost.

From a real WhatsApp thread, 31 July:

    Task #53 failed — Fix BEPTELIKOS-9875 … claude exited 1: You've hit your
    session limit · resets 3:40pm (Asia/Calcutta)
    …
    Arun   53 task can you continue now and finish it
    Asta   There's no task #53 I can act on …

Claude's five-hour subscription window closed mid-task. Three defects lined up so
the task died with no way back:

  1. The limit wasn't recognised as transient — the only classifier looked for
     the substring "quota", which "session limit" doesn't contain — so there was
     no failover, no cooldown, no checkpoint.
  2. The task was marked `failed`, and the pinned CLI session left to rot.
  3. `failed` has no resume path: reply()/approve() require awaiting_approval, and
     "continue task 53" found no *live* task, so it fell through to a fresh brain
     turn with zero context — which is why Asta said "no task #53".

These pin the fix: a transient limit is a PAUSE that keeps the session, auto-
resumes when the brain renews, and can be picked up by hand from any channel.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app import store


def _capture_notify(monkeypatch):
    notes: list[str] = []

    async def fake(msg, kind="task", **k):
        notes.append(msg)

    monkeypatch.setattr("app.notify.notify", fake)
    return notes


# --- the classifier is the root cause ----------------------------------------

def test_a_session_limit_is_a_transient_limit_not_a_crash():
    from app import agent
    # the exact wording that slipped past the old "quota"-only test
    assert agent.transient_limit("claude exited 1: You've hit your session limit "
                                 "· resets 3:40pm (Asia/Calcutta)")
    assert agent.transient_limit("You have exceeded your monthly quota")
    assert agent.transient_limit("429 too many requests")
    # a real crash is NOT a limit — resuming would just repeat it
    assert not agent.transient_limit("TypeError: 'NoneType' object is not subscriptable")
    # a drained prepaid balance does not self-heal, so it must NOT be paused-on
    assert not agent.transient_limit("Your credit balance is too low to run this")


def test_the_reset_time_is_parsed_so_auto_resume_knows_when():
    from app import agent
    at = agent.limit_reset_at("You've hit your session limit · resets 3:40pm (Asia/Calcutta)")
    assert at and at > time.time()                       # a real, future instant
    import datetime as dt
    assert dt.datetime.fromtimestamp(at).strftime("%H:%M") == "15:40"
    assert agent.limit_reset_at("no time mentioned here") is None


# --- a limited leg pauses instead of failing ---------------------------------

def test_a_code_leg_that_hits_its_limit_pauses_not_fails(monkeypatch):
    """Claude session limit — a stated reset time → paused AND a durable
    auto-resume armed for when the window renews."""
    from app import tasks
    notes = _capture_notify(monkeypatch)
    t = store.create_task("fix bug", "code", "do the thing", None)
    reset = time.time() + 600

    async def boom(task_id, tt, prompt):
        raise tasks._LimitPaused("claude", reset, "You've hit your session limit")

    monkeypatch.setattr(tasks, "_run_simple", boom)
    asyncio.run(tasks._worker(t["id"]))

    row = store.get_task(t["id"])
    assert row["status"] == "paused"                     # THE fix — not "failed"
    assert store.kv_get(f"task_resume_at:{t['id']}")     # auto-resume armed (we know when)
    assert any("paused" in n.lower() for n in notes)     # and he was told, with options


def test_a_limit_with_no_reset_time_waits_instead_of_looping(monkeypatch):
    """Copilot's monthly quota states no reset time — so it must NOT arm a blind
    auto-resume that would ping Arun every half hour. It pauses and waits for him
    to resume or switch. Same pause path, consistent across brains."""
    from app import tasks
    notes = _capture_notify(monkeypatch)
    t = store.create_task("fix bug", "code", "do the thing", None)

    async def boom(task_id, tt, prompt):
        raise tasks._LimitPaused("copilot", None, "You have exceeded your monthly quota")

    monkeypatch.setattr(tasks, "_run_simple", boom)
    asyncio.run(tasks._worker(t["id"]))

    assert store.get_task(t["id"])["status"] == "paused"       # still paused, not failed
    assert not store.kv_get(f"task_resume_at:{t['id']}")       # but NO blind auto-resume
    assert any("resume task" in n.lower() for n in notes)      # told how to pick it up


def test_a_claude_session_limit_is_wrapped_as_a_pause(monkeypatch):
    from app import claude_cli, tasks
    t = store.create_task("fix", "code", "prompt", None)
    store.kv_set(f"task_executor:{t['id']}", "claude")

    async def limited(*a, **k):
        raise RuntimeError("claude exited 1: You've hit your session limit · "
                           "resets 3:40pm (Asia/Calcutta)")

    monkeypatch.setattr(claude_cli, "one_shot", limited)
    with pytest.raises(tasks._LimitPaused) as ei:
        asyncio.run(tasks._run_code_leg(t["id"], "prompt", ".", resume=True,
                                        effort="high", workspace=None))
    assert ei.value.brain == "claude"
    assert ei.value.reset_at and ei.value.reset_at > time.time()


# --- resuming re-attaches the session (no re-discovery) -----------------------

def test_resume_reattaches_the_same_session(monkeypatch):
    from app import tasks
    _capture_notify(monkeypatch)
    t = store.create_task("fix", "code", "prompt", None)
    store.update_task(t["id"], status="paused")
    store.kv_set(f"task_executor:{t['id']}", "claude")
    store.kv_set(f"task_session:{t['id']}:claude", "sid-1")
    store.kv_set(f"task_resume_at:{t['id']}", str(time.time()))

    seen: dict = {}

    async def fake_leg(task_id, prompt, cwd, *, resume, effort, workspace=None):
        seen["resume"] = resume
        return "all done"

    async def fake_finish(task_id, tt, result, hops):
        store.update_task(task_id, status="done")

    monkeypatch.setattr(tasks, "_run_code_leg", fake_leg)
    monkeypatch.setattr(tasks, "_finish_code", fake_finish)

    async def go():
        detail = await tasks.resume_task(t["id"])
        await tasks._running[t["id"]]                    # let the resume worker finish
        return detail

    detail = asyncio.run(go())
    assert "resuming" in detail.lower()
    assert seen["resume"] is True                        # re-attached, did NOT restart
    assert store.get_task(t["id"])["status"] == "done"
    assert not store.kv_get(f"task_resume_at:{t['id']}")  # the pending auto-resume cleared


def test_resume_can_switch_to_another_brain(monkeypatch):
    from app import tasks
    _capture_notify(monkeypatch)
    t = store.create_task("fix", "code", "prompt", None)
    store.update_task(t["id"], status="paused")
    store.kv_set(f"task_executor:{t['id']}", "claude")

    async def fake_leg(*a, **k):
        return "done"

    async def fake_finish(task_id, tt, result, hops):
        store.update_task(task_id, status="done")

    monkeypatch.setattr(tasks, "_run_code_leg", fake_leg)
    monkeypatch.setattr(tasks, "_finish_code", fake_finish)

    async def go():
        d = await tasks.resume_task(t["id"], switch_to="copilot")
        await tasks._running[t["id"]]
        return d

    d = asyncio.run(go())
    assert store.kv_get(f"task_executor:{t['id']}") == "copilot"   # moved brains
    assert "copilot" in d


def test_a_running_task_is_not_resumed_twice(monkeypatch):
    from app import tasks
    t = store.create_task("fix", "code", "prompt", None)
    store.update_task(t["id"], status="running")
    with pytest.raises(ValueError):
        asyncio.run(tasks.resume_task(t["id"]))          # only paused/failed resume


# --- auto-resume once the brain renews ---------------------------------------

def test_auto_resume_fires_only_when_the_limit_has_lifted(monkeypatch):
    from app import tasks
    _capture_notify(monkeypatch)
    ready = store.create_task("ready", "code", "p", None)
    store.update_task(ready["id"], status="paused")
    store.kv_set(f"task_resume_at:{ready['id']}", str(time.time() - 5))     # due
    later = store.create_task("later", "code", "p", None)
    store.update_task(later["id"], status="paused")
    store.kv_set(f"task_resume_at:{later['id']}", str(time.time() + 3600))  # not yet

    kicked: list[int] = []

    async def fake_resume(task_id, switch_to=""):
        kicked.append(task_id)
        return "ok"

    monkeypatch.setattr(tasks, "resume_task", fake_resume)
    fired = asyncio.run(tasks._resume_due())
    assert ready["id"] in fired and later["id"] not in fired
    assert kicked == [ready["id"]]


# --- picking it back up from chat --------------------------------------------

def test_resume_task_phrasings_are_recognised():
    from app import main as main_mod
    assert main_mod._RESUME_TASK.match("resume task 53").group(1) == "53"
    assert main_mod._RESUME_TASK.match("retry task 7").group(1) == "7"
    assert main_mod._RESUME_TASK.match("continue task 12").group(1) == "12"
    assert main_mod._RESUME_TASK.match("what is task 5") is None      # a question, not a command
    m = main_mod._TASK_SWITCH.match("task 53 use copilot")
    assert m.group(1) == "53" and m.group(2) == "copilot"
    m2 = main_mod._TASK_SWITCH.match("9 switch to claude")
    assert m2.group(1) == "9" and m2.group(2) == "claude"


def test_paused_tasks_are_found_for_a_conversation():
    from app import tasks
    t = store.create_task("x", "code", "p", None)
    tasks.link_task("conv-1", t["id"])
    store.update_task(t["id"], status="paused")
    assert tasks.paused_tasks_for("conv-1") == [t["id"]]
    store.update_task(t["id"], status="running")         # a live one isn't "paused"
    assert tasks.paused_tasks_for("conv-1") == []
