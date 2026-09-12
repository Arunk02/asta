"""When somebody hands over the thing to look at, go and look at it.

A colleague sent a booking link and then, in the next message, "can you
check why STF is not done?". Asta forwarded both to Arun's phone and asked whether
it should investigate. His words: *"they gave tickets and details to check but
it is not automatically going and debugging this the proactiveness what i
wanted"*.

Two separate reasons it did not, and each on its own was enough:

* `familiar()` asks "has Asta worked on this before?" — the right question for a
  vague remark, the wrong one when the exact thing to look at has just been
  pasted. A booking id from a colleague is new ground by definition.
* The responder is handed ONE message. The link was in the message above the
  question, so even a perfect check had nothing to check.
"""

from __future__ import annotations

import time

import pytest

from app import responder, store


_LINK = ("Maersk :: Inland Booking tools.maersk-digital.net: "
         "inland/booking/ZZ1TESTBK9Q2")
_ASK = "Can you check why STF is not done for this one?"


# --- what counts as a handle -------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    (_LINK, "tools.maersk-digital.net: inland/booking/ZZ1TESTBK9Q2"),
    ("Incident INC9644343 assigned to OH - TELIKOS", "INC9644343"),
    ("https://github.com/Maersk-Global/telikos-email-service/pull/665",
     "https://github.com/Maersk-Global/telikos-email-service/pull/665"),
    ("can you look at BEPTELIKOS-10247", "BEPTELIKOS-10247"),
])
def test_a_handed_over_handle_is_recognised(text, expected):
    assert responder.handed_over(text) == expected


@pytest.mark.parametrize("text", [
    "shall we join here now?", "It went i think", _ASK,
    "shared grafana.maersk.io: explore",          # a host with no resource
    "sorry i was in a call", "thanks!",
])
def test_ordinary_chat_is_not_a_handle(text):
    """Narrow on purpose. A bare alphanumeric blob would fire on half of chat,
    and every false positive spends a turn on nothing."""
    assert responder.handed_over(text) == ""


# --- and what it changes -----------------------------------------------------

@pytest.fixture
def spawned(monkeypatch):
    """Records what the responder decided to do."""
    seen: dict = {}

    def fake_spawn(title, brief, kind, workspace=None):
        seen.update(title=title, brief=brief, kind=kind)
        return {"id": 999}
    from app import tasks
    monkeypatch.setattr(tasks, "spawn", fake_spawn)

    def fake_propose(**kw):
        seen["asked_first"] = kw.get("question", "?")
    from app import offers
    monkeypatch.setattr(offers, "propose", fake_propose)
    # Removes the rate limit and the age window, and NOTHING else — stubbing the
    # whole guard would delete the "is there a question here at all" check that
    # one of these tests exists to prove.
    monkeypatch.setattr(
        responder, "should_respond",
        lambda kind, *a, **k: "" if kind else "nothing checkable in it")
    return seen


def test_it_investigates_instead_of_asking_permission(spawned):
    """The whole complaint. The handle IS the permission — somebody saying
    "here, this one" — and the work it unlocks is read-only."""
    task = responder.respond("teams-chat", "Vinish Kumar", _ASK, context=_LINK)

    assert task is not None, "it asked instead of looking"
    assert "asked_first" not in spawned
    assert spawned["kind"] == "analysis", "investigation is read-only, never code"


def test_the_handle_reaches_the_worker(spawned):
    """A brief that says "check why STF not done" without the booking id sends a
    worker to look for something it cannot name."""
    responder.respond("teams-chat", "Vinish Kumar", _ASK, context=_LINK)
    assert "ZZ1TESTBK9Q2" in spawned["brief"]


def test_without_the_context_it_still_has_nothing_to_go_on(spawned):
    """Pins the second half: the fix is not the regex alone. Same message, no
    surrounding lines, and there is genuinely nothing to check."""
    responder.respond("teams-chat", "Vinish Kumar", _ASK)
    assert "asked_first" in spawned


def test_a_vague_ask_with_no_handle_still_asks_first(spawned):
    """The offer path is right when there is nothing concrete — this narrows it,
    it does not remove it."""
    responder.respond("teams-chat", "Vinish Kumar",
                      "can you check why that thing is slow?",
                      context="morning\nthanks for yesterday")
    assert "asked_first" in spawned


def test_the_reason_says_who_handed_what_over():
    """It shows up on his phone, so it has to read like an explanation."""
    known, why = responder.familiar(_LINK)
    assert not known                                  # never worked on before
    assert responder.handed_over(f"{_LINK}\n{_ASK}")


def test_context_cannot_invent_a_question_nobody_asked(spawned):
    """The ASK is judged on the message itself. Otherwise a stray link in the
    scrollback turns every "ok" into an investigation."""
    responder.respond("teams-chat", "Vinish Kumar", "ok cool", context=_LINK)
    assert spawned == {}


def test_the_chat_sweep_actually_passes_the_previous_lines():
    """A context parameter nothing fills is a parameter that does nothing."""
    import inspect
    from app import chat_watch
    src = inspect.getsource(chat_watch)
    assert "context=before" in src


# --- and it has somewhere to hand the answer back ----------------------------

def test_a_background_investigation_can_stage_its_reply():
    """Task #96 read prod, found why STF never ran, wrote the reply to Vinish —
    and then said "Teams send tool isn't available in this environment, so
    please send manually". The work done and nobody told.

    `prepare_to_send` needs a conversation to stage the approval INTO, and
    `conversation_of` returned "" for anything a background loop spawned, which
    is every investigation the responder starts."""
    from app import store, tasks
    conv = store.create_conversation("claude", None)      # his phone thread
    t = store.create_task("spawned by a loop", "analysis", "look into it", None)
    assert tasks.conversation_of(t["id"]) == conv["id"], \
        "a task nothing linked has nowhere to hand its answer back"


def test_with_no_conversation_at_all_it_still_refuses_rather_than_guesses():
    """The fallback is "his most recent thread", not "invent one" — a fresh
    install has nowhere to stage into and must say so."""
    from app import store, tasks
    for row in store.list_conversations(limit=50) or []:
        store.delete_conversation(row["id"])
    t = store.create_task("orphan", "analysis", "x", None)
    assert tasks.conversation_of(t["id"]) == ""


def test_an_explicit_link_still_wins():
    """The fallback must never override a task that knows its own chat — that is
    how an approval lands in the wrong place."""
    from app import store, tasks
    t = store.create_task("from a chat", "analysis", "x", None)
    tasks.link_task("conv-abc", t["id"])
    assert tasks.conversation_of(t["id"]) == "conv-abc"


def test_the_fallback_only_picks_where_he_SEES_it():
    """Guessing a Teams recipient would be unsafe; guessing the approval surface
    is not. Pinned so the distinction survives a refactor."""
    import inspect
    from app import tasks
    src = inspect.getsource(tasks.conversation_of)
    assert "recipient" in src.lower()
