"""A push that could not land is sent when WhatsApp is back — once, together."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app import notify, store, telegram


@pytest.fixture
def phone(monkeypatch):
    state = {"up": False, "sent": []}

    async def wa(text, done=False):
        if state["up"]:
            state["sent"].append(text)
        return state["up"]

    async def tg(text):
        return False

    monkeypatch.setattr(notify, "wa_send", wa)
    monkeypatch.setattr(telegram, "send", tg)
    monkeypatch.setattr(notify, "quiet_rule", lambda now=None: None)
    return state


def missed():
    return json.loads(store.kv_get(notify.MISSED_KEY) or "[]")


def test_a_failed_push_waits_and_goes_out_once_whatsapp_is_back(phone):
    asyncio.run(notify.deliver("❌ Task #7 failed — build broke"))
    asyncio.run(notify.deliver("❌ Task #7 failed — build broke"))       # same alert again
    asyncio.run(notify.deliver("📞 Alex didn't pick up"))
    assert [m["text"] for m in missed()] == ["❌ Task #7 failed — build broke", "📞 Alex didn't pick up"]
    assert asyncio.run(notify.resend_missed()) is False and missed(), "still down: kept"
    phone["up"] = True
    assert asyncio.run(notify.resend_missed()) is True
    body, = phone["sent"]
    assert "didn't reach your phone" in body and "Task #7 failed" in body and "Alex didn't pick up" in body
    assert missed() == [] and asyncio.run(notify.resend_missed()) is False


def test_a_push_that_landed_is_never_queued(phone):
    phone["up"] = True
    asyncio.run(notify.deliver("✅ merged"))
    assert missed() == []


def test_the_held_digest_keeps_its_own_items_and_is_not_queued_twice(phone, monkeypatch):
    store.kv_set(notify.HELD_KEY, json.dumps([{"text": "CI green on PR 12", "at": time.time()}]))
    asyncio.run(notify.flush_held())
    assert missed() == [] and json.loads(store.kv_get(notify.HELD_KEY))


def test_quiet_hours_hold_the_resend_too(phone, monkeypatch):
    asyncio.run(notify.deliver("❌ Task #7 failed"))
    phone["up"] = True
    monkeypatch.setattr(notify, "quiet_rule", lambda now=None: object())
    assert asyncio.run(notify.resend_missed()) is False and missed()


def test_old_misses_are_counted_not_replayed(phone):
    now = time.time()
    notify._queue_missed("yesterday's thing", now=now - notify.MISSED_FRESH_SECONDS - 60)
    notify._queue_missed("this morning's thing", now=now - 600)
    phone["up"] = True
    asyncio.run(notify.resend_missed(now=now))
    body, = phone["sent"]
    assert "this morning's thing" in body and "yesterday's thing" not in body
    assert "1 older one in the app" in body
