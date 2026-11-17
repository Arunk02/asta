"""While he is in the conversation himself, a new ask waits for HIS reply
before a run is spent on it.

3–17 Nov: 28 investigations were answered by him before Asta's answer arrived
(median 4.9 min after the ask), 18 of them while he was already writing in that
chat — each run spent for nothing."""
import time

from app import chat_watch


def _conv(sent_ago: float, his_minutes_ago=None, mine=False):
    now = time.time()
    return {"chat": "Colleague One", "handled_by_him": mine, "sent_at": now - sent_ago,
            "arun_minutes_ago": his_minutes_ago, "open_need": "", "last": "is SIT updated?",
            "raw": [{"sender": "Colleague One", "text": "is SIT updated with the fix?",
                     "sent_at": now - sent_ago}]}


def test_an_ask_waits_while_he_is_in_the_exchange():
    assert chat_watch.his_turn_first(_conv(90, his_minutes_ago=2), time.time())


def test_it_is_worked_once_he_has_not_answered_in_time():
    late = chat_watch.HE_HOLD_SECONDS + 30
    assert not chat_watch.his_turn_first(_conv(late, his_minutes_ago=2), time.time())


def test_no_wait_when_he_is_not_in_that_chat():
    assert not chat_watch.his_turn_first(_conv(90, his_minutes_ago=None), time.time())
    assert not chat_watch.his_turn_first(_conv(90, his_minutes_ago=45), time.time())


def test_his_reply_settles_it():
    assert not chat_watch.his_turn_first(_conv(90, his_minutes_ago=0, mine=True), time.time())


def test_the_hold_falls_due_five_minutes_after_their_message():
    c = _conv(60, his_minutes_ago=1)
    chat_watch.hold_for_him(c)
    due_in = chat_watch._next_held(time.time())
    assert chat_watch.HE_HOLD_SECONDS - 70 <= due_in <= chat_watch.HE_HOLD_SECONDS - 50
    assert chat_watch.held(c["chat"])["why"] == chat_watch.HE_IS_IN_IT

