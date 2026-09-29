"""Round 4, phases D, C and E: do the work, ask him once, never lose the thread.

His words, 29 Sep:

    only final analysis report comes to [me] before ask: this person asked this,
    this is the final analysis, can i send ... ask the clarifications like me in
    chat, get the context, do the needful ... for coding u come with plan, once i
    approve, code then ship ... it should know their previous context and
    remember and continue talk until it resolves, no context rot

So, tested here from real flows:
  D — a colleague's ask is clarified if vague, investigated, and reaches him ONCE
      as "who asked · what was found · the reply · send?"; "send" delivers it.
      A code ask becomes "plan it?"; nothing else interrupts him.
      One decision is in front of him at a time.
  C — the same person-context feeds his chat, calls and investigations.
  E — "what's happening with Navya?" is answered from the record, and the
      reading of conversations is measured on his own messages.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app import store


# --- D: the finished answer, split and presented -----------------------------------------

RESULT = """I checked PR 1251 in telikos-activityplanworkflow-service.

ANALYSIS:
Navya asked to merge PR 1251. CI is green, one approval, no conflicts; the change
only touches the hotfix mapping (MappingService.java:88).

REPLY:
Checked it — CI is green and it's approved, merging it now for the hotfix."""


def test_the_answer_splits_into_his_part_and_theirs():
    from app import answers
    analysis, reply = answers.split(RESULT)
    assert analysis.startswith("Navya asked to merge PR 1251")
    assert reply.startswith("Checked it")
    assert "ANALYSIS" not in reply


def test_markdown_headings_are_read_too():
    from app import answers
    a, r = answers.split("**ANALYSIS:**\nfound it\n\n## REPLY\nhere you go")
    assert a == "found it" and r == "here you go"


def test_a_result_without_a_reply_is_not_presented_as_one():
    from app import answers
    assert answers.split("just a normal report")[1] == ""


@pytest.fixture
def phone(monkeypatch):
    """His WhatsApp conversation, and a recorder for what reaches his phone."""
    from app import loop, notify
    cid = store.create_conversation("claude", None)["id"]
    store.kv_set("wa_conversation", cid)
    pushed = []

    async def _notify(text, level="info", urgency="direct", *a, **k):
        pushed.append({"text": text, "level": level, "urgency": urgency})
        return {}
    monkeypatch.setattr(notify, "notify", _notify)
    loop.clear_awaiting(cid)
    store.kv_set("answers_queue", "[]")
    return {"cid": cid, "pushed": pushed}


def test_he_is_asked_once_with_who_what_and_the_reply(phone):
    from app import answers, loop
    ok = asyncio.run(answers.present(who="Navya R", need="merge PR 1251", chat="Navya R",
                                     group=False, analysis="CI green, approved.",
                                     reply="Merged it for the hotfix.", task_id=7))
    assert ok and len(phone["pushed"]) == 1
    text = phone["pushed"][0]["text"]
    assert "Navya R" in text and "merge PR 1251" in text
    assert "CI green" in text and "Merged it for the hotfix." in text
    assert "send" in text.lower()
    staged = loop.awaiting(phone["cid"])
    assert staged["to"] == "Navya R" and staged["channel"] == "teams"
    assert staged["what"] == "Merged it for the hotfix."


def test_send_goes_through_the_recorded_teams_call_not_a_model(phone):
    """The approved words are the words that go out — the existing mechanical send."""
    from app import answers, loop, main
    asyncio.run(answers.present(who="Navya R", need="merge", chat="Navya R", group=False,
                                analysis="", reply="Done.", task_id=7))
    op = main._mechanical_send(loop.awaiting(phone["cid"]))
    assert op and op["name"] == "teams_send"
    assert op["args"] == {"to": "Navya R", "text": "Done.", "to_group": False}


def test_one_decision_at_a_time(phone):
    from app import answers, loop
    asyncio.run(answers.present(who="Navya R", need="merge", chat="Navya R", group=False,
                                analysis="", reply="first", task_id=1))
    asyncio.run(answers.present(who="Vinish Kumar", need="helm", chat="Vinish Kumar",
                                group=False, analysis="", reply="second", task_id=2))
    assert len(phone["pushed"]) == 1, "the second waits — 'send' must not be ambiguous"
    assert loop.awaiting(phone["cid"])["what"] == "first"
    loop.clear_awaiting(phone["cid"])
    assert asyncio.run(answers.next_after(phone["cid"]))
    assert loop.awaiting(phone["cid"])["what"] == "second"


def test_an_open_offer_also_holds_the_next_answer(phone):
    """An offer and a staged draft both read his next "yes"."""
    from app import answers, offers
    offers.propose("something", "ctx", "want me to?", "do it")
    asyncio.run(answers.present(who="Navya R", need="merge", chat="Navya R", group=False,
                                analysis="", reply="later", task_id=3))
    assert not phone["pushed"]


