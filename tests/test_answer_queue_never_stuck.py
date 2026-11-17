"""A draft of his own, left unanswered, must not hold colleague answers forever.

13 Nov: a send he had staged in his phone conversation (it carries "kind", not
"type") was never answered, and every colleague answer queued behind it for days
— nothing reached him and nothing said so."""
from __future__ import annotations

import asyncio
import json
import time

from app import answers, health, loop, store


def _phone():
    conv = store.create_conversation(model="claude_cli", workspace=None)
    store.kv_set("wa_conversation", conv["id"])
    return conv["id"]


def _his_draft(cid, age):
    loop.stage(cid, {"kind": "send", "what": "see attached", "to": "Test Person",
                     "channel": "teams", "_at": time.time() - age})


def _queued(age=60):
    item = {"type": "answer", "kind": "send", "who": "Colleague One", "to": "Colleague One",
            "to_group": False, "what": "Yes, it is deployed to SIT.", "need": "deploy?",
            "task_id": 7, "_at": time.time() - age}
    store.kv_set(answers._QUEUE, json.dumps([item]))


def _no_push(monkeypatch):
    from app import chat_watch, notify, teams_bridge
    told = []

    async def push(text, *a, **k):
        told.append(text)

    async def no_look(*a, **k):
        return False
    monkeypatch.setattr(notify, "notify", push)
    monkeypatch.setattr(chat_watch, "he_replied_since", no_look)
    monkeypatch.setattr(teams_bridge, "SENT", [])
    monkeypatch.setattr(answers, "origin_allowed", lambda it: True)
    return told


def test_a_fresh_draft_of_his_still_holds_the_line(monkeypatch):
    cid = _phone()
    _no_push(monkeypatch)
    _his_draft(cid, age=60)
    _queued()
    assert asyncio.run(answers.next_after(cid)) is False
    assert loop.awaiting(cid)["what"] == "see attached"


def test_an_old_unanswered_draft_of_his_makes_room_for_a_waiting_answer(monkeypatch):
    cid = _phone()
    _no_push(monkeypatch)
    _his_draft(cid, age=answers.PARK_SECONDS + 60)
    _queued()
    assert asyncio.run(answers.next_after(cid)) is True
    assert loop.awaiting(cid)["to"] == "Colleague One"
    said = [m["content"] for m in store.list_ui_messages(cid)]
    assert any("NOT sent" in s and "see attached" in s for s in said)


def test_an_old_draft_with_nothing_waiting_stays_where_he_left_it(monkeypatch):
    cid = _phone()
    _no_push(monkeypatch)
    _his_draft(cid, age=answers.PARK_SECONDS + 60)
    store.kv_set(answers._QUEUE, "[]")
    assert asyncio.run(answers.next_after(cid)) is False
    assert loop.awaiting(cid)["what"] == "see attached"


def test_a_queue_that_does_not_move_is_a_health_problem(monkeypatch):
    _phone()
    _queued()
    now = time.time()
    q = answers._load_queue()
    q[0]["_queued"] = now - answers.STUCK_SECONDS - 60
    store.kv_set(answers._QUEUE, json.dumps(q))
    store.kv_set("answers_last_shown", str(now - answers.STUCK_SECONDS - 60))
    assert "not draining" in answers.stuck(now)
    store.kv_set("answers_last_shown", str(now - 60))
    assert answers.stuck(now) == ""
    store.kv_set(answers._QUEUE, "[]")
    assert answers.stuck(now) == ""
    assert hasattr(health, "checks")
