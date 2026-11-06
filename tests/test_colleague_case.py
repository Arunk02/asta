"""A colleague's follow-ups, a "for today" rule, and the repo their work is in (6 Nov).

Synthetic replay: Arun reviewed a colleague's branch in a group chat; she asked
follow-up questions 1:1. He said "whatever Pat asks, respond, no approval needed,
just for today" — Asta said "noted" and kept staging. A draft pointed her to a
class in another repo than the one her branch is in; her "where do I add this?"
was offered to him as a code change; a known contact was flagged as unknown.
Names, repos and branches are made up.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app import answers, authority, main, responder, store

CHAT = "Lee Pat"            # Teams lists her surname first; he calls her "Pat"
GROUP = "Sam, Ravi, Lee, +2"


def _said(chat, sender, text, ago):
    now = time.time()
    store.save_teams_messages([{"key": f"k{chat}{ago}{text[:8]}", "chat": chat, "sender": sender,
                                "text": text, "sent_at": now - ago, "stamp": "today"}])


def _known_chats():
    _said(CHAT, CHAT, "hi Arun", 900)
    _said("Sam Roe", "Sam Roe", "ok", 800)


# --- F1: "just for today" -------------------------------------------------------

def test_for_today_without_approval_is_a_grant_that_ends_tonight():
    _known_chats()
    g = authority.standing_from_words(
        "Whatever Pat asks respond don't required me approval, talk with her directly just for today ..")
    assert g is not None and g.target == CHAT and g.act == "send"
    assert authority.auto_reply_to(CHAT), "her replies now go without his yes"
    end = time.localtime(g.expires_at)
    assert (end.tm_hour, end.tm_min) == (23, 59)
    assert not authority.auto_reply_to(CHAT, group=True), "never a group"


def test_an_expired_grant_no_longer_covers_her():
    _known_chats()
    g = authority.standing_from_words("respond to Pat directly without my approval for today")
    with store._connect() as c:
        c.execute("UPDATE authority SET expires_at=? WHERE id=?", (time.time() - 1, g.id))
    assert not authority.auto_reply_to(CHAT)
    assert authority.may("send", CHAT) is None


@pytest.mark.parametrize("words", [
    "what did Pat ask today?",                          # no waiver
    "don't reply to Pat without my approval",           # the opposite
    "respond to Sam and Pat directly for today",        # two people
    "respond to Nobody directly without my approval for today",   # not a contact
])
def test_anything_less_clear_grants_nothing(words):
    _known_chats()
    assert authority.standing_from_words(words) is None
    assert not authority.auto_reply_to(CHAT)


def test_the_dispatcher_records_it_before_any_brain(monkeypatch):
    _known_chats()
    conv = store.create_conversation("claude_cli", None)

    def no_brain(*a, **k):
        pytest.fail("a plain permission needs no brain turn")
    monkeypatch.setattr(main, "_start_turn", no_brain)

    class Sink:
        def __init__(self):
            self.sent = []

        async def send(self, p):
            self.sent.append(p)
    sink = Sink()
    asyncio.run(main._dispatch(conv, "Whatever Pat asks respond, no need of my approval, just for today",
                               sink, "whatsapp", skip_new_gates=True))
    said = " ".join(p.get("text") or "" for p in sink.sent)
    assert "Lee Pat" in said and "23:59" in said and "revoke" in said
    assert authority.auto_reply_to(CHAT)


# --- F2/F3: names and known threads ---------------------------------------------

def test_a_contact_spoken_to_today_is_known():
    for i in range(900):                                # old history fills the first rows
        store.save_teams_messages([{"key": f"old{i}", "chat": f"Old {i % 40}", "sender": "x",
                                    "text": "x", "sent_at": 1000 + i, "stamp": ""}])
    _said(CHAT, CHAT, "hi", 60)
    assert CHAT in main._known_threads()
    assert main._recipient_warning({"to": CHAT, "channel": "teams"}) == ""


def test_her_case_is_found_by_any_of_her_names():
    _said(CHAT, CHAT, "is this condition valid?", 300)
    responder.note_handed(CHAT)
    assert responder.named_handed("what about Pat's question?") == [CHAT]
    assert responder.named_handed("what about Lee's question?") == [CHAT]


# --- F4: the group review and the repo -------------------------------------------

def test_her_case_carries_his_group_review_and_the_repo_her_branch_is_in():
    _said(CHAT, CHAT, "please check branch feature/abc-123, avro changes not pushed", 3000)
    _said(CHAT, CHAT, "Comparing develop...feature/abc-123 · acme/svc-booking "
                      "https://github.com/acme/svc-booking/compare/develop...feature/abc-123", 2900)
    _said(GROUP, "Arunkumar K", "Keep the allocation activity separate and inject it dynamically", 2000)
    _said(GROUP, "Sam Roe", "Pat can you take care of the review comments?", 1900)
    _said("Other Team", "Arunkumar K", "unrelated", 1800)
    block = responder.case_so_far(CHAT, CHAT)
    assert "inject it dynamically" in block and GROUP in block, "his review in the group is part of it"
    assert "unrelated" not in block
    assert "svc-booking" in block and "feature/abc-123" in block
    assert "answer from that repo" in block.lower()


# --- F5: "she" -------------------------------------------------------------------

def test_she_finds_the_one_case_he_is_working_on():
    _said(CHAT, CHAT, "where do I add this?", 300)
    _said(CHAT, "Arunkumar K", "check the old status", 200)
    responder.note_handed(CHAT)
    conv = store.create_conversation("claude_cli", None)
    text = main._with_open_case(conv["id"], "It is not code, she is asking your opinion")
    assert "Lee Pat's open case" in text


def test_a_pronoun_with_two_open_cases_guesses_nothing():
    for who in (CHAT, "Sam Roe"):
        _said(who, who, "a question", 300)
        _said(who, "Arunkumar K", "an answer", 200)
        responder.note_handed(who)
    conv = store.create_conversation("claude_cli", None)
    assert main._with_open_case(conv["id"], "she is asking your opinion") == "she is asking your opinion"


# --- F6: advice is not a code change ----------------------------------------------

@pytest.mark.parametrize("text", [
    "now , where i need to add this activity ?",
    "how should I test that workflow?",
    "Is this condition Valid Arun , to check the amend booking",
    "then what i need to check with",
])
def test_asking_advice_on_her_own_work_is_not_a_code_change(text):
    assert responder.asks_advice(text)


@pytest.mark.parametrize("text", [
    "can you add the retry to the booking consumer and raise a PR",
    "please fix the mapper in svc-booking",
])
def test_a_real_code_ask_is_still_one(text):
    assert not responder.asks_advice(text)


# --- the false correction -----------------------------------------------------------

def test_an_earlier_real_send_is_not_called_false_by_her_other_name():
    _said(CHAT, "Arunkumar K", "the amend event answer", 120)
    claim = main.unproven_send("The amend-event answer is already sent to Pat.", time.time())
    assert claim == ""
