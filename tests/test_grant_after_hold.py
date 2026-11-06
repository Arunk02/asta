"""A permission he gives after "hold all sends" is that hold's one exception (6 Nov).

Synthetic replay: sends had been on hold for weeks ("send nothing without my
yes"). Today he said "whatever Pat asks, respond without my approval, just for
today". The permission was recorded — and every reply to her still waited,
because the hold outranked it. A newer, narrower instruction from him wins for
that one person; everyone else stays held. Names are made up.
"""
from __future__ import annotations

import time

from app import authority, steward, store

CHAT = "Lee Pat"


def _known():
    store.save_teams_messages([
        {"key": "a", "chat": CHAT, "sender": CHAT, "text": "hi", "sent_at": time.time() - 60, "stamp": ""},
        {"key": "b", "chat": "Sam Roe", "sender": "Sam Roe", "text": "hi", "sent_at": time.time() - 60, "stamp": ""}])


def test_a_grant_after_the_hold_lets_her_replies_go_and_only_hers():
    _known()
    steward.hold_sends(True)
    authority.standing_from_words("whatever Pat asks respond without my approval just for today")
    assert authority.auto_reply_to(CHAT)
    assert not steward.held_for(CHAT)
    assert steward.held_for("Sam Roe"), "everyone else is still on hold"
    assert steward.sends_held(), "the hold itself stays on"


def test_a_grant_from_before_the_hold_stays_held():
    _known()
    g = authority.standing_from_words("respond to Pat directly without my approval for today")
    with store._connect() as c:
        c.execute("UPDATE authority SET created_at=? WHERE id=?", (time.time() - 3600, g.id))
    steward.hold_sends(True)
    assert steward.held_for(CHAT) and not authority.auto_reply_to(CHAT)


def test_an_expired_exception_is_held_again():
    _known()
    steward.hold_sends(True)
    g = authority.standing_from_words("respond to Pat directly without my approval for today")
    with store._connect() as c:
        c.execute("UPDATE authority SET expires_at=? WHERE id=?", (time.time() - 1, g.id))
    assert steward.held_for(CHAT)


def test_a_hold_set_before_this_change_is_dated_from_its_record():
    """The live hold predates the timestamp key: its time comes from the ledger."""
    _known()
    store.kv_set(steward.HOLD_KEY, "1")
    store.record_outcome("steward", "sends held")
    time.sleep(0.01)
    authority.standing_from_words("respond to Pat directly without my approval for today")
    assert not steward.held_for(CHAT)


def test_no_hold_means_nothing_changes():
    _known()
    assert not steward.held_for("Sam Roe")
