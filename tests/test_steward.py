"""The conversation steward — Round 3, P9.

"A greeting has nothing to check, so it is forwarded as news. Nothing keeps the
conversation open or waits for the real ask."

On Teams people type "Hi", let it send, and type the question ten seconds later.
Asta pushed the "Hi" as something needing attention and then pushed the question
as a second thing: two interruptions for one conversation, the first carrying no
information at all, because the message it was about had not been written yet.
"""

from __future__ import annotations

import time

import pytest

from app import steward, store


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db", raising=False)
    store.init()
    yield


# --- what counts as only an opening -----------------------------------------------------

@pytest.mark.parametrize("said", [
    "Hi", "hi", "Hii", "hey", "Hello", "helo", "yo", "hai", "namaste",
    "Good morning", "good evening", "gm", "Hi Arun", "hi bro", "Hello there",
    "are you there?", "you around?", "quick question", "Hi, are you there?",
])
def test_a_message_that_is_only_an_opening_is_recognised(said):
    assert steward.is_greeting(said)


@pytest.mark.parametrize("said", [
    "Hi, can you check why STF is not done for booking 88271?",
    "morning — the consumer is throwing a null pointer",
    "hey, PR 1409 is up for review",
    "can you check this",
    "the deployment failed",
])
def test_a_message_with_an_ask_in_it_is_not_an_opening(said):
    assert not steward.is_greeting(said)


@pytest.mark.parametrize("said", [
    "Hi, prod is down",
    "hey — urgent, the booking service is failing",
    "good morning, we have a P1",
    "hi, customer escalation on 88271",
])
def test_urgency_outranks_the_greeting_every_time(said):
    """The steward must never be the reason an outage waited for a second
    message. Anything urgent-shaped goes straight through, greeting or not."""
    assert not steward.is_greeting(said)


# --- holding, and letting go -------------------------------------------------------------

def test_a_bare_hello_is_held_rather_than_forwarded():
    out = steward.consider("Alex Kumar", "Hi")
    assert out["hold"] is True
    assert steward.holding()[0]["who"] == "Alex Kumar"


def test_the_ask_that_follows_is_released_carrying_the_greeting():
    """One conversation, one interruption: he should be shown the question,
    with the greeting behind it, not either of them on its own."""
    steward.consider("Alex Kumar", "Hi")
    out = steward.consider("Alex Kumar", "can you check why STF is not done for 88271?")
    assert out["hold"] is False
    assert out["opened_with"] == "Hi"
    assert "88271" in out["text"]
    assert steward.holding() == [], "the hold outlived the conversation"


def test_saying_hello_twice_is_still_one_person_waiting():
    steward.consider("Alex Kumar", "Hi")
    first = steward.holding()[0]["at"]
    steward.consider("Alex Kumar", "hello?")
    rows = steward.holding()
    assert len(rows) == 1 and rows[0]["at"] == first, "the second hello restarted the clock"


def test_a_message_with_the_ask_already_in_it_is_never_held():
    out = steward.consider("Alex Kumar", "Hi, can you check booking 88271?")
    assert out["hold"] is False and steward.holding() == []


def test_two_colleagues_are_held_separately():
    steward.consider("Alex Kumar", "Hi")
    steward.consider("Nakka Harika", "good morning")
    assert {r["who"] for r in steward.holding()} == {"Alex Kumar", "Nakka Harika"}
    steward.consider("Alex Kumar", "the consumer is failing")
    assert [r["who"] for r in steward.holding()] == ["Nakka Harika"]


# --- a hold is not a drop ----------------------------------------------------------------

def test_a_greeting_that_goes_nowhere_expires_into_the_digest(monkeypatch):
    """Holding it forever is its own failure — he never learns somebody tried
    to reach him. It goes in the quiet channel, not as a push an hour late."""
    steward.consider("Alex Kumar", "Hi")
    assert steward.expired() == []
    monkeypatch.setattr(steward, "WAIT_SECONDS", -1.0)
    gone = steward.expired()
    assert [r["who"] for r in gone] == ["Alex Kumar"]
    assert "Alex Kumar" in steward.line_for(gone)
    assert steward.expired() == [], "mentioned twice"


def test_an_expired_hold_does_not_swallow_the_ask_that_finally_comes(monkeypatch):
    monkeypatch.setattr(steward, "WAIT_SECONDS", -1.0)
    steward.consider("Alex Kumar", "Hi")
    out = steward.consider("Alex Kumar", "sorry — can you check 88271?")
    assert out["hold"] is False and "88271" in out["text"]


def test_turning_it_off_forwards_everything_as_before(monkeypatch):
    monkeypatch.setenv("ASTA_STEWARD", "0")
    assert steward.consider("Alex Kumar", "Hi")["hold"] is False
