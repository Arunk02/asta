"""1 Oct, afternoon: the right yes, the right chat, in his voice, and heard back.

Replayed from his WhatsApp with Asta between 14:26 and 15:20:

  * "Send" — meant for the Shabda group message — posted a request for changes
    on Komal's PR: a task had staged the review seconds earlier and nobody had
    shown it to him. The next "Send" went to Komal's draft, which said "Will
    post once I confirm with Arun" in his own name.
  * "Shabda Anubhav, Vinish, +2" was not found by search, and the message went
    to Shabda's 1:1 instead.
  * A send during Asta's own call failed, and the draft was gone with it; the
    brain then told him it had been sent.
  * "hi Arunkumar, sorry saw the ping now" got "Hi Shabda, what's the issue?" —
    after he had already answered.
  * Komal's 14:56 message was stored by another reader and never reached him.
  * Copilot went silent on "why u not reading any teams message" and nobody
    answered it.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from app import answers, capabilities, chat_watch, loop, main, offers, ops, store, tasks
from app import teams_bridge as tb


class _Sink:
    def __init__(self):
        self.sent = []
        self.alive = True

    async def send(self, payload):
        self.sent.append(payload)


def _conv():
    c = store.create_conversation(model="claude_cli", workspace=None)
    c["model"] = "claude_cli"
    return c


def _no_brain(*a, **k):
    raise AssertionError("a brain was asked about a yes the code should have settled")


@pytest.fixture
def told(monkeypatch):
    pushed: list[str] = []

    async def notify(msg, kind="", **k):
        pushed.append(msg)

    from app import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", notify)
    return pushed


def _review_offer(from_task: bool):
    token = capabilities.FROM_TASK.set("199" if from_task else "")
    try:
        return offers.staged_write(
            "pr_review_inline", {"pr": "1459", "action": "request_changes", "body": "x",
                                 "comments": []},
            "🔎 Request changes on PR #1459 — 7 inline comment(s)", "VERDICT: REQUEST CHANGES",
            "Post this review on PR #1459 as you?", kind="pr_write")
    finally:
        capabilities.FROM_TASK.reset(token)


# --- a yes answers only what he has been shown ---------------------------------

def test_a_review_a_task_staged_a_moment_ago_is_not_posted_by_his_send(monkeypatch, told):
    ran: list[dict] = []

    async def run(op):
        ran.append(op)
        return "posted"

    monkeypatch.setattr(ops, "run", run)
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    offers.drop_all()
    _review_offer(from_task=True)
    conv, sink = _conv(), _Sink()
    asyncio.run(main._dispatch(conv, "Send", sink, "whatsapp"))
    assert ran == [], "his Send was for something else; the review was never shown to him"
    assert "just came in" in str(sink.sent) and "PR #1459" in str(sink.sent)
    assert offers.pending() and offers.pending().shown, "shown now — answerable next time"
    asyncio.run(main._dispatch(conv, "yes", _Sink(), "whatsapp"))
    assert ran and ran[0]["name"] == "pr_review_inline"


def test_send_does_not_post_a_review_even_one_he_has_seen(monkeypatch, told):
    async def run(op):
        raise AssertionError("a 'send' posted a review")

    monkeypatch.setattr(ops, "run", run)
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    offers.drop_all()
    _review_offer(from_task=False)
    sink = _Sink()
    asyncio.run(main._dispatch(_conv(), "Send", sink, "whatsapp"))
    assert "that is a post, not a message" in str(sink.sent)
    assert offers.pending(), "still open for his yes"


def test_an_offer_he_has_not_seen_survives_him_changing_the_subject(monkeypatch, told):
    monkeypatch.setattr(main, "_start_turn", lambda *a, **k: None)
    offers.drop_all()
    _review_offer(from_task=True)
    asyncio.run(main._dispatch(_conv(), "what is the status of the CT fix", _Sink(), "whatsapp"))
    assert offers.pending(), "never shown — it is not something he moved on from"


def test_a_queued_offer_waits_to_be_announced_before_a_yes_counts():
    offers.drop_all()
    offers.propose("first", "", "Do the first?", "do it")
    offers.propose("second", "", "Do the second?", "do it")
    offers.accept()
    head = offers.pending()
    assert head.subject == "second" and not head.shown
    head.render()
    assert offers.pending().shown


# --- the draft he meant --------------------------------------------------------

def _stage(cid, to, what, who=""):
    loop.stage(cid, {"type": "answer", "kind": "send", "what": what, "to": to,
                     "channel": "teams", "who": who or to, "_shown": time.time()})


def test_send_after_asking_about_someone_else_asks_which(monkeypatch, told):
    sent: list[dict] = []

    async def run(op):
        sent.append(op)
        return "✅ Sent to Fake Internal Team."

    monkeypatch.setattr(ops, "run", run)
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    store.kv_set("chatwatch_rail", json.dumps(["Shabda Anubhav, Vinish, +2", "Fake Internal Team"]))
    conv = _conv()
    store.add_ui_message(conv["id"], "user", "Send the Answer for shabda issue in the group", {})
    _stage(conv["id"], "Fake Internal Team", "Two blocking ones: the rename breaks lookups.",
           who="Komal Jayswal")
    sink = _Sink()
    asyncio.run(main._dispatch(conv, "Send", sink, "whatsapp"))
    assert sent == [], "Komal's draft is not the Shabda message"
    assert "Fake Internal Team" in str(sink.sent) and "Shabda" in str(sink.sent)
    assert loop.awaiting(conv["id"])
    asyncio.run(main._dispatch(conv, "Send", _Sink(), "whatsapp"))
    assert sent and sent[0]["args"]["to"] == "Fake Internal Team", "his second send means it"


def test_send_right_after_asking_for_that_draft_goes_straight(monkeypatch, told):
    sent: list[dict] = []

    async def run(op):
        sent.append(op)
        return "✅ Sent to Vinish Kumar."

    monkeypatch.setattr(ops, "run", run)
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    store.kv_set("chatwatch_rail", json.dumps(["Vinish Kumar", "Shabda Anubhav Dev"]))
    conv = _conv()
    store.add_ui_message(conv["id"], "user", "inform vinish the PR is raised", {})
    _stage(conv["id"], "Vinish Kumar", "bro raised the PR")
    asyncio.run(main._dispatch(conv, "Send", _Sink(), "whatsapp"))
    assert sent and sent[0]["args"]["to"] == "Vinish Kumar"


def test_a_send_that_fails_leaves_the_draft_waiting(monkeypatch, told):
    async def run(op):
        raise tb.NotFound("the group 'Shabda Anubhav, Vinish, +2' is not on the chat list")

    monkeypatch.setattr(ops, "run", run)
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    conv = _conv()
    _stage(conv["id"], "Shabda Anubhav, Vinish, +2", "checked PR 94, not the cause")
    sink = _Sink()
    asyncio.run(main._dispatch(conv, "send", sink, "whatsapp"))
    assert "Not sent" in str(sink.sent) and "still waiting" in str(sink.sent)
    assert loop.awaiting(conv["id"])["what"] == "checked PR 94, not the cause"


def test_a_send_during_asta_s_own_call_goes_out_when_the_call_ends(monkeypatch, told):
    sent: list[dict] = []
    calling = {"on": True}

    async def run(op):
        sent.append(op)
        return "✅ Sent to Shabda Anubhav, Vinish, +2."

    monkeypatch.setattr(ops, "run", run)
    monkeypatch.setattr(tb, "in_a_call", lambda: calling["on"])
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    real_sleep = asyncio.sleep

    async def fast(_s):
        calling["on"] = False
        await real_sleep(0)

    monkeypatch.setattr(main.asyncio, "sleep", fast)
    conv = _conv()
    _stage(conv["id"], "Shabda Anubhav, Vinish, +2", "checked PR 94, not the cause")
    sink = _Sink()

    async def go():
        await main._dispatch(conv, "send", sink, "whatsapp")
        for _ in range(20):
            await real_sleep(0)

    asyncio.run(go())
    assert "goes out the moment the call ends" in str(sink.sent)
    assert sent and sent[0]["args"]["to"] == "Shabda Anubhav, Vinish, +2"
    assert any("Sent to" in m for m in told), "and he hears that it went"


# --- the right chat --------------------------------------------------------------

def _opener(monkeypatch, rail_ok: bool):
    calls: list[tuple] = []

    async def rail(page, chat, strict=False):
        calls.append(("rail", chat, strict))
        return rail_ok

    async def find(page, chat, allow_group=False, group_only=False):
        calls.append(("search", chat, allow_group, group_only))
        return chat

    async def title(page):
        return "Shabda Anubhav Dev, Vinish Kumar, Yogesh K, Arunkumar K"

    async def rail_wait(page, timeout=20.0):
        return 5

    monkeypatch.setattr(tb, "_open_from_rail", rail)
    monkeypatch.setattr(tb, "_find_chat", find)
    monkeypatch.setattr(tb, "_chat_title", title)
    monkeypatch.setattr(tb, "wait_for_rail", rail_wait)
    return calls


def test_a_group_listed_by_its_members_is_sent_through_its_own_row(monkeypatch):
    calls = _opener(monkeypatch, rail_ok=True)
    asyncio.run(tb._open_target(object(), "Shabda Anubhav, Vinish, +2", to_group=False))
    assert calls == [("rail", "Shabda Anubhav, Vinish, +2", True)], \
        "a members-list name is a group whatever the caller said, and never searched"


def test_a_members_list_group_that_cannot_be_opened_is_refused_not_guessed(monkeypatch):
    calls = _opener(monkeypatch, rail_ok=False)
    with pytest.raises(tb.NotFound, match="nothing sent"):
        asyncio.run(tb._open_target(object(), "Shabda Anubhav, Vinish, +2", to_group=True))
    assert all(c[0] == "rail" for c in calls), "no search for the people in it"


def test_a_named_group_falls_back_to_search_for_groups_only(monkeypatch):
    calls = _opener(monkeypatch, rail_ok=False)
    asyncio.run(tb._open_target(object(), "Fake Internal Team", to_group=True))
    assert calls[-1] == ("search", "Fake Internal Team", True, True)


def test_a_person_is_still_their_one_to_one(monkeypatch):
    calls = _opener(monkeypatch, rail_ok=True)
    asyncio.run(tb._open_target(object(), "Vinish Kumar", to_group=False))
    assert calls == [("search", "Vinish Kumar", False, False)]


def test_a_send_checks_every_member_the_row_names():
    assert tb._rail_title_ok("Shabda Anubhav Dev, Vinish Kumar, Yogesh K", "Shabda Anubhav, Vinish, +2",
                             strict=True)
    assert not tb._rail_title_ok("Shabda Anubhav Dev", "Shabda Anubhav, Vinish, +2", strict=True), \
        "Shabda's 1:1 is not the group"
    assert tb._rail_title_ok("Fake Internal Team", "Fake Internal Team", strict=True)
    assert not tb._rail_title_ok("Fake Team", "Fake Internal Team", strict=True)


class _Page:
    """Enough of a Teams page for `_find_chat` to search, pick and click."""

    def __init__(self, options, opens=True):
        self.options, self.opens = options, opens
        self.keyboard = self

    async def press(self, _k):
        pass

    async def wait_for_selector(self, sel, timeout=0):
        if "messageBodyContent" in sel and not self.opens:
            raise TimeoutError("Timeout 20000ms exceeded")
        return self

    async def fill(self, _t):
        pass

    async def type(self, _t, delay=0):
        pass

    async def click(self, _sel, timeout=0):
        pass

    async def evaluate(self, js, *args):
        if "querySelectorAll('[role=\"option\"]').length" in js or "map((o, i)" in js:
            return self.options
        if "data-asta-pick" in js:
            return True
        if "treeitem" in js:
            return []
        return ""


def test_a_group_he_named_never_resolves_to_a_person_in_it(monkeypatch):
    real = asyncio.sleep
    monkeypatch.setattr(tb.asyncio, "sleep", lambda *_: real(0))
    page = _Page([{"i": 0, "aria": "Person Shabda Anubhav Dev", "text": "Shabda Anubhav Dev", "tid": ""}])
    with pytest.raises(tb.NotFound, match="no group named"):
        asyncio.run(tb._find_chat(page, "Shabda Anubhav Dev", allow_group=True, group_only=True))


def test_a_result_that_does_not_open_as_a_chat_is_a_clean_refusal(monkeypatch):
    """Twelve Chrome relaunches between 15:00 and 15:20 — one per chat that did
    not open. NotFound sends the page home instead."""
    real = asyncio.sleep
    monkeypatch.setattr(tb.asyncio, "sleep", lambda *_: real(0))
    page = _Page([{"i": 0, "aria": "Person Vinish Kumar", "text": "Vinish Kumar", "tid": ""}],
                 opens=False)
    with pytest.raises(tb.NotFound, match="did not open as a chat"):
        asyncio.run(tb._find_chat(page, "Vinish Kumar"))


# --- read once, by the sweep -------------------------------------------------------

def test_a_message_another_reader_stored_first_still_reaches_the_sweep(monkeypatch):
    chat = "Fake Internal Team"
    now = time.time()
    old = {"key": "k1", "chat": chat, "sender": "Vinish Kumar", "text": "for this only this is the answer",
           "sent_at": now - 600, "stamp": ""}
    komal = {"key": "k2", "chat": chat, "sender": "Komal Jayswal",
             "text": "u would have called and cleared your doubts", "sent_at": now - 60, "stamp": ""}
    chat_watch.remember(chat, [old])
    chat_watch.mark_processed(chat, [old])
    store.save_teams_messages([old, komal])         # a brain read the chat first

    async def read(c, limit=0, max_scrolls=0, since=None):
        return [old, komal]

    monkeypatch.setattr(tb, "read_history", read)
    fresh = asyncio.run(chat_watch.new_in(chat))
    assert [r["key"] for r in fresh] == ["k2"]
    assert asyncio.run(chat_watch.new_in(chat)) == [], "and once processed, never again"


def test_a_chat_with_no_record_yet_counts_what_the_sweep_had_reached():
    chat = "Vinish Kumar"
    now = time.time()
    a = {"key": "a", "chat": chat, "sender": "Vinish Kumar", "text": "old", "sent_at": now - 900, "stamp": ""}
    b = {"key": "b", "chat": chat, "sender": "Vinish Kumar", "text": "new", "sent_at": now - 30, "stamp": ""}
    store.save_teams_messages([a, b])
    store.kv_set(chat_watch._seen_at_key(chat), str(now - 600))
    done = chat_watch.processed(chat)
    assert chat_watch._ident_hash(a) in done and chat_watch._ident_hash(b) not in done


# --- in his voice, and not over him --------------------------------------------------

@pytest.mark.parametrize("said, kept", [
    ("Reviewed it Komal — staged comments on the PR. Two blocking ones: the rename breaks lookups. "
     "Will post once I confirm with Arun.", "Two blocking ones: the rename breaks lookups."),
    ("deployed to staging, raised a draft PR. pls check", "deployed to staging, raised a draft PR. pls check"),
    ("bro raised the PR\nhttps://github.com/a/b/pull/1", "bro raised the PR\nhttps://github.com/a/b/pull/1"),
])
def test_nothing_goes_out_in_his_name_that_he_would_not_write(said, kept):
    from app import writing
    assert writing.as_him(said) == kept


def test_a_draft_is_put_in_his_voice_and_a_members_list_is_a_group(monkeypatch):
    from app import agent
    cid = _conv()["id"]
    monkeypatch.setattr(tasks, "current_conversation", lambda: cid)
    monkeypatch.setattr(capabilities, "said_this_turn", lambda: "")
    agent.prepare_to_send("checked PR 94, not the cause. Will update once I confirm with Arun.",
                          to="Shabda Anubhav, Vinish, +2", channel="teams")
    staged = loop.take(cid)
    assert staged["to_group"] is True
    assert "Arun" not in staged["what"] and "checked PR 94" in staged["what"]


def test_hello_gets_a_warm_yes_tell_me_not_what_is_the_issue(monkeypatch):
    from app import steward, understand, writing
    monkeypatch.setattr(writing, "address_terms", lambda chat: [])
    assert steward.opener_line("Shabda Anubhav Dev", "Shabda Anubhav Dev") == "hi Shabda, yes tell me"
    assert understand.safe_question("Hi Shabda, what's the issue?", "Shabda Anubhav Dev") == ""
    assert understand.safe_question("what do you need?", "Komal Jayswal") == ""
    assert understand.safe_question("Is this about the email CT failure on PR 675?", "Shabda") != ""
    assert "help with" not in steward.ASK_BACK


def test_their_reply_to_his_message_is_his_conversation():
    chat = "Shabda Anubhav Dev"
    now = time.time()
    store.save_teams_messages([
        {"key": "m1", "chat": chat, "sender": "Arunkumar K", "text": "checked PR 94, not the cause",
         "sent_at": now - 1500, "stamp": ""},
        {"key": "m2", "chat": chat, "sender": chat, "text": "hi Arunkumar", "sent_at": now - 300, "stamp": ""},
        {"key": "m3", "chat": chat, "sender": chat, "text": "sorry saw the ping now", "sent_at": now - 290,
         "stamp": ""}])
    assert chat_watch.answering_him(chat, now - 300)
    assert not chat_watch.answering_him(chat, now - 290), "measured from the first new message"


def test_nothing_is_said_once_he_has_answered_himself(monkeypatch):
    sent: list[str] = []
    now = time.time()

    async def read(chat, limit=0, max_scrolls=0, since=None):
        return [{"sender": "Shabda Anubhav Dev", "text": "hi Arunkumar", "sent_at": now - 120},
                {"sender": "Arunkumar K", "text": "hi Shabda, tell me", "sent_at": now - 30}]

    async def send(chat, text, allow_group=False):
        sent.append(text)
        return chat

    monkeypatch.setattr(tb, "enabled", lambda: True)
    monkeypatch.setattr(tb, "read_history", read)
    monkeypatch.setattr(tb, "send_message", send)
    assert asyncio.run(chat_watch._say("Shabda Anubhav Dev", "hi Shabda, yes tell me",
                                       since=now - 120)) is False
    assert sent == []
    assert asyncio.run(chat_watch._say("Shabda Anubhav Dev", "hi Shabda, yes tell me",
                                       since=now - 10)) is True


def test_automatic_ack_does_not_hide_a_finished_answer(monkeypatch, told):
    now = time.time()
    chat = "Vinish Kumar"
    store.kv_set("wa_conversation", _conv()["id"])
    store.save_teams_messages([{"key": "question", "chat": chat, "sender": chat,
                                "text": "Please review my PR", "sent_at": now - 120}])
    store.record_automatic_teams_message("asta-ack", chat)

    async def read(name, limit=0, max_scrolls=0):
        return [{"key": "asta-ack", "sender": "Arunkumar K",
                 "text": "Checking, will update you", "sent_at": now - 60}]

    monkeypatch.setattr(tb, "enabled", lambda: True)
    monkeypatch.setattr(tb, "read_history", read)
    monkeypatch.setattr(chat_watch, "their_last", lambda name: now - 120)
    assert asyncio.run(answers.present(who=chat, need="review", chat=chat, group=False,
                                       analysis="All good", reply="Looks good to me."))
    assert loop.awaiting(answers.phone_conversation())["to"] == chat
    assert not any("already answered" in msg for msg in told)


def test_automatic_receipt_survives_restart_but_real_reply_still_counts(monkeypatch):
    now = time.time()
    chat = "Vinish Kumar"
    store.record_automatic_teams_message("ack-123", chat)
    rows = [{"key": "ack-123", "sender": "Arunkumar K", "sent_at": now - 30}]
    monkeypatch.setattr(tb, "enabled", lambda: True)

    async def read(name, limit=0, max_scrolls=0):
        return rows

    monkeypatch.setattr(tb, "read_history", read)
    assert not asyncio.run(chat_watch.he_replied_since(chat, now - 60))
    rows.append({"key": "human-456", "sender": "Arunkumar K", "sent_at": now - 20})
    assert asyncio.run(chat_watch.he_replied_since(chat, now - 60))
    assert asyncio.run(chat_watch.he_replied_since("Other chat", now - 60))


def test_automatic_send_records_only_the_fresh_verified_message(monkeypatch):
    now = time.time()
    from datetime import datetime, timezone

    def msg(sender, text, at):
        return {"sender": sender, "text": text,
                "iso": datetime.fromtimestamp(at, timezone.utc).isoformat()}

    older = msg("Arunkumar K", "Checking, will update you", now - 3600)
    current = msg("Arunkumar K", "Checking, will update you", now)
    assert not tb._automatic_receipt("Vinish", older["text"], [older], now)
    assert tb._automatic_receipt("Vinish", current["text"], [older, current], now)
    assert not store.is_automatic_teams_message(tb._msg_key("Vinish", older), "Vinish")
    assert store.is_automatic_teams_message(tb._msg_key("Vinish", current), "Vinish")
    assert not store.is_automatic_teams_message(tb._msg_key("Vinish", current), "Shabda")

    async def fake_send(chat, text):
        assert tb._automatic_send.get() == chat
        return chat

    monkeypatch.setattr(tb, "send_message", fake_send)
    assert asyncio.run(tb.send_automatic("Vinish", "checking")) == "Vinish"
    assert tb._automatic_send.get() == ""


def test_an_answer_he_already_gave_is_not_staged_again(monkeypatch, told):
    now = time.time()
    chat = "Shabda Anubhav, Vinish, +2"
    request = "Arunkumar, please review the email service issue"
    store.kv_set("wa_conversation", _conv()["id"])
    store.save_teams_messages([{"key": "s1", "chat": chat, "sender": "Shabda Anubhav Dev",
                                "text": request, "sent_at": now - 240,
                                "stamp": ""}])

    async def read(c, limit=0, max_scrolls=0, since=None):
        return [{"sender": "Arunkumar K", "text": "will check and update and approve",
                 "sent_at": now - 120}]

    monkeypatch.setattr(tb, "enabled", lambda: True)
    monkeypatch.setattr(tb, "read_history", read)
    assert asyncio.run(answers.present(who="Shabda Anubhav Dev", need="review email CT", chat=chat,
                                       group=True, analysis="Root cause: the 3.2.25 bump",
                                       reply="checked, it's the bump",
                                       source_text=request)) is True
    assert not loop.awaiting(answers.phone_conversation()), "no second reply staged"
    assert told and "already answered Shabda" in told[0] and "3.2.25" in told[0]


# --- what he hears is true, and he always hears ---------------------------------------

def test_a_claim_that_something_was_sent_is_checked_against_what_was(monkeypatch):
    monkeypatch.setattr(tb, "SENT", [])
    monkeypatch.setattr(tb, "STARTED", [])
    monkeypatch.setattr(ops, "DONE", [])
    t0 = time.time()
    lie = ('Already staged and ready — that message to the group ("checked PR 94 … so this '
           "isn't why email CT is failing\") is sent now with your confirm.")
    assert main.unproven_send(lie, t0)
    assert not main.unproven_send("Nothing had actually been sent.", t0)
    assert not main.unproven_send("Staged to the group — waiting on your yes.", t0)
    tb.SENT.append((time.time(), "Shabda Anubhav, Vinish, +2"))
    assert not main.unproven_send(lie, t0), "a send did happen this turn"


def test_a_false_claim_is_corrected_in_the_same_breath(monkeypatch):
    monkeypatch.setattr(tb, "SENT", [])
    monkeypatch.setattr(tb, "STARTED", [])
    monkeypatch.setattr(ops, "DONE", [])
    sink, conv = _Sink(), _conv()
    asyncio.run(main._correct_claims(sink, conv, "Done — it was sent to the group just now.", time.time()))
    assert "Correction: nothing was actually sent" in str(sink.sent)


def test_a_brain_that_stalls_silent_hands_the_message_on(monkeypatch):
    from app import agent as agent_mod
    from app import turn_budget
    ran: list[str] = []

    async def cli(out, conv, text, runner, brain, note=None, channel="web"):
        ran.append(brain)
        await out.send({"type": "note", "text": note})

    monkeypatch.setattr(main, "_run_turn_cli", cli)
    monkeypatch.setattr(main, "_fallback_chain", lambda failed: ["claude_cli"])
    monkeypatch.setattr(agent_mod, "is_cli", lambda b: True)
    monkeypatch.setattr(agent_mod, "runner", lambda b: None)
    stalled = turn_budget.TurnStopped(turn_budget.Stop("idle", 138.0, 120.0, []))
    sink = _Sink()
    assert asyncio.run(main._after_stall(sink, _conv(), "why u not reading teams", "copilot",
                                         "whatsapp", stalled)) is True
    assert ran == ["claude_cli"] and "stalled without answering" in str(sink.sent)
    spoke = turn_budget.TurnStopped(turn_budget.Stop("idle", 138.0, 120.0, ["half an answer"]))
    assert asyncio.run(main._after_stall(sink, _conv(), "x", "copilot", "whatsapp", spoke)) is False, \
        "it said something — that is reported, not re-asked"


def test_his_teams_status_is_set_without_a_brain(monkeypatch):
    set_to: list[str] = []

    async def presence(wanted):
        set_to.append(wanted)
        return "Appear away"

    monkeypatch.setattr(tb, "enabled", lambda: True)
    monkeypatch.setattr(tb, "set_presence", presence)
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    sink = _Sink()
    asyncio.run(main._dispatch(_conv(), "change the teams status into away", sink, "whatsapp"))
    assert set_to == ["away"] and "Appear away" in str(sink.sent)
    assert main._PRESENCE_CMD.match("change the teams status into offline or away") is None, \
        "two choices is a question for him, not a setting"


# --- 16:22: the booking question, the channels, and #201 ----------------------------

def test_the_sweep_reads_chats_not_the_teams_and_channels_below_them(monkeypatch):
    rows = [{"name": n, "text": n} for n in
            ["Copilot", "Mentions", "BEP_Telikos : Defect Triage", "Rajendra Kumar", "Vinish Kumar",
             "See more", "General", "Announcements", "See all channels", "General", "Techbytes"]]

    class Page:
        pass

    class Ctx:
        async def __aenter__(self):
            return Page()

        async def __aexit__(self, *a):
            return False

    async def rail_rows(page):
        return rows

    async def wait(page, timeout=20.0):
        return len(rows)

    monkeypatch.setattr(tb, "teams_page", lambda: Ctx())
    monkeypatch.setattr(tb, "wait_for_rail", wait)
    monkeypatch.setattr(chat_watch, "_rail_rows", rail_rows)
    store.kv_set(chat_watch._RAIL_KEY, json.dumps(["Vinish Kumar", "Rajendra Kumar",
                                                   "BEP_Telikos : Defect Triage"]))
    chosen = asyncio.run(chat_watch.candidates())
    assert "Rajendra Kumar" in chosen
    assert not {"General", "Announcements", "Techbytes", "See more"} & set(chosen)


def test_a_plan_waiting_for_his_yes_does_not_swallow_an_unrelated_follow_up(monkeypatch):
    conv = _conv()
    t = store.create_task("Fix Contract Test failure on email PR 675", "code", "p", None)
    store.update_task(t["id"], status="awaiting_approval")
    store.add_ui_message(conv["id"], "assistant",
                         "No booking H69LMCN6KZY in prod logs — should I check Temporal?", {})
    assert not main._conversation_is_on_task(conv["id"], t["id"])
    store.add_ui_message(conv["id"], "assistant",
                         f"📋 PLAN #{t['id']} Fix Contract Test failure on email PR 675", {})
    assert main._conversation_is_on_task(conv["id"], t["id"])


def test_the_working_note_names_only_a_task_this_message_started(monkeypatch):
    conv = _conv()
    t = store.create_task("Fix Contract Test failure on email PR 675", "code", "p", None)
    store.update_task(t["id"], status="awaiting_approval")
    monkeypatch.setattr(tasks, "live_tasks_for", lambda cid: [t["id"]])
    store_get = store.get_task
    monkeypatch.setattr(store, "get_task", lambda i: {**store_get(i), "created_at": time.time() - 3600})
    assert f"#{t['id']}" not in main._working_note(conv["id"], "H69LMCN6KZY check do we received this booking")


# --- manager and above: only on his "send" -------------------------------------------

@pytest.fixture
def above(monkeypatch, tmp_path):
    g = tmp_path / "guardrails.md"
    g.write_text("## Manager and above\nOnly on my send.\n- Praveen Kumar — my manager\n- Ravi Shankar\n")
    monkeypatch.setenv("ASTA_GUARDRAILS", str(g))
    return g


def test_the_list_is_his_guardrail_and_empty_means_off(above):
    from app import senior
    assert senior.people() == ["Praveen Kumar", "Ravi Shankar"]
    assert senior.is_senior("Praveen Kumar") and senior.is_senior("Praveen Kumar S")
    assert not senior.is_senior("Kumar") and not senior.is_senior("Vinish Kumar")
    assert not senior.is_senior("Praveen Kumar, Vinish, +2"), "a group waits for his yes anyway"
    above.write_text("## Communication\n- be polite\n")
    import os
    os.utime(above, (time.time() + 5, time.time() + 5))
    assert senior.people() == [] and not senior.is_senior("Praveen Kumar"), "removed → off"


def test_nothing_reaches_them_without_his_yes(above, monkeypatch):
    from app import senior
    with pytest.raises(senior.NeedsHisYes):
        asyncio.run(tb.send_message("Praveen Kumar", "checking, will update you"))
    with pytest.raises(senior.NeedsHisYes):
        asyncio.run(tb.send_voice_note("Praveen Kumar", "hi"))


def test_his_send_is_what_lets_it_through(above, monkeypatch):
    from app import senior
    seen: list[bool] = []

    async def fake(chat, text, allow_group=False):
        senior.check(chat)
        seen.append(True)
        return chat

    monkeypatch.setattr(tb, "send_message", fake)
    monkeypatch.setattr(tb, "SENT", [])
    out = asyncio.run(ops._teams_send(to="Praveen Kumar", text="done, PR is merged"))
    assert seen and "Praveen Kumar" in out


def test_no_automatic_line_goes_to_them_it_is_staged_for_him(above, monkeypatch, told):
    sent: list[str] = []

    async def send(chat, text, allow_group=False):
        sent.append(text)
        return chat

    monkeypatch.setattr(tb, "send_message", send)
    store.kv_set("wa_conversation", _conv()["id"])
    assert asyncio.run(chat_watch._say("Praveen Kumar", "checking, will update you")) is False
    assert sent == []
    staged = loop.awaiting(answers.phone_conversation())
    assert staged and staged["to"] == "Praveen Kumar" and staged["what"] == "checking, will update you"
    assert told and "Manager and above" in told[-1]


def test_asking_for_it_in_his_own_words_still_stages_it(above, monkeypatch):
    from app import agent
    monkeypatch.setattr(agent, "SEND_WHEN_ASKED", True)
    cid = _conv()["id"]
    monkeypatch.setattr(tasks, "current_conversation", lambda: cid)
    monkeypatch.setattr(capabilities, "said_this_turn", lambda: "tell praveen kumar the PR is merged")
    out = agent.prepare_to_send("PR is merged", to="Praveen Kumar", channel="teams")
    assert not out.startswith("Sending to"), "staged, not sent"
    assert loop.take(cid)["to"] == "Praveen Kumar"


# --- without LM Studio: still speaks, still reads, still catches the ask ---------------

def test_a_quick_verdict_falls_back_to_claude_then_copilot(monkeypatch):
    from app import claude_cli, copilot_cli, memory
    asked: list[str] = []

    async def claude(prompt, **k):
        asked.append("claude")
        raise RuntimeError("claude usage limit")

    async def copilot(prompt, **k):
        asked.append("copilot")
        return "CODE"

    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: None)
    monkeypatch.setattr(claude_cli, "one_shot", claude)
    monkeypatch.setattr(copilot_cli, "one_shot", copilot)
    assert asyncio.run(memory.quick_verdict("CODE or PERSON?")) == "CODE"
    assert asked == ["claude", "copilot"]
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: "PERSON")
    assert asyncio.run(memory.quick_verdict("CODE or PERSON?")) == "PERSON", "local first when up"


def test_a_call_answers_aloud_without_lm_studio(monkeypatch):
    from app import call_brain, memory

    async def verdict(prompt, max_tokens=8, timeout=25):
        return "CODE"

    monkeypatch.setattr(call_brain, "CONFIRM_SPEECH", True)
    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: None)
    monkeypatch.setattr(memory, "quick_verdict", verdict)
    assert asyncio.run(call_brain.confident("how does the amend flow handle the ATA date?"))


def test_reading_teams_falls_to_the_local_model_before_the_rules(monkeypatch):
    from app import understand
    used: list[str] = []

    async def call(text, model_name=""):
        used.append(model_name or "first")
        if model_name == understand.LOCAL:
            return '{"threads": [{"id": "t1", "state": "ask", "need": "check booking"}]}'
        raise RuntimeError("claude usage limit")

    monkeypatch.setattr(understand, "_call", call)
    got = asyncio.run(understand._ladder([{"id": "t1", "new": ["can u check H69 booking"]}], []))
    assert "t1" in got and used[-1] == understand.LOCAL


def test_a_call_knows_every_open_pr_of_his_by_what_it_changed(monkeypatch):
    from app import conversation, prname
    monkeypatch.setattr(prname, "his_open_prs", lambda limit=15: (
        "• AP PR 1252 — Derive ATA/ATD order-level references from TMS execution events\n"
        "• empv3-tenant-intake PR 44342 — Feature/topic refresh telikos prod"))
    brief = conversation.with_history("Vinish Kumar", "find out what he wanted")
    assert "AP PR 1252 — Derive ATA/ATD" in brief and "never pick the nearest one" in brief


def test_an_ask_to_him_is_still_caught_when_the_local_model_is_closed(monkeypatch):
    from app import memory, triage

    async def verdict(prompt, max_tokens=8, timeout=25):
        return "ACT"

    monkeypatch.setattr(memory, "local_llm_complete", lambda *a, **k: None)
    monkeypatch.setattr(memory, "quick_verdict", verdict)
    v = triage.Verdict(False, "addressed to you, no ask", "Rajendra: H69LMCN6KZY")
    assert asyncio.run(triage.refine(v, "Rajendra Kumar", "H69LMCN6KZY")).action
    quiet = triage.Verdict(False, "no ask detected", "deploy done")
    assert not asyncio.run(triage.refine(quiet, "Sumith", "deploy done")).action, \
        "only what is addressed to him is worth a paid look"


# --- the page reports; Asta reads only what changed ------------------------------------

@pytest.fixture
def rail(monkeypatch):
    chat_watch._RAIL.update(order=[], unread=set(), at=0.0, mentioned=False)
    chat_watch._HOT.clear()
    chat_watch._RESTORED.clear()
    chat_watch._CHECKED.clear()
    chat_watch._BACKOFF.clear()
    chat_watch._EVENT["ev"] = None
    tb.MENTIONED["ev"] = None
    yield
    chat_watch._RAIL.update(order=[], unread=set(), at=0.0, mentioned=False)
    chat_watch._HOT.clear()
    chat_watch._RESTORED.clear()


def test_after_a_restart_every_unread_chat_is_read_first(rail):
    """Offline all night, back online: what is bold on the rail is read now."""
    hot = chat_watch.on_rail(["*Vinish Kumar", "Komal Jayswal", "*Telikos SCP Internal Tech"])
    assert hot == ["Telikos SCP Internal Tech", "Vinish Kumar"]


def test_a_chat_turning_unread_or_moving_up_is_read_and_nothing_else(rail):
    chat_watch.on_rail(["Vinish Kumar", "Komal Jayswal", "Rajendra Kumar"])
    chat_watch.take_hot()
    assert chat_watch.on_rail(["Vinish Kumar", "*Komal Jayswal", "Rajendra Kumar"]) == ["Komal Jayswal"]
    chat_watch.take_hot()
    hot = chat_watch.on_rail(["Rajendra Kumar", "Vinish Kumar", "*Komal Jayswal"])
    assert hot == ["Rajendra Kumar"], "moved to the top: read; the rest only shifted down"
    assert chat_watch.on_rail(["Rajendra Kumar", "Vinish Kumar", "*Komal Jayswal"]) == [], \
        "the same picture again (the heartbeat) is not news"


def test_a_muted_group_marked_unread_is_read(rail):
    """1 Oct: the minute sweep never saw these — marking unread moves nothing."""
    chat_watch.on_rail(["Vinish Kumar", "UAT Code Promotion - Sept'Release"])
    assert chat_watch.on_rail(["Vinish Kumar", "*UAT Code Promotion - Sept'Release"]) == \
        ["UAT Code Promotion - Sept'Release"]


def test_asta_giving_a_chat_back_unread_is_not_news_but_is_rechecked(rail):
    chat_watch.on_rail(["Vinish Kumar", "Komal Jayswal"])
    chat_watch.take_hot()
    chat_watch.note_restored("Komal Jayswal")
    assert chat_watch.on_rail(["Vinish Kumar", "*Komal Jayswal"]) == [], "our own Mark as unread"
    chat_watch._RESTORED["Komal Jayswal"] -= 120
    assert "Komal Jayswal" in chat_watch.take_hot(), \
        "still unread a minute later: a second message there changes nothing on the rail"
    chat_watch.on_rail(["Vinish Kumar", "Komal Jayswal"])
    assert "Komal Jayswal" not in chat_watch._restored_due(time.time() + 120), \
        "he read it himself: no more rechecks"


def test_furniture_and_his_own_note_chat_are_never_opened(rail):
    chat_watch.on_rail(["Vinish Kumar"])
    hot = chat_watch.on_rail(["*Arunkumar K (You)", "*Copilot", "Vinish Kumar"])
    assert hot == []


def test_the_watch_loop_falls_back_to_every_minute_when_the_page_goes_quiet(rail):
    assert not chat_watch.rail_alive()
    chat_watch.on_rail(["Vinish Kumar"])
    assert chat_watch.rail_alive()
    assert not chat_watch.rail_alive(time.time() + chat_watch.RAIL_SILENT_SECONDS + 1)


def test_a_report_wakes_the_loop_within_seconds(rail, monkeypatch):
    monkeypatch.setattr(chat_watch, "HOT_DEBOUNCE_SECONDS", 0.0)

    async def go():
        waiting = asyncio.ensure_future(chat_watch._wait_for_work(30))
        await asyncio.sleep(0.05)
        chat_watch.on_rail(["*Vinish Kumar"])
        return await asyncio.wait_for(waiting, 2)

    assert asyncio.run(go()) == "hot"


def test_a_hot_sweep_opens_only_the_chats_reported(monkeypatch):
    opened: list[str] = []

    async def new_in(chat, advance=True):
        opened.append(chat)
        return []

    async def candidates():
        raise AssertionError("a hot sweep must not read the whole rail")

    monkeypatch.setattr(chat_watch, "new_in", new_in)
    monkeypatch.setattr(chat_watch, "candidates", candidates)
    asyncio.run(chat_watch.sweep(None, only=["Vinish Kumar"]))
    assert opened == ["Vinish Kumar"]


def test_the_watcher_reports_chats_not_channels_and_says_who_is_unread():
    js = tb.RAIL_WATCH_JS
    assert 'aria-level="1"' in js and "teams and channels" in js, "channels are a section, skipped"
    assert "fontWeight" in js and ">= 600" in js, "unread is the name in bold"
    assert "astaRail" in js and "60000" in js, "it reports, and it has a heartbeat"


def test_a_browser_grown_past_its_limit_is_recycled(monkeypatch):
    import time as _t
    size = {"mb": 3200.0}
    monkeypatch.setattr(tb, "profile_mb", lambda: size["mb"])
    monkeypatch.setattr(tb, "in_a_call", lambda: False)
    monkeypatch.setitem(tb._POOL, "born", _t.time() - 600)
    tb._SIZE.update(at=0.0, mb=0.0, base=0.0, born=0.0)
    # 2 Oct: a fresh Teams is ~3.2 GB — that is its normal size, not a reason to relaunch.
    assert not tb._too_big()
    for mb, want in ((5900.0, False), (6500.0, True)):
        size["mb"] = mb
        tb._SIZE["at"] = 0.0
        assert tb._too_big() is want, mb
    tb._SIZE.update(at=0.0, mb=0.0, base=0.0, born=0.0)
    monkeypatch.setattr(tb, "in_a_call", lambda: True)
    assert not tb._too_big(), "never in the middle of a call"


def test_a_browser_still_loading_is_never_judged_by_size(monkeypatch):
    import time as _t
    monkeypatch.setattr(tb, "profile_mb", lambda: 9000.0)
    monkeypatch.setattr(tb, "in_a_call", lambda: False)
    monkeypatch.setitem(tb._POOL, "born", _t.time() - 20)
    tb._SIZE.update(at=0.0, mb=0.0, base=0.0, born=0.0)
    assert not tb._too_big()


def test_placeholder_rows_with_no_name_are_never_read():
    chat_watch._RAIL.update(order=[], unread=set(), at=0.0)
    chat_watch._HOT.clear()
    hot = chat_watch.on_rail(["*\u200b", "* ", "*BEP_Telikos : Defect Triage"], now=1000.0)
    assert set(hot) == {"BEP_Telikos : Defect Triage"}
    with pytest.raises(tb.NotFound):
        tb._refuse_blank("\u200b")
    chat_watch._RAIL.update(order=[], unread=set(), at=0.0)
    chat_watch._HOT.clear()


def test_every_script_asta_puts_into_a_page_compiles(tmp_path):
    """1 Oct: the rail watcher shipped with a line break where "\\n" belonged.
    The browser rejected the whole script, so nothing reported, and only the
    fallback sweep kept Teams read. A substring test cannot see that; a parser can."""
    import importlib
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    checked = 0
    for mod_name in ("teams_bridge", "incoming", "chat_watch", "meetings", "call_rtc",
                     "call_screen", "call_rehearsal", "voice"):
        try:
            mod = importlib.import_module(f"app.{mod_name}")
        except Exception:                                      # noqa: BLE001
            continue
        for name in dir(mod):
            src = getattr(mod, name)
            if not (name.endswith("_JS") or name in ("_CHAT_ROWS", "_CHAT_ROWS_FULL", "_MARK_RAIL_ROW")) \
                    or not isinstance(src, str) or not src.strip().startswith(("(", "async")):
                continue
            f = tmp_path / f"{mod_name}_{name}.js"
            f.write_text("const f = (" + src.strip().rstrip(";") + ");\n")
            out = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
            assert out.returncode == 0, f"{mod_name}.{name} does not compile:\n{out.stderr[:400]}"
            checked += 1
    assert checked >= 5


# --- follow-through: what Asta promised, it does, and it remembers ---------------------

def test_a_scheduled_message_replaces_the_earlier_ones_for_the_same_thing():
    from app import reminders
    due = time.time() + 3 * 86400
    a = store.create_reminder('Send Vinish: "bro can you merge PR 1429"', due, "")
    b = store.create_reminder("Send Vinish: merge booking PR 1429 and AP PR 1252", due + 60, "")
    other = store.create_reminder("Standup", due, "")
    s = reminders.schedule_send("Vinish Kumar", "bro can u merge these when u get a chance", due)
    pending = {r["id"] for r in store.list_reminders()}
    assert s["id"] in pending and other["id"] in pending
    assert a["id"] not in pending and b["id"] not in pending, "one message per person per time"
    s2 = reminders.schedule_send("Vinish Kumar", "bro can u merge both", due)
    assert s["id"] not in {r["id"] for r in store.list_reminders()} and s2["id"]


def test_an_approved_scheduled_message_is_sent_when_due(monkeypatch, told):
    from app import reminders
    sent: list[dict] = []

    async def run(op):
        sent.append(op)
        return "✅ Sent to Vinish Kumar."

    monkeypatch.setattr(ops, "run", run)
    reminders.schedule_send("Vinish Kumar", "bro can u merge these", time.time() - 5, approved=True)
    assert asyncio.run(reminders.fire_due()) == 1
    assert sent and sent[0]["args"] == {"to": "Vinish Kumar", "text": "bro can u merge these",
                                        "to_group": False}
    assert any("Scheduled message sent" in m for m in told)


def test_an_unapproved_one_or_one_to_his_manager_waits_for_his_yes(monkeypatch, told, above):
    from app import reminders

    async def run(op):
        raise AssertionError("sent without his yes")

    monkeypatch.setattr(ops, "run", run)
    store.kv_set("wa_conversation", _conv()["id"])
    reminders.schedule_send("Praveen Kumar", "status: both PRs merged", time.time() - 5, approved=True)
    asyncio.run(reminders.fire_due())
    staged = loop.awaiting(answers.phone_conversation())
    assert staged and staged["to"] == "Praveen Kumar"


def test_the_tool_schedules_and_says_whether_it_needs_his_yes(monkeypatch):
    from app import agent
    cid = _conv()["id"]
    monkeypatch.setattr(tasks, "current_conversation", lambda: cid)
    monkeypatch.setattr(capabilities, "said_this_turn",
                        lambda: "keep reminder and notify vinish on monday to get merged")
    out = asyncio.run(agent.send_later("Vinish Kumar", "bro can u merge these",
                                             "2099-10-05T09:00"))
    assert out.startswith("Scheduled #") and "sends by itself" in out
    assert "Not scheduled" in asyncio.run(agent.send_later("Vinish Kumar", "x", "2001-01-01T09:00"))


def test_a_claim_that_something_was_scheduled_is_checked():
    t0 = time.time()
    lie = "Scheduled — the merge-nudge to Vinish goes out at 14:32 today, no further confirmation needed."
    assert main.unproven_schedule(lie, t0)
    assert not main.unproven_schedule("I'll check the logs and come back.", t0)
    store.create_reminder("SEND: {}", t0 + 3600, "")
    assert not main.unproven_schedule(lie, t0), "a reminder was created this turn"


def test_every_turn_starts_from_his_open_work(monkeypatch):
    from app import copilot_cli, prname
    monkeypatch.setattr(prname, "his_open_prs", lambda limit=15: (
        "• booking PR 1429 — fail RFP on missing mandatory fields\n• AP PR 1252 — Derive ATA/ATD"))
    store.create_reminder('SEND: {"to": "Vinish Kumar", "text": "bro merge both", "approved": true}',
                          time.time() + 3600, "")
    work = copilot_cli.open_work()
    assert "AP PR 1252" in work and "never a colleague's PR" in work
    assert "message to Vinish Kumar (sends by itself)" in work
    assert "send_later" in " ".join(__import__("app.tool_index", fromlist=["x"]).required_for(
        "notify vinish on monday to get both merged"))


# --- WhatsApp: 👀, "typing…", ✅ — no filler message ----------------------------------

def test_the_answer_after_a_long_turn_is_marked_as_the_answer(monkeypatch):
    pushed: list[tuple] = []

    async def wa(text, done=False):
        pushed.append((text, done))
        return True

    sink = main.HybridSink(wa, "c1")
    sink.handoff()
    asyncio.run(sink.send({"type": "delta", "text": "No booking H69LMCN6KZY in UAT either."}))
    asyncio.run(sink.send({"type": "done"}))
    assert pushed == [("No booking H69LMCN6KZY in UAT either.", True)]


def test_a_long_turn_that_ends_with_nothing_to_say_still_ticks_his_message(monkeypatch):
    from app import notify
    ticked: list[bool] = []

    async def done(ok=True):
        ticked.append(ok)
        return True

    async def wa(text, done=False):
        raise AssertionError("nothing to send")

    monkeypatch.setattr(notify, "wa_done", done)
    sink = main.HybridSink(wa, "c1")
    sink.handoff()
    asyncio.run(sink.close())
    assert ticked == [True]
    asyncio.run(sink.close())
    assert ticked == [True], "once"


def test_the_bridge_shows_working_without_a_message():
    import pathlib
    import shutil
    import subprocess
    js = (pathlib.Path(main.__file__).resolve().parents[1] / "whatsapp" / "bridge.js").read_text()
    assert 'sendPresenceUpdate("composing"' in js and '"👀"' in js and '"✅"' in js
    assert 'req.url === "/done"' in js and "data.working" in js
    node = shutil.which("node")
    if node:
        path = pathlib.Path(main.__file__).resolve().parents[1] / "whatsapp" / "bridge.js"
        assert subprocess.run([node, "--check", str(path)], capture_output=True).returncode == 0


# --- 2 Oct: a blank chat name threw the browser away 500 times ---------------------------

def test_a_blank_chat_name_is_refused_without_touching_the_browser(monkeypatch):
    def no_page():
        raise AssertionError("the browser was opened for a blank name")

    monkeypatch.setattr(tb, "teams_page", no_page)
    for blank in ("", "   "):
        with pytest.raises(tb.NotFound):
            asyncio.run(tb.read_history(blank))


def test_an_ambiguous_name_is_a_clean_refusal_not_a_broken_browser():
    assert issubclass(tb.Ambiguous, tb.NotFound) and issubclass(tb.Ambiguous, RuntimeError)
    matches = [{"aria": "Group chat Alpha", "text": "Alpha one", "i": 0},
               {"aria": "Group chat Alpha", "text": "Alpha two", "i": 1}]
    with pytest.raises(tb.Ambiguous, match="Refusing to guess"):
        tb._one_of(matches, "Alpha", "groups", set())


def test_a_muted_chat_row_starting_with_an_empty_line_still_has_its_name():
    # The row's first text line can be empty (the muted icon); its name is the
    # first line with words, in every script that reads the rail.
    for js in (tb.RAIL_WATCH_JS, tb._ROW_UNREAD_JS, tb._MARK_RAIL_ROW, tb._CHAT_ROWS):
        assert "find(Boolean)" in js
    assert "const name = (n.innerText || '').split('\\n').map(t => t.trim()).find(Boolean)" in tb.RAIL_WATCH_JS


def test_what_the_page_last_reported_is_kept_for_a_look():
    import json as _json
    chat_watch._RAIL.update(order=[], unread=set(), at=0.0)
    chat_watch.on_rail(["*Muted group one", "Vinish Kumar"], now=1000.0)
    seen = _json.loads(store.kv_get("rail_last"))
    assert seen["rows"] == ["*Muted group one", "Vinish Kumar"]
    chat_watch._RAIL.update(order=[], unread=set(), at=0.0)
    chat_watch._HOT.clear()



# --- 2 Oct: a chat left unread was re-opened every minute, forever ----------------------

LIST = ["Arunkumar K (You)", "BEP_Telikos : Defect Triage", "Vinish Kumar", "Team Booking",
        "AP Changes Related to Soft Closure", "Rajendra Kumar"]


def _restore(chat, unread_list, at):
    chat_watch.on_rail(unread_list)
    chat_watch.take_hot()
    chat_watch._RESTORED[chat] = at
    chat_watch._CHECKED[chat] = at


def test_two_days_unread_at_the_top_is_three_re_reads_not_three_thousand(rail):
    t0 = time.time()
    rows = ["*" + c if c == "BEP_Telikos : Defect Triage" else c for c in LIST]
    _restore("BEP_Telikos : Defect Triage", rows, t0)
    opens = 0
    for minute in range(1, 2 * 24 * 60):
        now = t0 + minute * 60
        due = chat_watch._restored_due(now)
        if due:
            opens += 1
            for c in due:
                chat_watch._BACKOFF[c] = chat_watch._BACKOFF.get(c, 0) + 1
                chat_watch._CHECKED[c] = now
    assert opens == 3, "1, 2 and 4 minutes after — then the full sweep's look at the top"


def test_a_restored_chat_lower_down_is_not_re_read_a_new_message_moves_it_up(rail):
    t0 = time.time() - 300
    rows = ["*" + c if c == "Rajendra Kumar" else c for c in LIST]
    _restore("Rajendra Kumar", rows, t0)
    assert chat_watch._restored_due(t0 + 3600) == []
    # His next message moves the chat to the top: the watcher reports it at once.
    moved = ["*Rajendra Kumar"] + [c for c in LIST if c != "Rajendra Kumar"]
    assert "Rajendra Kumar" in chat_watch.on_rail(moved)


def test_news_in_a_restored_chat_starts_the_re_reads_again(rail):
    t0 = time.time()
    rows = ["*" + c if c == "BEP_Telikos : Defect Triage" else c for c in LIST]
    _restore("BEP_Telikos : Defect Triage", rows, t0)
    chat_watch._BACKOFF["BEP_Telikos : Defect Triage"] = 3
    assert chat_watch._restored_due(t0 + 3600) == []
    chat_watch.news_in("BEP_Telikos : Defect Triage")
    assert chat_watch._restored_due(t0 + 61) == ["BEP_Telikos : Defect Triage"]


def test_woken_for_a_re_read_that_is_not_due_is_not_a_full_sweep(rail, monkeypatch):
    from app import wake

    async def sleep(seconds):
        await asyncio.sleep(0.01)
        return False

    monkeypatch.setattr(wake, "sleep", sleep)
    t0 = time.time()
    rows = ["*" + c if c == "BEP_Telikos : Defect Triage" else c for c in LIST]
    _restore("BEP_Telikos : Defect Triage", rows, t0)
    # Due in a minute: the loop wakes then, finds nothing due yet (clock not moved).
    assert asyncio.run(chat_watch._wait_for_work(300)) == "idle"


def test_a_group_is_left_read_and_a_persons_chat_given_back_unread():
    vin = [{"sender": "Vinish Kumar", "text": "Bro, tomorrow 10"},
           {"sender": "Arunkumar K", "text": "ok"}]
    room = [{"sender": "Sonal Pathak", "text": "ready"}, {"sender": "Roshan Kumar Thakur", "text": "fyi"}]
    assert tb.one_to_one("Vinish Kumar", vin)
    assert not tb.one_to_one("BEP_Telikos : Defect Triage", room)
    assert not tb.one_to_one("Fake Internal Team", room)
    assert not tb.one_to_one("Shabda Anubhav, Vinish, +2", vin)
    src = (Path(tb.__file__)).read_text()
    assert "if was_unread and one_to_one(chat, rows) and await mark_unread(page, chat):" in src


def test_teams_painting_its_icon_bar_is_not_six_chats_moving(rail):
    names = ["Arunkumar K (You)", "Vinish Kumar", "*BEP_Telikos : Defect Triage", "Daily deployment slot",
             "Team Booking and Execution", "AP Changes Related to Soft Closure", "Nakka Harika",
             "Fake Internal Team", "Palikala Divya Maheswari", "ATA/ATD changes with Billing"]
    chat_watch.on_rail(names)
    chat_watch.take_hot()
    assert chat_watch.on_rail(["", "", "", "", ""]) == []
    assert chat_watch.on_rail(names) == [], "the real list again: nothing moved"


def test_an_ack_in_a_1to1_is_marked_unread_for_him_and_a_group_is_not(monkeypatch):
    sent: list[str] = []
    marked: list[str] = []

    async def send(chat, text, allow_group=False):
        sent.append(chat)
        return chat

    async def give_back(chat):
        marked.append(chat)
        return True

    async def not_yet(chat, since):
        return False

    monkeypatch.setattr(tb, "send_message", send)
    monkeypatch.setattr(tb, "give_back_unread", give_back)
    monkeypatch.setattr(chat_watch, "he_replied_since", not_yet)
    chat_watch._RESTORED.clear()
    assert asyncio.run(chat_watch._say("Vinish Kumar", "hi Vinish, yes tell me")) is True
    assert marked == ["Vinish Kumar"] and "Vinish Kumar" in chat_watch._RESTORED
    monkeypatch.setenv("ASTA_GROUP_SILENT", "0")
    asyncio.run(chat_watch._say("Team Booking and Execution", "noted", group=True))
    assert marked == ["Vinish Kumar"], "a group is never marked by Asta"
    chat_watch._RESTORED.clear()


def test_every_inline_page_script_compiles_too(tmp_path):
    """2 Oct: the selector check's own rail script had the same line break where
    "\\n" belonged — written inline in `page.evaluate(...)`, so the test above
    never saw it, and the check silently skipped four of its seven selectors."""
    import ast
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    root = Path(__file__).resolve().parents[1] / "app"
    bad: list[str] = []
    checked = 0
    for py in sorted(root.rglob("*.py")):
        tree = ast.parse(py.read_text(), filename=str(py))
        for node_ in ast.walk(tree):
            if not (isinstance(node_, ast.Call) and isinstance(node_.func, ast.Attribute)
                    and node_.func.attr in ("evaluate", "evaluate_handle", "add_init_script",
                                            "wait_for_function")
                    and node_.args and isinstance(node_.args[0], ast.Constant)
                    and isinstance(node_.args[0].value, str)):
                continue
            src = node_.args[0].value.strip()
            if not src.startswith(("(", "async", "function")):
                continue
            f = tmp_path / f"{py.stem}_{node_.lineno}.js"
            f.write_text("const f = (" + src.rstrip(";") + ");\n")
            out = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
            checked += 1
            if out.returncode:
                bad.append(f"{py.relative_to(root.parent)}:{node_.lineno}: {out.stderr.strip().splitlines()[-1][:120]}")
    assert checked >= 20
    assert not bad, "page scripts that do not compile:\n" + "\n".join(bad)
