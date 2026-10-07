"""A colleague hands over a PR as the reference for the same change elsewhere.

7 Oct, Vinish, 17:12–17:22:

    Vinish: Can you please use this and work from booking side <AP PR 1260>
    Asta:   (reads it as a review) "...you mean review this, or replicate it?"
    Vinish: Incorrect
    Asta:   (checks the same two readings) "...no gap there either"
    Arun:   that was for the job reopen/closure ticket... planning the same in booking
    Vinish: It will come as a work process name, like job closure. Same for job open.
    Asta:   (a run started before that) "...so I stop guessing?"
    Vinish: Bro, you are talking or Asta?
    Asta:   "Yeah bro, it's me, Arun — not Asta."

AP PR 1260 sends job open the way job closure already arrives; booking handles
JOB_CLOSURE explicitly (WorkProcessNameEnum, its milestone, the infra service)
and has nothing for job open. That difference was the whole job.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from test_conversation_threads import _msg, rail  # noqa: F401
from test_round4_phase_d import phone  # noqa: F401

from app import store

LINK = "https://github.com/Maersk-Global/telikos-activityplanworkflow-service/pull/1260/changes"
ASK = f"Can you please use this and work from booking side {LINK}"
CHAT = "Vinish Kumar"
TID = "teams:Vinish Kumar"


# --- A. what it is ---------------------------------------------------------------------

def test_a_reference_pr_for_another_side_is_work_not_a_review():
    from app import responder
    assert responder.what_it_asks(ASK) == "port"
    found = responder.counterpart(ASK)
    assert found["side"] == "booking" and found["number"] == "1260"
    assert responder.title_for("port", CHAT, ASK) == "Vinish Kumar asked: carry AP PR 1260 to booking"


@pytest.mark.parametrize("text, kind", [
    ("please review https://github.com/o/booking/pull/1459", "review_request"),
    ("I left comments on your PR https://github.com/o/r/pull/5", "pr_review"),
    ("take this as reference https://github.com/o/ap/pull/12 and implement it in email service", "port"),
    ("do the same in booking for https://github.com/o/ap/pull/12", "port"),
    ("use this and work from booking side", ""),       # no PR: nothing to carry
])
def test_only_a_pr_handed_over_as_reference_is_a_port(text, kind):
    from app import responder
    got = responder.what_it_asks(text)
    assert (got == "port") == (kind == "port")
    if kind and kind != "port":
        assert got == kind


# --- B. how it is investigated ----------------------------------------------------------

def test_the_port_brief_compares_with_the_existing_sibling_case():
    from app import responder
    brief = responder.brief_for("port", CHAT, ASK)
    assert "not a review" in brief and "sibling" in brief
    assert "JOB_CLOSURE" in brief, "the worked example names the existing case to mirror"
    assert "only if the sibling takes that same path" in brief.replace('"', "")


def test_a_port_is_investigated_first_not_offered_as_a_blind_plan(rail):
    rail.rows[CHAT] = [_msg(CHAT, ASK)]
    rail.script[TID] = {"state": "ask", "need": "Use PR 1260 and work from booking side",
                        "closing_confidence": 0.05, "summary": "carry AP 1260 to booking",
                        "entities": [], "work": "code"}
    rail.sweep()
    assert len(rail.asked) == 1, "one investigation, read with the port method"
    from app import offers
    assert offers.pending() is None, "the plan is offered with the findings, not before them"


def test_the_ack_says_what_is_being_checked():
    from app import steward
    line = steward.ack_line(CHAT, ASK)
    assert line.startswith("Got it, checking what booking needs")
    assert "PR details" not in line


def test_a_finished_port_offers_the_plan_with_the_reference_and_method(phone, monkeypatch):
    from app import answers, offers
    monkeypatch.setattr(answers, "moved_on", lambda chat, since: "")
    t = store.create_task("Vinish Kumar asked: carry AP PR 1260 to booking", "analysis", "p", None)
    store.update_task(t["id"], status="done", finished_at=time.time())
    answers.remember_meta(t["id"], who=CHAT, need="carry AP PR 1260 to booking", chat=CHAT,
                          group=False, thread=TID, source_text=ASK, kind="port")
    result = ("ANALYSIS:\n1. AP sends JOB_OPENED as workProcessName (WorkFlowUtility.java:88).\n"
              "2. booking handles JOB_CLOSURE in WorkProcessNameEnum.java:71 — nothing for JOB_OPENED.\n\n"
              "REPLY:\nGot it — job open comes as workProcessName JOB_OPENED like job closure, "
              "so booking needs the same handling as JOB_CLOSURE.")
    assert asyncio.run(answers.present_task(t["id"], store.get_task(t["id"]), result))
    queued = [q for q in answers._load_queue() if q.get("type") == "plan"]
    shown = offers.pending()
    plan = queued[0] if queued else None
    assert plan or shown, "the plan is offered once the answer is in front of him"
    if plan:
        assert plan["reference"].endswith("/pull/1260") and "JOB_CLOSURE" in plan["summary"]
    else:
        assert "/pull/1260" in shown.action and "sibling" in shown.action


# --- C. a correction changes the reading ------------------------------------------------

def test_a_correction_of_a_port_is_worked_as_a_port_and_names_what_was_rejected(monkeypatch):
    from app import answers, responder, tasks
    monkeypatch.setenv("ASTA_RESPOND", "1")
    old = store.create_task("Vinish Kumar asked: Can you please use this…", "analysis", "p", None)
    store.update_task(old["id"], status="done", finished_at=time.time(), result=(
        "Still running.\n\nANALYSIS:\n1. booking-domain part is an 8-line removal.\n\n"
        "REPLY:\nYou mean review this, or replicate it in booking-service?"))
    answers.remember_meta(old["id"], who=CHAT, need="work from booking side", chat=CHAT,
                          group=False, thread=TID, source_text=ASK, kind="ask")
    monkeypatch.setattr(responder, "familiar", lambda _: (True, "previous work"))
    spawned = []

    def spawn(title, prompt, kind, workspace, **kwargs):
        spawned.append((title, prompt))
        return store.create_task(title, kind, prompt, workspace)

    monkeypatch.setattr(tasks, "spawn", spawn)
    task = responder.respond("teams-chat", CHAT, "Correction to the answer for task: Incorrect",
                             key="corr", priority=1, reply_to=CHAT, correction_of=old["id"],
                             source_text="Incorrect")
    assert task and answers._meta(task["id"])["ask_kind"] == "port"
    title, prompt = spawned[0]
    assert "carry" in title
    assert "do NOT re-check it" in prompt and "never a choice between your own guesses" in prompt
    assert "REPLY they rejected: You mean review this" in prompt
    assert "Still running" not in prompt, "the disputed answer is its report, not its chatter"


def test_reply_rules_forbid_either_or_guesses_and_process_talk():
    from app import answers
    rider = answers.REPLY_FORMAT
    assert "never a choice" in rider and "so I stop" in rider and "Nothing after the REPLY" in rider


# --- D. no automatic replies after a correction -----------------------------------------

def _auto_allowed(monkeypatch, sent):
    from app import authority, chat_watch

    async def send_reply(target, text):
        sent.append((target, text))
        return f"✅ Sent to {target}."

    async def not_replied(*a, **k):
        return False
    monkeypatch.setattr(authority, "auto_reply_to", lambda target, group=False: True)
    monkeypatch.setattr(authority, "send_reply", send_reply)
    monkeypatch.setattr(chat_watch, "he_replied_since", not_replied)


def test_after_a_correction_the_next_reply_waits_for_arun(phone, monkeypatch):
    from app import answers, loop
    sent = []
    _auto_allowed(monkeypatch, sent)
    answers.mark_corrected(TID)
    intent = {"to": CHAT, "what": "Got it, job open like job closure.", "thread": TID}
    assert not asyncio.run(answers._deliver_automatic_reply(intent))
    assert not sent
    answers.sent({**intent, "type": "answer"}, by_arun=False)
    assert answers.held_after_correction(TID), "an automatic send never lifts the hold"
    answers.sent({**intent, "type": "answer"})
    assert not answers.held_after_correction(TID), "his own approved reply does"
    assert asyncio.run(answers._deliver_automatic_reply(intent)) and sent
    loop.clear_awaiting(phone["cid"])


def test_a_correction_in_the_sweep_marks_the_thread(rail, phone):
    from app import answers
    task = store.create_task("Vinish asked", "analysis", "p", None)
    store.update_task(task["id"], status="done", result="ANALYSIS:\nx\n\nREPLY:\ny",
                      finished_at=time.time() - 30)
    answers.remember_meta(task["id"], who=CHAT, need="x", chat=CHAT, group=False,
                          thread=TID, source_text=ASK)
    rail.rows[CHAT] = [_msg(CHAT, "Incorrect")]
    rail.script[TID] = {"state": "ask", "need": "", "closing_confidence": 0.1,
                        "summary": "rejected", "entities": []}
    rail.sweep()
    assert answers.held_after_correction(TID)


# --- E. one thread, one piece of work ---------------------------------------------------

def test_a_new_message_supersedes_the_threads_running_answer(rail, monkeypatch):
    from app import answers, tasks
    old = store.create_task("Vinish asked", "analysis", "p", None)
    store.update_task(old["id"], status="running")
    answers.remember_meta(old["id"], who=CHAT, need="x", chat=CHAT, group=False, thread=TID)
    cancelled = []

    async def cancel(task_id, status="cancelled", why=""):
        cancelled.append((task_id, status))
        store.update_task(task_id, status=status)
        return True
    monkeypatch.setattr(tasks, "cancel", cancel)
    rail.rows[CHAT] = [_msg(CHAT, "It will come as a work process name, like job closure. "
                                  "Same way we have to do it for job open.")]
    rail.script[TID] = {"state": "ask", "need": "job open like job closure",
                        "closing_confidence": 0.05, "summary": "explained", "entities": [],
                        "work": "check"}
    rail.sweep()
    assert cancelled == [(old["id"], "superseded")]
    assert len(rail.asked) == 1 and "job open" in rail.asked[0]["text"]


def test_a_live_code_task_on_the_same_pr_takes_their_words(rail, phone, monkeypatch):
    from app import tasks
    code = store.create_task("Plan booking-service implementation of AP PR 1260", "code",
                             f"Take {LINK.removesuffix('/changes')} as reference…", "booking")
    store.update_task(code["id"], status="running")
    store.save_teams_messages([{"key": "link", "chat": CHAT, "sender": CHAT, "text": ASK,
                                "sent_at": time.time() - 300, "stamp": ""}])
    rail.rows[CHAT] = [_msg(CHAT, f"It will come as a work process name for {LINK}, "
                                  "like job closure. Same for job open.")]
    rail.script[TID] = {"state": "ask", "need": "job open", "closing_confidence": 0.05,
                        "summary": "explained", "entities": [], "work": "code"}
    rail.sweep()
    assert not rail.asked, "no second investigation beside the code task"
    assert "work process name" in (store.kv_get(f"task_addenda:{code['id']}") or "")
    assert any(f"#{code['id']}" in p["text"] for p in phone["pushed"])


def test_a_task_does_not_spawn_its_own_review_or_investigation(monkeypatch):
    from app import agent, capabilities, review

    async def brief(pr, workspace, repo=""):
        return "THE DIFF", {"number": "1260", "title": "t", "url": LINK}
    monkeypatch.setattr(review, "brief", brief)
    token = capabilities.FROM_TASK.set("258")
    try:
        out = asyncio.run(agent.review_pr(LINK))
        assert "do this review yourself" in out and "THE DIFF" in out
        assert "Do this reading yourself" in agent.delegate_task("Review AP 1260", "x")
    finally:
        capabilities.FROM_TASK.reset(token)
    assert not store.list_tasks(limit=5)


# --- F. a conversation that moved on ----------------------------------------------------

def _finished(result, source=ASK):
    from app import answers
    t = store.create_task("Vinish asked", "analysis", "p", None)
    store.update_task(t["id"], status="done", finished_at=time.time())
    answers.remember_meta(t["id"], who=CHAT, need="x", chat=CHAT, group=False,
                          thread=TID, source_text=source)
    return store.get_task(t["id"])


def _row(sender, text, at, key):
    store.save_teams_messages([{"key": key, "chat": CHAT, "sender": sender, "text": text,
                                "sent_at": at, "stamp": ""}])


def test_an_answer_overtaken_by_their_next_message_is_not_sent(phone, monkeypatch):
    from app import answers, loop
    sent = []
    _auto_allowed(monkeypatch, sent)
    t = _finished("")
    _row(CHAT, "It will come as a work process name, bro", float(t["created_at"]) + 30, "late")
    result = "ANALYSIS:\nold reading\n\nREPLY:\nCan you point me to the exact file you mean?"
    assert asyncio.run(answers.present_task(t["id"], t, result))
    assert not sent and loop.awaiting(phone["cid"]) is None and not phone["pushed"]


def test_an_answer_for_a_chat_arun_stepped_into_reaches_only_him(phone, monkeypatch):
    from app import answers, loop
    sent = []
    _auto_allowed(monkeypatch, sent)
    t = _finished("")
    _row("Arunkumar K", "bro that was for the job reopen ticket", float(t["created_at"]) + 30, "his")
    result = "ANALYSIS:\nbooking lacks JOB_OPENED\n\nREPLY:\nGot it."
    assert asyncio.run(answers.present_task(t["id"], t, result))
    assert not sent and loop.awaiting(phone["cid"]) is None
    assert "nothing sent" in phone["pushed"][0]["text"]


def test_astas_own_lines_are_not_arun_stepping_in(monkeypatch):
    from app import answers, chat_watch
    at = time.time()
    chat_watch.note_asta_said(CHAT, "Got it, checking what booking needs for this, bro")
    _row("Arunkumar K", "Got it, checking what booking needs for this, bro", at + 5, "ack")
    assert answers.moved_on(CHAT, at) == ""


# --- G. the question path follows the same rules ----------------------------------------

def test_a_question_after_a_correction_waits_for_arun(phone, monkeypatch):
    from app import answers, chat_watch, loop
    said = []

    async def _say(chat, line, **k):
        said.append(line)
        return True
    monkeypatch.setattr(chat_watch, "_say", _say)
    monkeypatch.setattr(answers, "moved_on", lambda chat, since: "")
    answers.mark_corrected(TID)
    t = _finished("")
    result = "ANALYSIS:\nunclear\n\nREPLY:\nWhich part should change on booking?"
    asyncio.run(answers.present_task(t["id"], t, result))
    assert not said
    assert loop.awaiting(phone["cid"])["what"] == "Which part should change on booking?"


# --- H. his voice, never Asta's process, never a false "it's me" -----------------------

@pytest.mark.parametrize("reply", [
    "Bro, I've checked both readings twice now — can you point me to it so I stop guessing?",
    "Got it, bro — you're right, my earlier read was wrong.",
    "Yeah bro, it's me, Arun — not Asta.",
])
def test_these_words_never_go_out_on_their_own(reply, phone, monkeypatch):
    from app import answers
    sent = []
    _auto_allowed(monkeypatch, sent)
    assert answers.unfit_to_send(reply)
    assert not asyncio.run(answers._deliver_automatic_reply({"to": CHAT, "what": reply,
                                                              "thread": TID}))
    assert not sent


def test_who_is_replying_is_never_answered_by_asta(rail, phone):
    rail.rows[CHAT] = [_msg(CHAT, "Bro, you are talking or Asta?")]
    rail.script[TID] = {"state": "ask", "need": "is it Arun or Asta",
                        "closing_confidence": 0.0, "summary": "identity", "entities": []}
    rail.sweep()
    assert not rail.asked and not rail.sent
    assert any("whether it's you or Asta" in p["text"] for p in phone["pushed"])


def test_an_investigation_that_claims_to_be_arun_is_dropped(phone, monkeypatch):
    from app import answers, loop
    sent = []
    _auto_allowed(monkeypatch, sent)
    monkeypatch.setattr(answers, "moved_on", lambda chat, since: "")
    t = _finished("", source="Bro, you are talking or Asta?")
    result = "ANALYSIS:\nIt's Arun.\n\nREPLY:\nYeah bro, it's me, Arun — not Asta."
    assert asyncio.run(answers.present_task(t["id"], t, result))
    assert not sent and loop.awaiting(phone["cid"]) is None
    assert "over to you" in phone["pushed"][0]["text"]


# --- the real timeline ------------------------------------------------------------------

def test_the_vinish_timeline_now(rail, phone, monkeypatch):
    """17:12 the link · an answer in flight when he explains · 17:22 "you or Asta?"

    One investigation per state of the conversation, nothing stale sent, the
    explanation folded into the booking plan once it exists, and the identity
    question left to Arun."""
    from app import answers, tasks
    sent = []
    _auto_allowed(monkeypatch, sent)
    cancelled = []

    async def cancel(task_id, status="cancelled", why=""):
        cancelled.append(task_id)
        store.update_task(task_id, status=status)
        return True
    monkeypatch.setattr(tasks, "cancel", cancel)
    t0 = time.time()

    # 17:12 — the link: a port, investigated once.
    rail.rows[CHAT] = [_msg(CHAT, ASK, t0)]
    rail.script[TID] = {"state": "ask", "need": "work from booking side on PR 1260",
                        "closing_confidence": 0.05, "summary": "carry AP 1260", "entities": [],
                        "work": "code"}
    rail.sweep()
    assert len(rail.asked) == 1
    from app import responder
    assert responder.what_it_asks(rail.asked[0]["text"]) == "port"

    # An answer is running for the thread when he explains (17:20).
    first = store.create_task("Vinish Kumar asked: carry AP PR 1260 to booking", "analysis", "p", None)
    store.update_task(first["id"], status="running")
    answers.remember_meta(first["id"], who=CHAT, need="carry", chat=CHAT, group=False,
                          thread=TID, source_text=ASK, kind="port")
    explain = ("It will come as a work process name, bro, like how it is coming for job "
               "closure. The same way we have to do it for this one also: job open.")
    _row(CHAT, explain, float(first["created_at"]) + 40, "explain")
    rail.rows[CHAT] = [_msg(CHAT, explain, float(first["created_at"]) + 40)]
    rail.script[TID] = {"state": "ask", "need": "job open like job closure",
                        "closing_confidence": 0.05, "summary": "explained", "entities": [],
                        "work": "check"}
    rail.sweep()
    assert cancelled == [first["id"]], "the stale run is stopped, not raced"
    assert len(rail.asked) == 2 and "job open" in rail.asked[1]["text"]

    # If the stale run had finished anyway, its answer still does not go out.
    stale = "ANALYSIS:\nold\n\nREPLY:\nCan you point me to the exact file you mean?"
    store.update_task(first["id"], status="done")
    assert asyncio.run(answers.present_task(first["id"], store.get_task(first["id"]), stale))
    assert not sent

    # 17:22 — "you or Asta?" is his to answer.
    rail.rows[CHAT] = [_msg(CHAT, "Bro, you are talking or Asta?", t0 + 600)]
    rail.script[TID] = {"state": "ask", "need": "is it you", "closing_confidence": 0.0,
                        "summary": "identity", "entities": []}
    rail.sweep()
    assert len(rail.asked) == 2 and not sent
    reviews = [t for t in store.list_tasks(limit=20) if t["title"].startswith("Review")]
    assert not reviews
