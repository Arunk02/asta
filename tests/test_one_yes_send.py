"""27 Oct: a long note he dictated to a colleague "exactly as written" was staged
back to him, then his "yes send it as is" was read as an edit, and it took a
third yes to send. His words are the approval — once. (Text here is synthetic.)"""
from __future__ import annotations

import asyncio

import pytest

from app import loop, main, ops, store, tasks
from tests.test_say_it_once import _asked

NOTE = ("Hi, on your PR 4242 review comments: 1) The flag is read after the first activity in "
        "its queue, so nothing reads it before it is set. 2) The two party fields come from the "
        "same lookup, so a missing value is the lookup, not the mapper; for order ORD-0000-TEST "
        "the lookup returned empty in the test environment. 3) The amend path keeps the old flag, "
        "which is what we want for an amendment, so please leave it as it is. 4) Tests: add one "
        "for the amend path with the flag already set, and one for the empty lookup case. Ping me "
        "if any of this looks wrong after you rerun the suite and compare the two runs.")


@pytest.mark.parametrize("said", ["yes send it as is", "send it as is", "yes send as is",
                                  "yes, send it as it is", "send as it is", "yes go ahead and send",
                                  "send it exactly as written", "ok send it like that",
                                  "go ahead and send it", "yes send it as is pls"])
def test_his_ways_of_saying_send_it_unchanged_are_a_yes(said):
    assert main._affirmation(said)[0] is True


@pytest.mark.parametrize("said", ["send it as is but drop point 3", "yes but change the second line",
                                  "don't send it as is", "not as is"])
def test_an_edit_or_a_no_is_not_a_yes(said):
    assert main._affirmation(said)[0] is False


@pytest.fixture
def teams(monkeypatch):
    sent = []

    async def run(op):
        sent.append(op)
        return "📨 Sent to Alex Doe."

    async def notify(msg, kind="", **k):
        pass

    from app import notify as notify_mod
    monkeypatch.setattr(ops, "run", run)
    monkeypatch.setattr(notify_mod, "notify", notify)
    return sent


def _prepare(agent, cid, monkeypatch, what):
    monkeypatch.setattr(tasks, "current_conversation", lambda: cid)

    async def go_():
        out = agent.prepare_to_send(what, to="Alex Doe", channel="teams")
        await asyncio.sleep(0.05)
        return out
    return asyncio.run(go_())


def test_a_long_message_he_dictated_exactly_goes_out_without_a_second_yes(monkeypatch, teams):
    assert len(NOTE) > 600
    agent, cid = _asked(monkeypatch, f"Send this to Alex Doe on Teams (1:1), exactly as written: {NOTE}")
    out = _prepare(agent, cid, monkeypatch, NOTE)
    assert out.startswith("Sending to Alex Doe now")
    assert teams and teams[0]["args"]["text"] == NOTE, "his words, unchanged"
    assert not loop.awaiting(cid)


def test_a_long_message_a_brain_wrote_still_waits_for_his_yes(monkeypatch, teams):
    agent, cid = _asked(monkeypatch, "send alex a summary of the PR 4242 comments")
    out = _prepare(agent, cid, monkeypatch, NOTE)
    assert teams == [] and out.startswith("Draft staged")


def test_his_text_with_an_assistant_line_removed_is_not_his_verbatim_text(monkeypatch, teams):
    text = NOTE + " Will post once I confirm with Arun."
    agent, cid = _asked(monkeypatch, f"Send this to Alex Doe exactly as written: {text}")
    out = _prepare(agent, cid, monkeypatch, text)
    assert teams == [] and out.startswith("Draft staged"), "as_him changed it, so he reads it first"


def test_a_revision_restaged_in_the_turn_that_approved_it_is_sent(monkeypatch, teams):
    """His yes came with an edit: the brain revises and stages again in that turn."""
    words = "yes looks good update and send"
    agent, cid = _asked(monkeypatch, f"Arun did NOT approve sending the draft as-is. His feedback:\n{words}\n\n"
                                     f"Revise accordingly — change only what he asked for")
    loop.stage(cid, {"kind": "send", "what": NOTE, "to": "Alex Doe", "channel": "teams"})
    loop.clear_awaiting(cid, answered_by=words)
    out = _prepare(agent, cid, monkeypatch, NOTE.replace("pay-to", "pay to"))
    assert out.startswith("Sending to Alex Doe now") and len(teams) == 1


def test_the_answered_draft_does_not_approve_a_later_unrelated_turn(monkeypatch, teams):
    agent, cid = _asked(monkeypatch, "ok what else is pending")
    loop.stage(cid, {"kind": "send", "what": NOTE, "to": "Alex Doe", "channel": "teams"})
    loop.clear_awaiting(cid, answered_by="yes looks good update and send")
    out = _prepare(agent, cid, monkeypatch, NOTE)
    assert teams == [] and out.startswith("Draft staged")


def test_the_27_october_exchange_now_sends_on_his_yes(monkeypatch, teams):
    """Replayed through the chat path: the draft is waiting, he says "yes send it as is"."""
    from tests.test_say_it_once import _Sink, _conv, _no_brain
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    conv = _conv()
    loop.stage(conv["id"], {"type": "send", "kind": "send", "what": NOTE, "to": "Alex Doe",
                            "channel": "teams"})
    asyncio.run(main._dispatch(conv, "yes send it as is", _Sink(), "web"))
    assert len(teams) == 1 and teams[0]["args"]["to"] == "Alex Doe"
    assert not loop.awaiting(conv["id"])
