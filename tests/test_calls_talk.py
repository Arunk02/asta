"""A call Asta places for him is a conversation. 8 Oct, Swamy.

Swamy asked to connect about booking PR 1470. "Connect with swamy … and clarify"
came back as a re-worded text; the old "let's connect at 3:30" draft was shown
again after his own reply had gone; "Go ahead and connect" staged the call back
to him; and when it finally rang, `teams_call` only dialled — Swamy said hello
into silence and hung up after 40 s.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app import agent as agent_mod
from app import answers, capabilities, consent, conversation, main, ops, store, threads


@pytest.fixture
def talks(monkeypatch):
    """converse() as it is called, without a browser or a voice."""
    from app import daemon, meetings, notify
    calls, pushed = [], []

    async def fake_converse(who, topic, workspace="", **kw):
        calls.append({"who": who, "topic": topic, **kw})
        return f"Talked to {who} about {topic}"

    async def fake_notify(text, *a, **k):
        pushed.append(text)
        return {}

    monkeypatch.setattr(conversation, "converse", fake_converse)
    monkeypatch.setattr(notify, "notify", fake_notify)
    monkeypatch.setattr(meetings, "call_person",
                        lambda *a, **k: pytest.fail("a person was rung without anyone to talk"))
    monkeypatch.setattr(daemon, "once", lambda name, coro: asyncio.get_event_loop().create_task(coro))
    return calls, pushed


def test_a_call_to_one_person_is_a_conversation_not_a_silent_line(talks):
    calls, pushed = talks

    async def go():
        out = await ops._teams_call(who="Yelugubanti Ayyappa Swamy", topic="booking PR 1470",
                                    agenda="explain the JOB_OPENED work process")
        await asyncio.sleep(0.05)
        return out
    out = asyncio.run(go())
    assert "talk it through" in out
    assert calls == [{"who": "Yelugubanti Ayyappa Swamy", "topic": "booking PR 1470",
                      "agenda": "explain the JOB_OPENED work process"}]
    assert pushed and pushed[0].startswith("📞 Talked to")


def test_a_call_without_a_topic_takes_it_from_the_conversation(talks):
    calls, _ = talks
    t = threads.open("teams", "Yelugubanti Ayyappa Swamy")
    threads.update(t["id"], summary="Arun pointed Swamy to PR 1470 (Job Opened WorkProcesses "
                                    "for Soft Closure); Swamy agreed to connect now.")

    async def go():
        await ops._teams_call(who="Yelugubanti Ayyappa Swamy")
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert calls[0]["topic"] == "PR 1470"
    assert conversation.topic_for("Nobody Known") == "your message to Arun"


def test_go_ahead_and_connect_dials_with_his_words_as_the_agenda(monkeypatch):
    from app import offers, teams_bridge
    monkeypatch.setattr(teams_bridge, "enabled", lambda: True)
    ran = {}

    async def fake_run(spec):
        ran.update(spec)
        return "📞 Calling"
    monkeypatch.setattr(ops, "run", fake_run)
    monkeypatch.setattr(offers, "staged_write",
                        lambda *a, **k: pytest.fail("staged a call he asked for"))
    said = "Confirm call swamy and help him the issues which he asked"
    capabilities.TURN_TEXT.set(said)
    try:
        asyncio.run(agent_mod.teams_call("Yelugubanti Ayyappa Swamy"))
    finally:
        capabilities.TURN_TEXT.set("")
    assert ran["args"]["agenda"] == said
    assert ran["args"]["who"] == "Yelugubanti Ayyappa Swamy"


@pytest.mark.parametrize("text", [
    "Go ahead and connect",
    "Connect with swamy if he wants to connect now and inform and clarify the job opened",
    "connect now",
    "hop on a call with vinish",
])
def test_connect_is_how_he_says_call(text):
    assert consent.asked_to_call(text)


@pytest.mark.parametrize("text", [
    "connect to the DB and check the row",
    "it can't connect to kafka",
    "don't connect with him",
    "why does the pod not connect",
    "Call and connect with",
])
def test_connect_that_is_not_a_phone_call(text):
    assert not consent.asked_to_call(text)


@pytest.mark.parametrize("text, moves_on", [
    ("Connect with swamy and clarify the job opened work processes", True),
    ("What swamy told , asked u to connect right ..?", True),
    ("Did you connect and explain to him?", True),
    ("make it shorter", False),
    ("what if we add the PR link?", False),
    ("mention the milestone too", False),
])
def test_a_call_or_a_question_is_not_an_edit_to_the_draft(text, moves_on):
    assert main._moves_on_from_draft(text) is moves_on


def test_asking_whether_to_send_is_not_a_claim_that_it_was_sent():
    since = time.time()
    assert main.unproven_send("Just to confirm — do you want that message sent to "
                              "Swamy now, or something else?", since) == ""
    assert main.unproven_send("Sent to Swamy now.", since)


def _phone():
    conv = store.create_conversation(model="claude_cli", workspace=None)
    store.kv_set("wa_conversation", conv["id"])
    return conv


def test_a_queued_draft_he_has_answered_meanwhile_is_not_shown(monkeypatch):
    """11:37:05 his "sure connect now" reached Swamy; 11:37:07 the queued
    "let's connect at 3:30" draft was put in front of him again."""
    from app import chat_watch, loop, notify, teams_bridge
    conv = _phone()
    chat = "Yelugubanti Ayyappa Swamy"
    asked = time.time() - 240
    store.save_teams_messages([{"key": "k1", "chat": chat, "sender": chat,
                                "text": "or can we connect now", "sent_at": asked}])
    item = {"type": "answer", "kind": "send", "who": chat, "to": chat, "to_group": False,
            "what": "Works for me, let's connect at 3:30.", "need": "call", "task_id": 266,
            "_at": asked + 5}
    store.kv_set(answers._QUEUE, json.dumps([item]))
    monkeypatch.setattr(teams_bridge, "SENT", [(time.time() - 2, chat)])

    async def quiet(*a, **k):
        return None

    async def no_look(*a, **k):
        return False
    monkeypatch.setattr(notify, "notify", quiet)
    monkeypatch.setattr(chat_watch, "he_replied_since", no_look)
    assert asyncio.run(answers.next_after(conv["id"])) is False
    assert loop.awaiting(conv["id"]) is None
    assert not answers._load_queue()


def test_a_queued_draft_still_waiting_on_him_is_shown(monkeypatch):
    from app import chat_watch, loop, notify, teams_bridge
    conv = _phone()
    chat = "Yelugubanti Ayyappa Swamy"
    store.save_teams_messages([{"key": "k2", "chat": chat, "sender": chat,
                                "text": "or can we connect now", "sent_at": time.time() - 60}])
    item = {"type": "answer", "kind": "send", "who": chat, "to": chat, "to_group": False,
            "what": "Sure, connecting now.", "need": "call", "task_id": 266,
            "_at": time.time()}
    store.kv_set(answers._QUEUE, json.dumps([item]))
    monkeypatch.setattr(teams_bridge, "SENT", [])

    async def quiet(*a, **k):
        return None

    async def no_look(*a, **k):
        return False
    monkeypatch.setattr(notify, "notify", quiet)
    monkeypatch.setattr(chat_watch, "he_replied_since", no_look)
    assert asyncio.run(answers.next_after(conv["id"])) is True
    assert loop.awaiting(conv["id"])["to"] == chat


def test_the_call_knows_what_asta_already_found_for_them():
    chat = "Yelugubanti Ayyappa Swamy"
    t = store.create_task("Swamy asks about PR 1470", "investigate", "p", "booking")
    store.update_task(t["id"], teams_chat=chat, status="done",
                      result="ANALYSIS: PR 1470 adds JOB_OPENED to WorkProcessNameEnum because "
                             "AP PR 1260 now sends it on job reopen.\n\nREPLY: sure")
    brief = conversation.with_history(chat, "explain PR 1470")
    assert "AP PR 1260 now sends it on job reopen" in brief