def test_a_code_ask_becomes_plan_it_and_nothing_is_coded(phone):
    from app import answers, offers
    shown = asyncio.run(answers.offer_plan(who="Navya R", chat="Navya R",
                                           need="add a null check to the mapper",
                                           summary="Navya wants a null check", thread=""))
    assert shown
    o = offers.pending()
    assert o and o.kind == "plan_code"
    assert "plan" in phone["pushed"][0]["text"].lower()
    assert "before he approves" in o.action, "the plan gate is part of the instruction"


def test_a_finished_investigation_for_a_colleague_comes_as_the_decision(phone, monkeypatch):
    from app import answers
    t = store.create_task("Check PR 1251", "analysis", "brief", None, teams_chat="Navya R")
    answers.remember_meta(t["id"], who="Navya R", need="merge PR 1251", chat="Navya R",
                          group=False, thread="")
    assert asyncio.run(answers.present_task(t["id"], t, RESULT))
    assert "merge PR 1251" in phone["pushed"][0]["text"]


def test_an_ordinary_investigation_is_left_as_it_was(phone):
    from app import answers
    t = store.create_task("Check something", "analysis", "brief", None)
    assert not asyncio.run(answers.present_task(t["id"], t, RESULT))


def test_sending_marks_that_asta_has_spoken_in_the_conversation():
    from app import answers, threads
    threads.open("teams", "Navya R", chat="Navya R")
    answers.sent({"thread": "teams:Navya R", "what": "Merged."})
    t = threads.get("teams:Navya R")
    assert t["asta_spoke"] == 1 and t["status"] == "answered"


def test_the_investigation_is_briefed_for_a_reply_with_their_history(monkeypatch):
    monkeypatch.setenv("ASTA_RESPOND", "1")
    from app import responder, tasks
    monkeypatch.setattr(responder, "familiar", lambda text: (True, "known"))
    seen = {}

    def spawn(title, prompt, kind="analysis", ws=None, teams_chat="", **k):
        seen.update(prompt=prompt, teams_chat=teams_chat)
        return store.create_task(title, kind, prompt, ws, teams_chat=teams_chat)
    monkeypatch.setattr(tasks, "spawn", spawn)
    t = responder.respond("teams-chat", "Navya R", "can you please merge PR 1251?", priority=1,
                          context="Earlier with Navya R: PR 1251 was reviewed on 26 Sep",
                          reply_to="Navya R", need="merge PR 1251", thread="teams:Navya R")
    assert t and seen["teams_chat"] == "Navya R"
    assert "REPLY:" in seen["prompt"] and "ANALYSIS:" in seen["prompt"]
    assert "reviewed on 26 Sep" in seen["prompt"], "it continues, it does not restart"


# --- D: the clarifying question that may go out in his name ------------------------------

@pytest.mark.parametrize("q,ok", [
    ("Which booking number is this about?", True),
    ("Could you share the PR link?", True),
    ("Sure, I'll check it now?", False),
    ("Arun will look at it tomorrow?", False),
    ("Which booking", False),
    ("x?", False),
])
def test_only_a_real_question_may_go_out_unapproved(q, ok):
    from app import understand
    assert bool(understand.safe_question(q)) is ok


# --- C: the same person-context everywhere ------------------------------------------------

def _navya_history():
    from app import threads
    threads.remember_episode("teams", "Navya R", need="review PR 1251",
                             summary="Hotfix PR 1251 reviewed and approved.",
                             entities=["github.com/org/svc/pull/1251"],
                             closed_at=time.time() - 2 * 86400)
    threads.open("teams", "Navya R", chat="Navya R")
    threads.update("teams:Navya R", need="merge PR 1251",
                   summary="Navya asked Arun to merge PR 1251.", status="awaiting_arun")


def test_what_asta_knows_about_a_person_is_one_short_block():
    from app import threads
    _navya_history()
    block = threads.context_for("Navya R")
    assert "merge PR 1251" in block and "answer waiting for Arun" in block
    assert "reviewed and approved" in block
    assert block.count("\n") <= 4, "bounded — the rest stays searchable, not carried"


def test_his_chat_knows_what_is_going_on_with_the_person_just_named():
    from app import copilot_cli, referents
    _navya_history()
    referents.note("Navya R", "can you please merge this", source="Teams 1:1")
    ctx = copilot_cli.turn_context("what did she want?")
    assert "merge PR 1251" in ctx


def test_a_call_starts_knowing_the_history():
    from app import conversation
    _navya_history()
    agenda = conversation.with_history("Navya R", "the hotfix")
    assert agenda.startswith("the hotfix") and "PR 1251" in agenda
    assert "do not ask again" in agenda


def test_a_call_to_a_stranger_is_unchanged():
    from app import conversation
    assert conversation.with_history("Nobody Known", "hello") == "hello"


# --- E: visibility, and measuring the reading --------------------------------------------

