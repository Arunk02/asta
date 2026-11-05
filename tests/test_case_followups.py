"""The rest of one case, one answer (5 Nov).

Synthetic replay: a colleague's case was handed over, a reply went out in his
name, and later work overturned it. The chat brain is told the case when he
names the person; a fresh session's recap carries the open case; a draft resting
on an unconfirmed finding says so and never goes out automatically; and a task
that overturns what was said raises one correction draft. Names are made up.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app import answers, main, responder, store

CHAT = "Pat Lee"


def _finding(title, result, chat=CHAT, prompt=""):
    t = store.create_task(title, "analysis", prompt or title, None, teams_chat=chat)
    store.update_task(t["id"], status="done", result=result)
    return t


def _handed_case():
    responder.note_handed(CHAT)
    _finding("Pat asked: why is the date missing",
             "ANALYSIS:\nThe tracker answered TRACKING with no dates.\n\nREPLY:\nno dates yet")


def test_the_chat_brain_is_told_the_case_when_he_names_the_person():
    _handed_case()
    conv = store.create_conversation("claude_cli", None)
    text = main._with_open_case(conv["id"], "what about Pat's date one?")
    assert "TRACKING with no dates" in text and text.rstrip().endswith("what about Pat's date one?")
    assert main._with_open_case(conv["id"], "and Pat again?") == "and Pat again?", \
        "told once; the session has it after that"
    assert main._with_open_case(conv["id"], "what about Sam?") == "what about Sam?"


def test_a_handed_chat_is_known_by_its_own_name():
    responder.note_handed(CHAT)
    assert CHAT in responder.handed_chats()
    responder.note_handed("Sam Roe", now=time.time() - responder.CASE_HANDED_SECONDS - 5)
    assert "Sam Roe" not in responder.handed_chats()


def test_a_retired_session_is_handed_the_open_case():
    from app import copilot_cli
    _handed_case()
    conv = store.create_conversation("claude_cli", None)
    store.add_ui_message(conv["id"], "user", "hello", {})
    store.add_ui_message(conv["id"], "assistant", "hi", {})
    store.kv_set(f"session_recap:{conv['id']}", "1")
    recap = copilot_cli._switch_recap(store.get_conversation(conv["id"]), "Claude CLI")
    assert "Pat Lee" in recap and "TRACKING with no dates" in recap


def test_a_draft_resting_on_an_unconfirmed_finding_says_so():
    shown = answers.render(CHAT, "why the date is missing",
                           "It is most likely dropped in svc-beta (hypothesis, not confirmed in logs).",
                           "it's dropped in svc-beta")
    assert "Basis:" in shown and "unconfirmed" in shown
    plain = answers.render(CHAT, "why the date is missing",
                           "Published at 14:08:42 with the date (svc-beta Mapper.java:244).",
                           "the date is in the payload")
    assert "Basis:" not in plain


def test_an_unconfirmed_reply_is_never_sent_automatically(monkeypatch):
    from app import authority
    monkeypatch.setattr(authority, "auto_reply_to", lambda *a, **k: True)

    async def never(*a, **k):
        pytest.fail("an unconfirmed root cause went out on its own")
    monkeypatch.setattr(authority, "send_reply", never)
    intent = {"to": CHAT, "what": "it's dropped in svc-beta", "task_id": 7,
              "analysis": "Most likely dropped in svc-beta — hypothesis, not confirmed."}
    assert asyncio.run(answers._deliver_automatic_reply(intent)) is False


def test_a_task_that_overturns_what_was_said_raises_a_correction(monkeypatch):
    responder.note_handed(CHAT)
    shown = {}

    async def present(**kw):
        shown.update(kw)
        return True
    monkeypatch.setattr(answers, "present", present)
    t = store.create_task("Where is the date dropped", "analysis",
                          "Pat Lee asked where the date is dropped", None)
    result = ("ANALYSIS:\nCorrects: 'dropped in svc-beta's builder' — the published payload "
              "carries it (14:08:42).\n\nREPLY:\ncorrection — it isn't dropped, it's in the payload")
    assert asyncio.run(answers.offer_correction(t["id"], t, result)) is True
    assert shown["chat"] == CHAT and "isn't dropped" in shown["reply"]
    assert "Corrects" in shown["analysis"]


def test_a_result_with_no_correction_offers_none(monkeypatch):
    responder.note_handed(CHAT)
    t = store.create_task("Where is the date dropped", "analysis", "Pat Lee asked", None)
    assert asyncio.run(answers.offer_correction(t["id"], t, "ANALYSIS:\nfine\n\nREPLY:\nok")) is False


def test_a_delegated_task_about_a_handed_case_carries_it(monkeypatch):
    from app import agent, tasks
    _handed_case()
    spawned = {}

    def spawn(title, prompt, kind, workspace=None, teams_chat=""):
        spawned.update(prompt=prompt)
        return {"id": 99}
    monkeypatch.setattr(tasks, "spawn", spawn)
    agent.delegate_task("Find where Pat's date is dropped", "Pat Lee: dig into the date", "analysis")
    assert "TRACKING with no dates" in spawned["prompt"] and "Corrects:" in spawned["prompt"]
