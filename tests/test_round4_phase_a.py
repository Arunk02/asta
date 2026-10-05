"""Round 4, phase A: four of the six failures of 29 September, fixed inside the
current pipeline.

Each test is one real exchange from that day:

  · Navya, in a 1:1: "Thank you", and he had already reacted with a salute.
    Pushed to him in red anyway — only a TEXT reply counted as handled.
  · He asked "tell abt telikos inland journey". 31 of his 52 indexed passages
    mention inland; the tool ranker handed the brain 31 tools and not
    `search_knowledge`, and the answer was "no grounded info here".
  · Asta pushed "Navya R: need ur help", and a minute later he said "ask her
    what it is" and was asked who "her" was. Pushes never reach the thread the
    brain reads.
  · His WhatsApp is one thread of 490 messages, re-read at 107k-300k tokens a
    turn. A phone conversation that picks up after hours is a new sitting.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app import store


# --- his reaction is his answer ---------------------------------------------------------

def _stored(chat: str, sender: str, text: str, sent_at: float) -> dict:
    return {"chat": chat, "sender": sender, "text": text, "sent_at": sent_at}


def test_in_a_one_to_one_a_reaction_on_their_message_is_his():
    """A 1:1 has two people in it. A reaction on her message is his."""
    from app import chat_watch
    m = _stored("Navya R", "Navya R", "Thank you\n\n1 Saluting face reaction.", time.time())
    assert chat_watch.answered_by_him("Navya R", m)


def test_in_a_group_a_reaction_proves_nothing_about_who_reacted():
    from app import chat_watch
    m = _stored("BEP_Telikos : Defect Triage", "Roshan Kumar Thakur",
                "same behaviour on PP also\n\n1 Like reaction.", time.time())
    assert not chat_watch.answered_by_him("BEP_Telikos : Defect Triage", m)


def test_a_message_without_a_reaction_is_not_handled_by_one():
    from app import chat_watch
    m = _stored("Navya R", "Navya R", "I have done the change, please review", time.time())
    assert not chat_watch.answered_by_him("Navya R", m)


def test_the_word_reaction_in_a_sentence_is_not_a_reaction():
    """Only the count Teams renders under a message counts."""
    from app import chat_watch
    m = _stored("Navya R", "Navya R", "what was the customer reaction to the release?",
                time.time())
    assert not chat_watch.answered_by_him("Navya R", m)


# --- his documents are always within reach ----------------------------------------------

@pytest.fixture
def indexed(tmp_path, monkeypatch):
    from app import knowledge
    folder = tmp_path / "knowledge"
    folder.mkdir()
    monkeypatch.setenv("ASTA_KNOWLEDGE_DIR", str(folder))
    (folder / "inland.md").write_text(
        "# New Booking Journey Overview\n\nThe inland booking journey runs across "
        "three stages: Search, Search results and Additional Details. A booking is "
        "confirmed once the haulage leg is planned.\n")
    knowledge.reindex()
    return knowledge


def test_a_question_his_documents_answer_is_given_the_tool(indexed):
    from app import tool_index
    assert "search_knowledge" in tool_index.required_for("tell abt telikos inland journey")


def test_a_question_they_do_not_answer_is_not_forced_to_carry_it(indexed):
    from app import tool_index
    assert "search_knowledge" not in tool_index.required_for("set a reminder for 5pm")


def test_the_passages_arrive_before_the_brain_answers(indexed):
    """A tool the brain may forget to call is not grounding. The passages are put
    in front of it, with where they came from, before it starts."""
    from app import copilot_cli
    block = copilot_cli.turn_context("tell abt telikos inland journey")
    assert "inland booking journey" in block.lower()
    assert "inland.md" in block, "every passage says which document it came from"


def test_small_talk_carries_no_documents(indexed):
    from app import copilot_cli
    assert "inland" not in copilot_cli.turn_context("thanks, that's all").lower()


def test_a_broken_index_never_breaks_a_turn(monkeypatch):
    from app import copilot_cli, knowledge

    def boom(*a, **k):
        raise RuntimeError("index unreadable")
    monkeypatch.setattr(knowledge, "search", boom)
    assert isinstance(copilot_cli.turn_context("tell abt telikos inland journey"), str)


# --- who "her" is -----------------------------------------------------------------------

def test_the_person_just_pushed_is_named_in_the_next_turn():
    from app import copilot_cli, referents
    referents.note("Navya R", "need ur help", source="Teams 1:1")
    block = copilot_cli.turn_context("ask her what it is")
    assert "Navya R" in block and "need ur help" in block


def test_the_newest_person_comes_first_and_the_list_stays_short():
    from app import referents
    for i in range(20):
        referents.note(f"Person {i}", f"said thing {i}", source="Teams 1:1")
    names = [r["who"] for r in referents.recent()]
    assert names[0] == "Person 19"
    assert len(names) <= referents.KEEP


def test_mentioning_someone_again_moves_them_up_rather_than_duplicating():
    from app import referents
    referents.note("Navya R", "need ur help", source="Teams 1:1")
    referents.note("Vinish Kumar", "ping when free", source="Teams 1:1")
    referents.note("Navya R", "Thank you", source="Teams 1:1")
    rows = referents.recent()
    assert [r["who"] for r in rows][:2] == ["Navya R", "Vinish Kumar"]
    assert sum(1 for r in rows if r["who"] == "Navya R") == 1


def test_people_named_days_ago_are_not_offered_as_her():
    from app import referents
    referents.note("Old Person", "hello", source="Teams 1:1",
                   now=time.time() - 3 * 86400)
    assert all(r["who"] != "Old Person" for r in referents.recent())


# --- a phone conversation after a gap is a new sitting ----------------------------------

def test_a_long_gap_starts_a_new_episode():
    from app import episodes
    cid = "conv-wa"
    episodes.touch(cid, now=1000.0)
    assert episodes.gap_elapsed(cid, now=1000.0 + episodes.gap_seconds() + 1)


def test_a_quick_reply_stays_in_the_same_episode():
    from app import episodes
    cid = "conv-wa"
    episodes.touch(cid, now=1000.0)
    assert not episodes.gap_elapsed(cid, now=1000.0 + 60)


def test_the_first_message_ever_is_not_a_gap():
    from app import episodes
    assert not episodes.gap_elapsed("never-seen", now=5000.0)


def test_the_web_ui_keeps_its_own_chats_untouched():
    """The UI already has one context per chat; only phone channels, which are one
    endless thread, need a sitting boundary."""
    from app import episodes
    assert all(episodes.applies_to(channel) for channel in ("whatsapp", "telegram", "voice"))
    assert not episodes.applies_to("web")
