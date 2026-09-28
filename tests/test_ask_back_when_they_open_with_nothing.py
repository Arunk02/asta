"""Someone says "need ur help". Ask them what, instead of forwarding the words.

28 Sep, 16:46. Navya messaged "Hi Arunkumar, need ur help". Asta pushed it to
his phone as a red interruption carrying no information, because the message
that explains it had not been written yet. The steward exists precisely to stop
that ("a greeting is the start of something, not news") and it missed, because
its vocabulary knew "are you there" and "quick question" but not "need your
help".

Then he had to tell Asta to ask her, wait, and approve a nine-word draft.

His decision, in his words:

    once u recieved send short message get wht they want, once you got u have
    to analyse and come back with the actual result and ask me check once
    approve will send, so only final result approval should come to user

So the shape is: ONE clarifying question, sent without asking him — it commits
nothing on his behalf and cannot be taken back in any way that matters — then
the existing read-only investigation, then ONE approval on the real answer.

That first send is the only outward act in Asta that does not wait for his yes,
so every boundary on it is tested here: only a contentless opener, never when
it is urgent, never twice to the same person, and off unless switched on.
"""

from __future__ import annotations

import pytest

from app import steward


# --- what counts as "they have not said anything yet" -------------------------

@pytest.mark.parametrize("said", [
    "Hi Arunkumar, need ur help",          # the real one
    "need ur help",
    "need your help",
    "Hi, need some help",
    "need a favour",
    "can you help me",
    "could you help",
    "got a minute?",
    "you free?",
    "hi",
    "are you there",
    "quick question",
])
def test_an_opener_with_no_ask_in_it_is_held(said):
    assert steward.is_greeting(said), (
        f"{said!r} says somebody wants something, not what — forwarding those "
        "words is an interruption carrying no information")


@pytest.mark.parametrize("said", [
    "need ur help with booking 88271",
    "can you help me with the UAT release?",
    "Hi Arunkumar, can you check the TMS consumer?",
    "need a favour - can you review my PR today",
    "quick question: which env is the cert for?",
])
def test_an_opener_that_already_carries_the_ask_is_never_held(said):
    assert not steward.is_greeting(said), (
        "holding this would delay the very thing the steward exists to deliver")


@pytest.mark.parametrize("said", [
    "need ur help urgent",
    "need your help, prod is down",
    "can you help me, customer escalation",
])
def test_urgency_outranks_the_opener(said):
    assert not steward.is_greeting(said)


# --- the one send that does not wait for him ----------------------------------

def test_it_is_off_unless_switched_on(monkeypatch):
    monkeypatch.delenv("ASTA_ASK_BACK", raising=False)
    assert not steward.ask_back_enabled()


def test_switched_on_a_held_opener_gets_one_short_question(monkeypatch):
    monkeypatch.setenv("ASTA_ASK_BACK", "1")
    line = steward.ask_back_line("Navya R", "Hi Arunkumar, need ur help")
    assert line, "a held opener with the flag on should produce the question"
    assert len(line) < 120, "one short line, not a paragraph"
    assert "?" in line


def test_the_question_never_commits_anything_on_his_behalf(monkeypatch):
    """The whole justification for sending this without asking him is that it
    promises nothing. The moment it says "I'll look at it" or "sure, will do",
    it is a commitment he never approved."""
    monkeypatch.setenv("ASTA_ASK_BACK", "1")
    line = steward.ask_back_line("Navya R", "need ur help").lower()
    for promise in ("i'll ", "i will", "will do", "on it", "happy to"):
        assert promise not in line, f"{promise!r} is a commitment, not a question"


def test_the_question_is_courteous(monkeypatch):
    """It goes out in HIS name, to a colleague who has just asked him for
    something. His rule: "always use bit polite tone, not rude". Short is the
    house style; short arriving as curt is not.

    "What do you need?" is the shortest correct sentence here and reads like a
    ticket form, which is exactly the failure this guards.
    """
    monkeypatch.setenv("ASTA_ASK_BACK", "1")
    line = steward.ask_back_line("Navya R", "need ur help")
    assert any(soft in line.lower() for soft in
               ("could you", "would you", "please", "may i", "can you")), (
        f"{line!r} asks, but not politely")


def test_nobody_is_asked_back_twice(monkeypatch):
    """They said hi, Asta asked what they needed, and they have not replied yet.
    Asking again is not attentiveness."""
    monkeypatch.setenv("ASTA_ASK_BACK", "1")
    steward.consider("Navya R", "need ur help")
    steward.note_asked_back("Navya R")
    assert steward.ask_back_line("Navya R", "still there?") == ""


def test_an_ask_that_is_not_an_opener_is_never_asked_back(monkeypatch):
    """They already said what they want. Replying "what do you need?" to that is
    worse than saying nothing."""
    monkeypatch.setenv("ASTA_ASK_BACK", "1")
    assert steward.ask_back_line("Navya R", "can you check booking 88271?") == ""


def test_the_ask_back_is_recorded(monkeypatch):
    """Otherwise the one unapproved outward act in Asta is also the invisible one."""
    from app import store
    monkeypatch.setenv("ASTA_ASK_BACK", "1")
    steward.note_asked_back("Navya R")
    kinds = [(o["kind"], o["outcome"]) for o in store.recent_outcomes(20)]
    assert ("steward", "asked back") in kinds
