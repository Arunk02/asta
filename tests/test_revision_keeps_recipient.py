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
    loop.note_reply(cid, {"to": "Priya Nair", "what": "no idea who is on that team"})
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
    loop.note_reply(cid, {"to": "Priya Nair", "what": "old words"})
    assert agent._route_recipient(cid, capabilities.said_this_turn(), "Ravi Menon", False) \
        == ("Ravi Menon", False, "")


# --- a reply goes back where the ask came from --------------------------------
# 12 Nov: a colleague asked in a group; that draft got feedback, and his next
# plain line ("ask him to use develop") was staged 1:1 to the colleague.

GROUP = "Ravi Menon, Priya, +2"
NAMED = "Release Prep - Oct"


def _reply_to(monkeypatch, words, chat, who, to, to_group=False, channel="teams"):
    from app import tasks
    agent, cid = _asked(monkeypatch, words)
    monkeypatch.setattr(tasks, "current_conversation", lambda: cid)
    loop.note_reply(cid, {"to": chat, "to_group": True, "who": who, "what": "the group draft"})
    return agent.prepare_to_send("Please use the develop branch.", to=to, channel=channel,
                                 to_group=to_group), cid


def test_the_person_who_asked_in_a_group_is_answered_in_the_group(monkeypatch):
    out, _ = _reply_to(monkeypatch, "no ask him to use develop branch", GROUP,
                       "Ravi Menon", "Ravi Menon")
    assert out.startswith("Staged to the group"), out
    assert f"Draft staged to group {GROUP}" in out


def test_a_group_with_its_own_name_works_the_same(monkeypatch):
    out, _ = _reply_to(monkeypatch, "ask him to use develop", NAMED, "Kiran Das", "Kiran Das")
    assert f"Draft staged to group {NAMED}" in out, out


def test_a_member_its_title_lists_goes_to_the_group(monkeypatch):
    out, _ = _reply_to(monkeypatch, "ask her to use develop", GROUP, "Ravi Menon", "Priya")
    assert f"Draft staged to group {GROUP}" in out, out


def test_the_group_itself_stages_unchanged(monkeypatch):
    out, _ = _reply_to(monkeypatch, "ask him to use develop", GROUP, "Ravi Menon", GROUP,
                       to_group=True)
    assert out.startswith(f"Draft staged to group {GROUP}"), out


def test_he_can_ask_for_it_one_to_one(monkeypatch):
    out, _ = _reply_to(monkeypatch, "message him 1:1 to use develop", GROUP, "Ravi Menon",
                       "Ravi Menon")
    assert out.startswith("Draft staged to Ravi Menon"), out


def test_he_can_name_the_person(monkeypatch):
    out, _ = _reply_to(monkeypatch, "tell ravi to use develop", GROUP, "Ravi Menon", "Ravi Menon")
    assert out.startswith("Draft staged to Ravi Menon"), out


def test_someone_outside_the_group_is_not_moved(monkeypatch):
    out, _ = _reply_to(monkeypatch, "ask him to use develop", GROUP, "Ravi Menon", "Kiran Das")
    assert out.startswith("Draft staged to Kiran Das"), out


def test_an_email_is_never_moved_into_a_teams_group(monkeypatch):
    out, _ = _reply_to(monkeypatch, "email him the steps", GROUP, "Ravi Menon",
                       "ravi.menon@example.com", channel="email")
    assert not out.startswith("Staged to the group"), out


def test_staging_records_where_the_reply_goes():
    cid = "c-reply"
    loop.stage(cid, {"type": "answer", "to": GROUP, "to_group": True, "who": "Ravi Menon",
                     "what": "on it"})
    ctx = loop.reply_context(cid)
    assert ctx["to"] == GROUP and ctx["group"] and ctx["who"] == "Ravi Menon"
    loop.stage(cid, {"to": GROUP, "to_group": True, "what": "revised"})   # a brain's revision
    assert loop.reply_context(cid)["who"] == "Ravi Menon"
    loop.clear_awaiting(cid)


def test_the_context_lasts_an_hour():
    import time
    cid = "c-old"
    loop.note_reply(cid, {"to": GROUP, "to_group": True, "who": "Ravi Menon"})
    assert loop.reply_context(cid)
    assert loop.reply_context(cid, now=time.time() + loop.REPLY_SECONDS + 1) == {}


def test_the_brain_is_told_where_the_open_reply_goes():
    from app import main
    loop.note_reply("c-told", {"to": GROUP, "to_group": True, "who": "Ravi Menon"})
    said = main._with_open_reply("c-told", "ask him to use develop")
    assert said.startswith("[Open reply: Ravi Menon asked in the group") and GROUP in said
    loop.note_reply("c-one", {"to": "Priya Nair"})
    assert main._with_open_reply("c-one", "hi") == "hi"
