"""11 Nov: Claude is the primary brain; a finished review says how to post it,
and a waiting review whose PR moved is dropped. Synthetic PRs."""
from __future__ import annotations

import asyncio

import pytest

from app import agent, offers, pr_authority, repo_ops, review, store, tasks

REF = "org/svc-booking#4321"
OLD = REF + "@" + "a" * 40 + ":f1"
NEW = REF + "@" + "b" * 40 + ":f2"


def test_claude_is_the_default_brain(monkeypatch):
    monkeypatch.delenv("ASTA_EXECUTOR", raising=False)
    assert repo_ops.default_executor() == "claude"
    monkeypatch.setenv("ASTA_EXECUTOR", "copilot")
    assert repo_ops.default_executor() == "copilot", "his setting still wins"


def test_a_new_task_runs_on_claude(monkeypatch):
    monkeypatch.delenv("ASTA_EXECUTOR", raising=False)
    monkeypatch.setattr(tasks, "brain_override", lambda: "")
    t = store.create_task("x", "code", "p", "ws")
    assert tasks._resolve_executor(t["id"]) == "claude"


def test_chat_uses_claude_first_when_both_can_answer(monkeypatch):
    monkeypatch.delenv("ASTA_EXECUTOR", raising=False)
    monkeypatch.setattr(agent, "model_registry",
                        lambda: {n: {"available": True} for n in agent.EXECUTORS})
    assert agent.default_chat_model() == agent.from_exec_name("claude")


def _review(pr, revision=OLD):
    return offers.staged_write("pr_review_inline",
                               {"pr": pr, "action": "comment", "body": "x", "comments": [],
                                "review_origin": {"ref": pr, "revision": revision}},
                               f"🔎 Comment on {pr}", "x", "Post?", kind="pr_write")


def test_a_review_behind_another_says_how_to_post_it():
    _review("org/svc-booking#4300")
    _review(REF)
    t = store.create_task("Review booking PR 4321", "analysis", "p", None)
    hint = tasks._how_to_post(t["id"], store.get_task(t["id"]))
    assert "post PR 4321" in hint and "bare “yes” answers" in hint


def test_a_review_in_front_says_yes_works():
    _review(REF)
    t = store.create_task("Review booking PR 4321", "analysis", "p", None)
    assert "Say “yes” or “post PR 4321”" in tasks._how_to_post(t["id"], store.get_task(t["id"]))


def test_no_review_waiting_adds_nothing():
    t = store.create_task("Review booking PR 4999", "analysis", "p", None)
    assert tasks._how_to_post(t["id"], store.get_task(t["id"])) == ""


@pytest.fixture
def head(monkeypatch):
    state = {"now": OLD}

    async def revision(pr):
        return state["now"], True
    monkeypatch.setattr(review, "revision", revision)
    return state


def test_a_waiting_review_on_a_moved_pr_is_dropped(head):
    _review(REF)
    _review("org/svc-booking#4300", "org/svc-booking#4300@" + "c" * 40 + ":x")
    head["now"] = NEW
    notes = asyncio.run(pr_authority.stale_reviews_sweep())
    assert offers.review_for_pr("4321") is None and any("new commit" in n for n in notes)


def test_a_waiting_review_on_an_unchanged_pr_stays(head):
    _review(REF)
    assert asyncio.run(pr_authority.stale_reviews_sweep()) == []
    assert offers.review_for_pr("4321") is not None
