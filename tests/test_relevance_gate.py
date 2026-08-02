"""The gate wired into delegate_task — the model's own spawn path, end to end.

Drives the real `agent.delegate_task` with the trigger bound exactly as `_run_turn`
binds it, and a faked `tasks.spawn` so no worker is launched. Proves the control
flow the incident needed: a passive question is held before a task is created, a
real request for work still spawns, and a disabled gate spawns regardless.
"""

from __future__ import annotations

import contextvars

import pytest

from app import agent as agent_mod
from app import relevance, store, tasks


@pytest.fixture
def no_worker(monkeypatch):
    """Capture spawns instead of firing a background worker (which would reach a brain)."""
    spawned: list[dict] = []

    def fake_spawn(title, prompt, kind="analysis", workspace=None, teams_chat=""):
        spawned.append({"title": title, "kind": kind, "workspace": workspace})
        return {"id": 4242}

    monkeypatch.setattr(tasks, "spawn", fake_spawn)
    return spawned


def _run(trigger: str, **kw):
    """delegate_task, with the turn trigger bound in an isolated context."""
    ctx = contextvars.copy_context()
    ctx.run(relevance.bind_trigger, trigger)
    return ctx.run(lambda: agent_mod.delegate_task(**kw))


def test_passive_question_is_held_before_any_task_is_created(no_worker, monkeypatch):
    monkeypatch.setenv("ASTA_RELEVANCE", "1")
    out = _run("No recent one..?", title="analyse contmark-agent-harness",
               prompt="look at the repo", kind="analysis")
    assert "held off" in out                    # the model gets a confirm to relay
    assert no_worker == []                       # nothing was spawned — the incident is stopped
    counts = {r["outcome"]: r["n"] for r in store.outcome_counts(0.0) if r["kind"] == "relevance"}
    assert counts == {"held": 1}                 # and it's on the scoreboard


def test_a_real_request_for_work_still_spawns(no_worker, monkeypatch):
    monkeypatch.setenv("ASTA_RELEVANCE", "1")
    out = _run("analyse the contmark-agent-harness repo", title="analyse repo",
               prompt="look at the repo", kind="analysis")
    assert "spawned" in out
    assert no_worker == [{"title": "analyse repo", "kind": "analysis", "workspace": None}]


def test_disabled_spawns_even_on_a_passive_question(no_worker, monkeypatch):
    """The safety contract: byte-identical to today until ASTA_RELEVANCE is set."""
    monkeypatch.delenv("ASTA_RELEVANCE", raising=False)
    out = _run("No recent one..?", title="analyse repo", prompt="look", kind="analysis")
    assert "spawned" in out
    assert len(no_worker) == 1


def test_a_code_task_off_a_passive_question_is_held_too(no_worker, monkeypatch):
    monkeypatch.setenv("ASTA_RELEVANCE", "1")
    out = _run("did that ever get fixed?", title="fix it", prompt="edit code",
               kind="code", workspace="asta")
    assert "held off" in out and "make that change" in out
    assert no_worker == []
