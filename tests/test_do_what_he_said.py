"""A repo, environment or PR he named is decided — never asked back (5 Nov).

Synthetic replay: "replicate the fix in svc-alpha" got back "Should the fix target
svc-beta, or did you mean the svc-alpha repo?" The names are made up.
"""
from __future__ import annotations

import asyncio

import pytest

from app import agent, asking, store, tasks


@pytest.fixture(autouse=True)
def _repos(monkeypatch):
    monkeypatch.setattr(asking, "_repo_words", lambda: {"ap", "alpha", "beta"})


@pytest.mark.parametrize("question, his, named", [
    ("Should the fix target beta, or did you mean the alpha repo?",
     "send this and replicate fix it in alpha", "alpha"),
    ("Which environment — sit or uat?", "check it in sit", "sit"),
    ("Push to PR 1527 or open a new one?", "fix the CI on pr 1527", "PR 1527"),
])
def test_a_choice_he_already_made_is_recognised(question, his, named):
    assert asking.already_named(question, his) == named


@pytest.mark.parametrize("question, his", [
    ("Which environment is it in?", "why is the date missing"),      # he named none
    ("Is alpha's dispatcher the one you mean?", "fix it in alpha"),  # not a choice
    ("Which of the two tests should go?", "add tests in alpha"),     # names nothing he said
])
def test_a_real_question_still_goes_to_him(question, his):
    assert asking.already_named(question, his) == ""


def test_the_brain_is_told_to_do_it_instead_of_asking(monkeypatch):
    conv = store.create_conversation("claude_cli", None)
    store.add_ui_message(conv["id"], "user", "send this and replicate fix it in alpha", {})

    async def never(*a, **k):
        pytest.fail("he was asked to choose a repo he had named")
    monkeypatch.setattr(asking, "ask", never)
    token = tasks.bind_conversation(conv["id"]) if hasattr(tasks, "bind_conversation") else None
    if token is None:
        token = tasks._TURN_CONV.set(conv["id"])
    try:
        said = asyncio.run(agent.ask_user("Should the fix target beta, or the alpha repo?"))
    finally:
        tasks._TURN_CONV.reset(token)
    assert said.startswith("Not asked") and "alpha" in said and "proof" in said
