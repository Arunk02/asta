"""22 Oct, Vinish: "…why is it failing only for this one?" and, 9 s later, "Is
this specific to a multi-stop case or something?" — one question in two
messages. Asta read the first alone (and answered it with an old finding), then
started a task for the second. It now waits until they have stopped writing."""

import time

import pytest

from app import chat_watch


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(chat_watch, "QUIET_SECONDS", 40.0)


def _conv(sent_ago: float, mine=False):
    now = time.time()
    return {"chat": "Vinish Kumar", "handled_by_him": mine, "sent_at": now - sent_ago,
            "raw": [{"sender": "Vinish Kumar", "text": "Is this specific to a multi-stop case?",
                     "sent_at": now - sent_ago}],
            "open_need": ""}


def test_a_message_seconds_old_waits_for_the_rest():
    assert chat_watch._wait_reason(_conv(9), time.time()) == chat_watch.STILL_WRITING


def test_once_they_are_quiet_it_is_read():
    assert chat_watch._wait_reason(_conv(45), time.time()) == ""


def test_his_own_reply_never_waits():
    assert chat_watch._wait_reason(_conv(5, mine=True), time.time()) == ""


def test_the_hold_falls_due_just_after_their_last_message():
    now = time.time()
    c = _conv(9)
    since = float(c["sent_at"]) - (chat_watch.HOLD_SECONDS - chat_watch.QUIET_SECONDS)
    chat_watch.hold(c["chat"], c["raw"], chat_watch.STILL_WRITING, since)
    due_in = chat_watch._next_held(now)
    assert 25 <= due_in <= 35                       # 40 s after the message, 9 s already gone


def test_someone_typing_on_and_on_is_read_after_three_minutes(monkeypatch):
    c = _conv(5)
    chat_watch.hold(c["chat"], c["raw"], chat_watch.STILL_WRITING, time.time())
    allh = chat_watch._held_all()
    allh[c["chat"]]["first"] = time.time() - chat_watch.MAX_HOLD_SECONDS - 1
    from app import store
    import json
    store.kv_set(chat_watch._HELD_KEY, json.dumps(allh))
    assert chat_watch._wait_reason(c, time.time()) == ""
