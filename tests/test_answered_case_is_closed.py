"""A case he answered himself is over — and stays over.

Synthetic replay of a live failure. A colleague asked a question in a 1:1; Asta
read it seconds before his own reply, so it stayed "waiting" on his list. The
background answer later found he had replied and sent nothing — but marked the
case "answer waiting for Arun's approval". The next chat turn was told so, read
"share the module to <colleague>" as "send the finding", and sent it at once.
His correction arrived before the queued send went out, and it went anyway.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app import agent, answers, attention, chat_watch, loop, store, threads
from app import teams_bridge as tb

WHO = "Marlowe Reyes"


@pytest.fixture
def told(monkeypatch):
    pushed: list[str] = []

    async def notify(msg, kind="", **k):
        pushed.append(msg)

    from app import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", notify)
    return pushed


def _ask(now, text="node issue?"):
    store.attention_upsert(f"{WHO.lower()} {text}", "teams-chat", who=WHO,
                           what=f"{WHO}: {text}", now=now)
    return f"{WHO.lower()} {text}"


# --- 1. the finished answer closes the case ------------------------------------

def test_answer_he_already_gave_closes_the_case_and_clears_his_list(monkeypatch, told):
    now = time.time()
    key = _ask(now - 300)
    store.kv_set("wa_conversation", _conv())
    t = threads.open("teams", WHO, chat=WHO, now=now - 300)
    store.save_teams_messages([{"key": "m1", "chat": WHO, "sender": WHO,
                                "text": "node issue?", "sent_at": now - 300, "stamp": ""}])

    async def read(c, limit=0, max_scrolls=0, since=None):
        return [{"sender": "Arunkumar K", "text": "scheduler issue", "sent_at": now - 290}]

    monkeypatch.setattr(tb, "enabled", lambda: True)
    monkeypatch.setattr(tb, "read_history", read)
    assert asyncio.run(answers.present(who=WHO, need="check the node issue", chat=WHO,
                                       group=False, analysis="Not a node issue",
                                       reply="not a node issue", thread=t["id"],
                                       source_text="node issue?")) is True
    assert not loop.awaiting(answers.phone_conversation()), "nothing staged"
    assert told and "already answered" in told[0]
    assert threads.get(t["id"])["status"] == "answered"
    assert "waiting for Arun's approval" not in threads.context_for(WHO, now=now)
    assert store.attention_get(key)["state"] == "acted", "off his list"


# --- 2. his reply, captured on any read, settles what came before it ----------

def test_his_reply_read_after_their_ask_settles_it():
    now = time.time()
    key = _ask(now - 60)
    tb._capture(WHO, [{"sender": "Arunkumar K", "text": "scheduler issue",
                       "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 55)) + "Z"}])
    assert store.attention_get(key)["state"] == "acted"


def test_an_ask_after_his_reply_is_still_owed():
    now = time.time()
    key = _ask(now - 10, "and the logs?")
    tb._capture(WHO, [{"sender": "Arunkumar K", "text": "scheduler issue",
                       "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 60)) + "Z"}])
    assert store.attention_get(key)["state"] in ("new", "notified")


def test_their_own_message_settles_nothing():
    now = time.time()
    key = _ask(now - 60)
    tb._capture(WHO, [{"sender": WHO, "text": "any update?",
                       "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 5)) + "Z"}])
    assert store.attention_get(key)["state"] in ("new", "notified")


def test_settle_with_before_keeps_later_asks():
    now = time.time()
    early, late = _ask(now - 100, "one"), _ask(now - 10, "two")
    assert attention.settle_with(WHO, before=now - 50) == 1
    assert store.attention_get(early)["state"] == "acted"
    assert store.attention_get(late)["state"] in ("new", "notified")


# --- 3. a send he asked for is held when he writes again first ----------------

def _conv():
    c = store.create_conversation(model="claude_cli", workspace=None)
    return c["id"]


def _fake_send(monkeypatch, cid, he_writes: bool):
    sent: list[str] = []

    async def send_message(chat, text, allow_group=False):
        if he_writes:
            store.add_ui_message(cid, "user", "no, the other file")
        gate = tb.send_gate.get()
        why = gate() if gate else ""
        if why:
            raise tb.SendHeld(why)
        sent.append(text)
        return chat

    monkeypatch.setattr(tb, "send_message", send_message)
    return sent


def test_a_brain_worded_send_is_held_when_he_writes_before_it_goes(monkeypatch, told):
    cid = _conv()
    sent = _fake_send(monkeypatch, cid, he_writes=True)
    asyncio.run(agent._send_as_asked(cid, WHO, "root cause: the scheduler", hold_if_he_writes=True))
    assert sent == []
    assert told and "Held" in told[0]
    queued = answers._load_queue()
    assert queued and queued[-1]["to"] == WHO and queued[-1]["what"] == "root cause: the scheduler"


def test_a_send_goes_when_he_says_nothing_more(monkeypatch, told):
    cid = _conv()
    sent = _fake_send(monkeypatch, cid, he_writes=False)
    asyncio.run(agent._send_as_asked(cid, WHO, "root cause: the scheduler", hold_if_he_writes=True))
    assert sent == ["root cause: the scheduler"]


def test_his_own_words_or_an_approved_draft_are_never_held(monkeypatch, told):
    cid = _conv()
    sent = _fake_send(monkeypatch, cid, he_writes=True)
    asyncio.run(agent._send_as_asked(cid, WHO, "on it, will update"))
    assert sent == ["on it, will update"]
    assert tb.send_gate.get() is None, "the gate never leaks past its send"
