"""12 Nov: a test line typed while a colleague's answer was waiting was taken as
feedback on it and re-staged to a Teams group sharing one word with it. A
revision keeps its recipient unless he names another. (Synthetic names.)"""
from __future__ import annotations

from app import loop
from tests.test_say_it_once import _asked


def _feedback(words: str) -> str:
    return (f"Arun did NOT approve sending the draft as-is. His feedback:\n{words}\n\n"
            f"Revise accordingly — change only what he asked for.")


def _stage(monkeypatch, words, to, to_group=False):
    from app import tasks
    agent, cid = _asked(monkeypatch, _feedback(words))
    monkeypatch.setattr(tasks, "current_conversation", lambda: cid)
    loop.note_rejected({"to": "Priya Nair", "what": "no idea who is on that team"})
    return agent.prepare_to_send("bridge ok", to=to, channel="teams", to_group=to_group), cid


def test_a_revision_never_moves_to_a_group_he_did_not_name(monkeypatch):
    out, cid = _stage(monkeypatch, 'test after the move: reply with just "bridge ok"',
                      "Test Scenario walkthrough - Oct Release", to_group=True)
    assert out.startswith("Not staged") and "Priya Nair" in out
    assert not loop.awaiting(cid)


def test_a_group_sharing_one_word_is_not_named_even_without_to_group(monkeypatch):
    out, _ = _stage(monkeypatch, "test it again", "Test Scenario walkthrough - Oct Release")
    assert out.startswith("Not staged")


def test_the_same_recipient_still_stages(monkeypatch):
    out, _ = _stage(monkeypatch, "make it shorter", "Priya")
    assert out.startswith("Draft staged to Priya"), out


def test_a_person_he_names_by_first_name_may_take_it(monkeypatch):
    out, _ = _stage(monkeypatch, "send it to ravi instead", "Ravi Menon")
    assert not out.startswith("Not staged"), out


def test_a_group_he_names_in_full_may_take_it(monkeypatch):
    out, _ = _stage(monkeypatch, "post it in release prep - oct instead",
                    "Release Prep - Oct", to_group=True)
    assert not out.startswith("Not staged"), out


def test_an_ordinary_ask_is_not_a_revision(monkeypatch):
    from app import agent, capabilities, tasks
    _, cid = _asked(monkeypatch, "tell ravi the build is green")
    monkeypatch.setattr(tasks, "current_conversation", lambda: cid)
    loop.note_rejected({"to": "Priya Nair", "what": "old words"})
    assert agent._revision_moves_recipient(capabilities.said_this_turn(), "Ravi Menon", False) == ""


# --- a group's conversation stays in the group ---------------------------------
# 12 Nov: a colleague asked in a group; that draft got feedback, and his next
# plain line ("ask him to use develop") was staged 1:1 to the colleague.

GROUP = "Ravi Menon, Priya, +2"


def _after_group_draft(monkeypatch, words, to, to_group=False):
    from app import tasks
    agent, cid = _asked(monkeypatch, words)
    monkeypatch.setattr(tasks, "current_conversation", lambda: cid)
    loop.note_rejected({"to": GROUP, "to_group": True, "what": "the group draft"})
    return agent.prepare_to_send("Please use the develop branch.", to=to, channel="teams",
                                 to_group=to_group), cid


def test_a_plain_line_after_a_group_draft_does_not_go_one_to_one(monkeypatch):
    out, cid = _after_group_draft(monkeypatch, "no ask him to use develop branch", "Ravi Menon")
    assert out.startswith("Not staged") and GROUP in out
    assert not loop.awaiting(cid)


def test_it_may_go_to_the_group(monkeypatch):
    out, _ = _after_group_draft(monkeypatch, "no ask him to use develop branch", GROUP,
                                to_group=True)
    assert not out.startswith("Not staged"), out


def test_he_can_ask_for_it_one_to_one(monkeypatch):
    out, _ = _after_group_draft(monkeypatch, "message him 1:1 to use develop", "Ravi Menon")
    assert not out.startswith("Not staged"), out


def test_he_can_name_the_person(monkeypatch):
    out, _ = _after_group_draft(monkeypatch, "tell ravi to use develop", "Ravi Menon")
    assert not out.startswith("Not staged"), out


def test_someone_outside_the_group_is_not_affected(monkeypatch):
    out, _ = _after_group_draft(monkeypatch, "ask him to use develop", "Kiran Das")
    assert not out.startswith("Not staged"), out


def test_a_member_who_wrote_in_a_named_group_counts(monkeypatch):
    from app import agent, store
    monkeypatch.setattr(store, "teams_messages",
                        lambda chat="", **k: [{"sender": "Kiran Das", "text": "hi"}]
                        if chat == "Release Prep - Oct" else [])
    assert agent._in_group("Kiran Das", "Release Prep - Oct")
    assert not agent._in_group("Asha Rao", "Release Prep - Oct")
