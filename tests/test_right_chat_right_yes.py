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


def test_an_answer_he_already_gave_is_not_staged_again(monkeypatch, told):
    now = time.time()
    chat = "Shabda Anubhav, Vinish, +2"
    store.kv_set("wa_conversation", _conv()["id"])
    store.save_teams_messages([{"key": "s1", "chat": chat, "sender": "Shabda Anubhav Dev",
                                "text": "please review the email service issue", "sent_at": now - 240,
                                "stamp": ""}])

    async def read(c, limit=0, max_scrolls=0, since=None):
        return [{"sender": "Arunkumar K", "text": "will check and update and approve",
                 "sent_at": now - 120}]

    monkeypatch.setattr(tb, "enabled", lambda: True)
    monkeypatch.setattr(tb, "read_history", read)
    assert asyncio.run(answers.present(who="Shabda Anubhav Dev", need="review email CT", chat=chat,
                                       group=True, analysis="Root cause: the 3.2.25 bump",
                                       reply="checked, it's the bump")) is True
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