def test_whats_happening_with_navya_is_answered_from_the_record():
    from app import threads
    _navya_history()
    threads._record("teams:Navya R", "understood:ask", "model conf=0.10 need=merge PR 1251")
    report = threads.status_report("navya")
    assert "Navya R" in report and "merge PR 1251" in report
    assert "understood:ask" in report and "reviewed and approved" in report


def test_the_brain_is_offered_that_record_when_he_asks():
    from app import tool_index
    assert "conversation_status" in tool_index.required_for("whats happening with navya")
    assert "conversation_status" in tool_index.required_for("did you reply to vinish?")


def test_the_rules_floor_never_quietly_gets_worse():
    """Measured on his own messages: 20 of 28 on 29 Sep. The model read 27."""
    from app import understand_eval
    acc, wrong = understand_eval.rules_only()
    assert acc >= 0.70, "\n".join(wrong)


def test_the_eval_reads_his_reactions_the_way_the_sweep_does():
    from app import understand_eval
    handled = [it for it in understand_eval.items() if it["handled_by_him"]]
    assert len(handled) == 4


# --- D, in the sweep: what he sees, and what he does not -----------------------------------

@pytest.fixture
def rail(monkeypatch, phone):
    from app import chat_watch, responder, teams_bridge, understand
    monkeypatch.setenv("ASTA_THREADS", "1")
    monkeypatch.setenv("ASTA_UNDERSTAND_MODEL", "haiku")
    monkeypatch.setenv("ASTA_ASK_BACK", "1")
    rows, script, sent, asked = {}, {}, [], []

    async def _candidates():
        return list(rows)

    async def _new_in(chat, advance=True):
        return rows.pop(chat, [])

    async def _send(chat, text, *a, **k):
        sent.append((chat, text))
        return True

    async def _call(prompt):
        return json.dumps({"threads": [{"id": t, **d} for t, d in script.items()]})

    def _respond(source, who, text, **k):
        asked.append({"who": who, **k})
        return {"id": 99, "title": "t"}          # taken on

    monkeypatch.setattr(chat_watch, "candidates", _candidates)
    monkeypatch.setattr(chat_watch, "new_in", _new_in)
    monkeypatch.setattr(teams_bridge, "send_message", _send)
    monkeypatch.setattr(understand, "_call", _call)
    monkeypatch.setattr(responder, "respond", _respond)

    from app import notify

    class R:
        pass
    r = R()
    r.rows, r.script, r.sent, r.asked, r.pushed = rows, script, sent, asked, phone["pushed"]
    r.sweep = lambda: asyncio.run(chat_watch.sweep(notify.notify))
    return r


def _m(who, text):
    return {"key": text, "sender": who, "text": text, "sent_at": time.time()}


def _decide(state, need="", question="", work="check", conf=0.0):
    return {"state": state, "need": need, "question": question, "work": work,
            "closing_confidence": conf, "summary": need, "entities": []}


def test_a_vague_ask_gets_a_question_not_a_guess(rail):
    rail.rows["Vinish Kumar"] = [_m("Vinish Kumar", "also this failed")]
    rail.script["teams:Vinish Kumar"] = _decide("ask", "something failed",
                                                question="Which job or test failed?")
    rail.sweep()
    assert rail.sent == [("Vinish Kumar", "Which job or test failed?")]
    assert not rail.asked and not rail.pushed


def test_he_is_asked_at_most_twice(rail):
    for _ in range(3):
        rail.rows["Vinish Kumar"] = [_m("Vinish Kumar", "it failed again")]
        rail.script["teams:Vinish Kumar"] = _decide("ask", "failure",
                                                    question="Which job failed?")
        rail.sweep()
    assert len(rail.sent) == 2 and len(rail.asked) == 1, "then it gets on with it"


def test_a_clear_ask_is_worked_silently_until_the_answer_is_ready(rail):
    """Only the final analysis comes to him."""
    rail.rows["Navya R"] = [_m("Navya R", "can you please merge PR 1251?")]
    rail.script["teams:Navya R"] = _decide("ask", "merge PR 1251")
    rail.sweep()
    assert rail.asked and rail.asked[0]["reply_to"] == "Navya R"
    assert not rail.pushed, "nothing now — the answer comes as one 'send?'"


def test_urgent_interrupts_even_while_it_is_being_worked(rail):
    rail.rows["Vinish Kumar"] = [_m("Vinish Kumar", "prod bookings are down")]
    rail.script["teams:Vinish Kumar"] = _decide("urgent", "prod bookings down")
    rail.sweep()
    assert rail.pushed and rail.pushed[0]["urgency"] == "direct"


def test_a_code_ask_is_offered_as_a_plan_not_investigated(rail):
    rail.rows["Navya R"] = [_m("Navya R", "can you add a null check in the mapper")]
    rail.script["teams:Navya R"] = _decide("ask", "add a null check", work="code")
    rail.sweep()
    assert not rail.asked
    assert rail.pushed and "plan" in rail.pushed[0]["text"].lower()
