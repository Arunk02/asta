"""The approved plan is kept as a durable definition of done (GSD's one good idea).

Two guarantees: off by default it changes nothing (no capture, no prompt change),
and when on, the plan Arun approves is what a later compacted implementation leg is
re-anchored to — not a fresh guess. Captured at approval, the one moment the text
is unambiguously the definition of done.
"""

from __future__ import annotations

import asyncio

import pytest

from app import store, task_spec, tasks


# --- the artifact itself --------------------------------------------------------

def test_disabled_captures_nothing_and_injects_nothing(monkeypatch):
    monkeypatch.delenv("ASTA_TASK_SPEC", raising=False)
    task_spec.capture(1, "PLAN READY\nchange retry.py: add backoff")
    assert task_spec.get(1) == ""
    assert task_spec.preamble(1) == ""


def test_enabled_keeps_the_approved_plan(monkeypatch):
    monkeypatch.setenv("ASTA_TASK_SPEC", "1")
    task_spec.capture(1, "discovery noise…\nPLAN READY\nchange retry.py: add backoff")
    assert "add backoff" in task_spec.get(1)


def test_the_first_approved_plan_wins(monkeypatch):
    monkeypatch.setenv("ASTA_TASK_SPEC", "1")
    task_spec.capture(1, "PLAN: original bar")
    task_spec.capture(1, "PLAN: a different, later bar")
    assert task_spec.get(1) == "PLAN: original bar"   # not overwritten


def test_capture_keeps_the_tail_where_the_plan_lives(monkeypatch):
    monkeypatch.setenv("ASTA_TASK_SPEC", "1")
    long_head = "x" * 5000
    task_spec.capture(1, long_head + "\nPLAN READY\nthe actual plan is here")
    assert "the actual plan is here" in task_spec.get(1)
    assert len(task_spec.get(1)) <= task_spec._MAX


def test_preamble_carries_the_plan_and_the_do_exactly_this_instruction(monkeypatch):
    monkeypatch.setenv("ASTA_TASK_SPEC", "1")
    task_spec.capture(7, "change retry.py: add exponential backoff")
    pre = task_spec.preamble(7)
    assert "Definition of done" in pre
    assert "add exponential backoff" in pre


# --- wired into the approval + resume path -------------------------------------

async def _noop(*a, **k):
    return None


def test_approving_a_plan_captures_it_as_the_spec(monkeypatch):
    monkeypatch.setenv("ASTA_TASK_SPEC", "1")
    monkeypatch.setattr(tasks, "_resume_worker", _noop)     # don't run a brain

    t = store.create_task("add retry", "code", "add a retry to the client", None)
    store.update_task(t["id"], status="awaiting_approval",
                      result="…discovery…\nPLAN READY\nretry.py: wrap send() in backoff")

    async def go():
        tasks.reply(t["id"], "PLAN APPROVED")
        await asyncio.sleep(0)                              # let the spawned noop settle
    asyncio.run(go())

    assert "wrap send() in backoff" in task_spec.get(t["id"])


def test_feedback_does_not_capture_a_spec(monkeypatch):
    monkeypatch.setenv("ASTA_TASK_SPEC", "1")
    monkeypatch.setattr(tasks, "_resume_worker", _noop)

    t = store.create_task("add retry", "code", "add a retry", None)
    store.update_task(t["id"], status="awaiting_approval",
                      result="PLAN READY\nsome plan")

    async def go():
        tasks.reply(t["id"], "actually, use a fixed delay instead")
        await asyncio.sleep(0)
    asyncio.run(go())

    assert task_spec.get(t["id"]) == ""                     # only an approval sets the bar


def test_the_implementation_leg_is_re_anchored_to_the_spec(monkeypatch):
    monkeypatch.setenv("ASTA_TASK_SPEC", "1")
    t = store.create_task("add retry", "code", "add a retry", None)
    store.kv_set(f"task_spec:{t['id']}", "retry.py: wrap send() in exponential backoff")

    captured = {}

    async def fake_leg(task_id, prompt, cwd, **kw):
        captured["prompt"] = prompt
        return "done: diff"

    monkeypatch.setattr(tasks, "_run_code_leg", fake_leg)
    monkeypatch.setattr(tasks, "_finish_code", _noop)
    asyncio.run(tasks._resume_worker(t["id"], "PLAN APPROVED", approved=True))

    assert "Definition of done" in captured["prompt"]
    assert "exponential backoff" in captured["prompt"]


def test_disabled_resume_prompt_is_unchanged(monkeypatch):
    monkeypatch.delenv("ASTA_TASK_SPEC", raising=False)
    t = store.create_task("add retry", "code", "add a retry", None)
    store.kv_set(f"task_spec:{t['id']}", "some captured plan")   # even if one exists

    captured = {}

    async def fake_leg(task_id, prompt, cwd, **kw):
        captured["prompt"] = prompt
        return "done"

    monkeypatch.setattr(tasks, "_run_code_leg", fake_leg)
    monkeypatch.setattr(tasks, "_finish_code", _noop)
    asyncio.run(tasks._resume_worker(t["id"], "PLAN APPROVED", approved=True))

    assert "Definition of done" not in captured["prompt"]       # no injection when off
