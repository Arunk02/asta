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
