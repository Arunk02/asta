"""19 Nov: one ask — "enable MX as S4 country in booking and AP on the latest
release branch and raise PR, update and raise PR in prod helm" — became three code
tasks. Two of them changed the same file, so duplicate PRs went up, and each task
stopped for its own yes; "go ahead and create PR, don't need my approval" was
answered "Which one do you mean?".

Synthetic repos, tasks and words throughout.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import agent as agent_mod, capabilities, go, store, tasks

ASK = "enable XX as a new country in svc-a and svc-b on the release branch and raise PR"


@pytest.fixture
def spawner(monkeypatch):
    """tasks.spawn as far as these rules see it: a row, with his words recorded."""
    from app import relevance
    monkeypatch.setattr(relevance, "guard_spawn", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "refinable_match", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "owner_of_pr", lambda *a, **k: None)
    made = []

    def _spawn(title, prompt, kind="analysis", workspace=None, teams_chat="", **k):
        t = store.create_task(title, kind, prompt, workspace, teams_chat)
        store.kv_set(f"task_asked:{t['id']}", json.dumps({"his": capabilities.said_this_turn()}))
        made.append(t)
        return t

    monkeypatch.setattr(tasks, "spawn", _spawn)
    capabilities.TURN_TEXT.set(ASK)
    yield made
    capabilities.TURN_TEXT.set("")


def test_a_second_code_task_for_the_same_ask_is_folded_into_the_first(spawner):
    first = agent_mod.delegate_task("XX — svc-a", "change svc-a", kind="code", workspace="ws")
    second = agent_mod.delegate_task("XX — svc-b", "change svc-b", kind="code", workspace="ws")
    third = agent_mod.delegate_task("XX — prod values", "values", kind="code", workspace="ws")
    assert len(spawner) == 1, (first, second, third)
    tid = spawner[0]["id"]
    assert second.startswith(f"Not a new task: task #{tid}")
    addenda = store.kv_get(f"task_addenda:{tid}") or ""
    assert "svc-b" in addenda and "prod values" in addenda


def test_the_first_task_is_told_the_whole_ask_is_its_own(spawner):
    agent_mod.delegate_task("XX — svc-a", "change svc-a", kind="code", workspace="ws")
    assert spawner[0]["prompt"].endswith(agent_mod.WHOLE_ASK)


def test_a_different_ask_still_gets_its_own_task(spawner):
    agent_mod.delegate_task("XX — svc-a", "change svc-a", kind="code", workspace="ws")
    capabilities.TURN_TEXT.set("fix the null check in svc-c")
    agent_mod.delegate_task("Null check — svc-c", "fix it", kind="code", workspace="ws")
    assert len(spawner) == 2


def test_a_finished_task_does_not_swallow_a_new_ask(spawner):
    agent_mod.delegate_task("XX — svc-a", "change svc-a", kind="code", workspace="ws")
    store.update_task(spawner[0]["id"], status="done")
    agent_mod.delegate_task("XX — svc-a again", "do it again", kind="code", workspace="ws")
    assert len(spawner) == 2


# --- his go-ahead covers every task of one ask -------------------------------------

def _live(words: str, status: str = "awaiting_approval") -> dict:
    t = store.create_task("t", "code", "p", None)
    store.update_task(t["id"], status=status)
    store.kv_set(f"task_asked:{t['id']}", json.dumps({"his": words}))
    return t


def test_go_ahead_with_three_tasks_from_one_ask_approves_all_three(monkeypatch):
    ids = [_live(ASK)["id"] for _ in range(3)]
    approved = []

    async def approve(tid):
        approved.append(tid)
    monkeypatch.setattr(tasks, "approve", approve)
    out = asyncio.run(go.act("Go ahead and create PR don't need my approval update and raise PR",
                             None))
    assert sorted(approved) == sorted(ids)
    assert "Which one" not in out
    assert all(go.ships(tid) for tid in ids)


def test_tasks_from_different_asks_still_ask_which_one(monkeypatch):
    _live(ASK)
    _live("fix the null check in svc-c")
    out = asyncio.run(go.act("go ahead and raise PR", None))
    assert out.startswith("Which one do you mean?")


def test_a_bare_go_ahead_and_approve_is_not_about_a_finished_task():
    t = store.create_task("done earlier", "code", "p", None)
    store.update_task(t["id"], status="done")
    assert asyncio.run(go.act("Yes go ahead and approve", None)) == ""


def test_naming_the_finished_task_still_gets_its_answer():
    t = store.create_task("done earlier", "code", "p", None)
    store.update_task(t["id"], status="done")
    out = asyncio.run(go.act("approve task", t["id"]))
    assert f"#{t['id']}" in out
