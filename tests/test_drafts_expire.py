"""Round 5, PR B: a draft is asked about at most twice, never queued twice, and
leaves the line quietly when he does not answer it.

25 Nov, his words: "if we don't ack the msg in some time, remove it from the
queue — it keeps notifying the same draft again and again". That day the same
untasked draft was queued at 13:37 and again at 13:40 and shown both times."""
from __future__ import annotations

import asyncio
import json
import time

from app import answers, digest, loop, store


def _phone():
    conv = store.create_conversation(model="claude_cli", workspace=None)
    store.kv_set("wa_conversation", conv["id"])
    return conv["id"]


def _quiet(monkeypatch):
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


def _draft(**kw):
    item = {"type": "answer", "kind": "send", "who": "Test Colleague", "to": "Test Colleague",
            "to_group": False, "what": "It went to SIT this morning.", "need": "is it deployed?",
            "task_id": None, "_at": time.time()}
    item.update(kw)
    return item


def test_the_same_untasked_ask_is_queued_once(monkeypatch):
    _phone()
    _quiet(monkeypatch)
    store.kv_set(answers._QUEUE, "[]")
    answers._enqueue(_draft())
    answers._enqueue(_draft(what="It went to SIT this morning, build 41."))
    q = answers._load_queue()
    assert len(q) == 1
    assert "build 41" in q[0]["what"]          # the newer wording, in the old place


def test_an_ask_already_in_front_of_him_is_not_queued_again(monkeypatch):
    cid = _phone()
    told = _quiet(monkeypatch)
    store.kv_set(answers._QUEUE, "[]")
    assert asyncio.run(answers._show(cid, _draft())) is True
    answers._enqueue(_draft())
    assert answers._load_queue() == []
    assert len(told) == 1


def test_different_people_or_asks_are_different_drafts():
    a = answers.ask_key(_draft())
    assert a != answers.ask_key(_draft(who="Other Person", to="Other Person"))
    assert a != answers.ask_key(_draft(need="which build?"))
    assert answers.ask_key(_draft(task_id=5)) != answers.ask_key(_draft(task_id=6))


def test_generic_needs_are_told_apart_by_the_draft_text():
    one = _draft(need="wrote to you", what="Checking the logs now.")
    two = _draft(need="wrote to you", what="The PR is merged.")
    assert answers.ask_key(one) != answers.ask_key(two)


def test_shown_at_most_twice_then_expired_quietly(monkeypatch):
    cid = _phone()
    told = _quiet(monkeypatch)
    for _ in range(answers.MAX_SHOWINGS):
        assert asyncio.run(answers._show(cid, _draft())) is True
        loop.clear_awaiting(cid)
    assert asyncio.run(answers._show(cid, _draft())) is False
    assert len(told) == answers.MAX_SHOWINGS
    assert any("expired unanswered" in r["text"] for r in digest.pending())
    assert answers.shelved("Test Colleague")[0]["_reason"] == "expired"


def test_a_lone_draft_left_past_the_ttl_leaves_the_line(monkeypatch):
    cid = _phone()
    told = _quiet(monkeypatch)
    store.kv_set(answers._QUEUE, "[]")
    asyncio.run(answers._show(cid, _draft()))
    # He saw it long ago and never answered; nothing else is waiting.
    long_ago = time.time() - answers.DRAFT_TTL_SECONDS - 60
    store.kv_set(answers._SEEN + answers.ask_key(_draft()), json.dumps({"n": 1, "first": long_ago}))
    staged = loop.awaiting(cid)
    staged["_shown"] = long_ago
    loop.stage(cid, staged)
    assert answers._blocked(cid) is False
    assert loop.awaiting(cid) is None
    assert len(told) == 1                       # no second push about it
    assert answers.shelved("Test Colleague")[0]["_reason"] == "expired"


def test_a_fresh_draft_still_holds_the_line(monkeypatch):
    cid = _phone()
    _quiet(monkeypatch)
    asyncio.run(answers._show(cid, _draft()))
    assert answers._blocked(cid) is True


def test_a_revived_draft_can_be_sent(monkeypatch):
    cid = _phone()
    _quiet(monkeypatch)
    item = _draft()
    answers._expire(item, "unanswered 130 min")
    entry = answers.shelved("Test Colleague")[0]
    back = answers.unshelve(entry)
    assert back["what"] == item["what"]
