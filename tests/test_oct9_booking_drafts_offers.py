"""9 Oct, the afternoon after task 272 — four things he saw on his phone.

1. "HYNBFDPWY7R this booking in prod, why in AP status was still in progress"
   got "Bash is denied, register the Grafana proxy yourself": a ref with one
   digit was not a booking, so the brain got no log tools.
2. Task 275 staged a reply to Komal; it surfaced under that booking question,
   and his restatement of the booking went to the brain as an edit to it.
3. Every call that rang re-sent Vinish's old code-change offer — and a "yes"
   to pick up would have planned that instead.
4. Vinish's customs ask was offered five times while task 272 was building it,
   and the 272 plan was made without the flow doc that says the update goes to IOM.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app import (answers, booking_case, capabilities, context_pack, loop, main, offers,
                 store, tasks, tool_index)

BOOKING_Q = ("HYNBFDPWY7R this booking in prod , why in Ap status was still in progress "
             "not marked as closed . can you check")
RESTATED = ("HYNBFDPWY7R - for this booking in prod in booking cancelled activityplenevent "
            "in Ap service status remains in progress instead of closed , this was created on aug")
KOMAL_DRAFT = ("Komal Jayswal\nreviewed 1469, comments staged. is availability date for non "
               "NAM too? and IOM part already covered?")
VINISH_ASK = ("Implement the customs-status logic using only CUSTOMS_NOT_RELEASED/"
              "CUSTOMS_RELEASED/CUSTOMS_NOT_SELECTED fields as Vinish specified. Per-container "
              "billing integration logic (IS_CUSTOMS_CLEARED true/false, or skip the "
              "reference when not selected).")


@pytest.fixture(autouse=True)
def _clean():
    store.init()
    offers.drop_all()
    yield
    offers.drop_all()


@pytest.fixture
def phone(monkeypatch):
    said: list[str] = []

    async def _n(text, *a, **k):
        said.append(text)
    monkeypatch.setattr("app.notify.notify", _n)
    return said


# --- 1. a one-digit booking ref is a booking, and brings the log tools ---------------

def test_a_ref_with_one_digit_is_a_booking():
    assert booking_case.ids(BOOKING_Q) == ["HYNBFDPWY7R"]
    assert booking_case.missing(BOOKING_Q) == []


@pytest.mark.parametrize("text", [
    "password1x reset please",            # lower case: a word, not a ref
    "CUSTOMS_NOT_RELEASED for both",     # an identifier, not a ref
    "ABCDEFGH for now",                   # no digit
])
def test_words_and_identifiers_are_not_bookings(text):
    assert booking_case.ids(text) == []


def test_the_refs_that_already_matched_still_do():
    assert booking_case.ids("check h7sl7t6czlp and MH65W8JZNVNT in uat") == \
        ["H7SL7T6CZLP", "MH65W8JZNVNT"]


def test_a_booking_question_brings_the_log_and_workflow_tools():
    need = tool_index.required_for(BOOKING_Q)
    assert {"grafana_logs", "temporal_workflows", "temporal_workflow"} <= set(need)


def test_a_booking_question_is_checked_in_the_logs_not_asked_for_again():
    rule = booking_case.for_turn(BOOKING_Q)
    assert "HYNBFDPWY7R" in rule and "grafana_logs" in rule
    assert "not given" not in rule
    from app import capabilities as caps
    assert "register an MCP server" in caps.registry()["grafana_logs"].note


# --- 2. a task's draft is its own, and a new booking is not a draft edit ------------

def test_a_task_draft_waits_with_the_task_not_the_chat(monkeypatch):
    cid = store.create_conversation("wa", "claude_cli")["id"]
    monkeypatch.setattr(tasks, "current_conversation", lambda: cid)
    token = capabilities.FROM_TASK.set("275")
    try:
        from app import agent
        out = agent.prepare_to_send("reviewed 1469, comments staged.", to="Komal Jayswal",
                                    channel="chat")
    finally:
        capabilities.FROM_TASK.reset(token)
    assert "when the task finishes" in out
    assert loop.take(cid) is None, "it would surface after his next, unrelated message"
    assert loop.take_task_send(275)["to"] == "Komal Jayswal"


def test_the_finished_task_shows_its_draft_labelled(monkeypatch, phone):
    cid = store.create_conversation("wa", "claude_cli")["id"]
    monkeypatch.setattr(answers, "phone_conversation", lambda: cid)
    loop.set_task_send(275, "reviewed 1469, comments staged.", "Komal Jayswal")
    t = {"title": "Review Komal Jayswal's PR #1469"}
    assert asyncio.run(answers.present_task_draft(275, t))
    assert "From task #275 (Review Komal Jayswal's PR #1469)" in phone[-1]
    assert loop.awaiting(cid)["to"] == "Komal Jayswal"
    loop.clear_awaiting(cid)


def test_a_task_draft_is_not_shown_twice_when_its_reply_was(phone):
    loop.set_task_send(275, "reviewed 1469", "Komal Jayswal")
    assert not asyncio.run(answers.present_task_draft(275, {}, answered=True))
    assert loop.take_task_send(275) is None and not phone


@pytest.mark.parametrize("text, moves_on", [
    (RESTATED, True),                                  # the 9 Oct message
    ("check task 276 why it failed", True),
    ("add PR 1475 link too", False),                   # a new subject, put INTO it
    ("mention the 1469 comments are minor", False),
    ("PR 1469 drop the IOM question", False),          # the draft's own PR
    ("make it shorter", False),
])
def test_a_booking_the_draft_never_named_is_a_new_ask(text, moves_on):
    assert main._moves_on_from_draft(text, KOMAL_DRAFT) is moves_on


# --- 3. a call is asked, not the offer he moved on from -----------------------------

def _vinish_offer():
    return offers.propose("🛠 Vinish Kumar asks for a code change", "customs statuses",
                          "Want me to plan it?", action="plan", kind="plan_code")


def _ring():
    return offers.staged_write("call_answer", {"speak": False}, "📞 Rekha Mathew is calling",
                               "Rekha Mathew is calling.", "Answer it?", kind="call")


def test_a_call_goes_ahead_of_an_offer_he_moved_on_from():
    old = _vinish_offer()
    offers.set_aside(old.id)
    call = _ring()
    assert offers.pending().id == call.id, "his yes would have planned Vinish's change"
    assert "Rekha" in call.render() and "Vinish" not in call.render()
    assert [o.id for o in offers.waiting()] == [old.id]


def test_the_set_aside_offer_comes_back_without_being_asked_again():
    old = _vinish_offer()
    offers.set_aside(old.id)
    _ring()
    offers.accept()
    back = offers.pending()
    assert back.id == old.id and back.aside
    assert not store.kv_get(f"offer_unasked:{old.id}")


def test_an_offer_he_is_being_asked_still_holds_the_line():
    asked = _vinish_offer()
    _ring()
    assert offers.pending().id == asked.id


def test_a_ringing_call_is_what_reaches_his_phone(phone):
    from app import incoming
    old = _vinish_offer()
    offers.set_aside(old.id)
    asyncio.run(incoming.offer({"who": "Rekha Mathew", "via": "page", "at": 1.0}))
    assert "Rekha" in phone[-1] and "Vinish" not in phone[-1]


# --- 4. work already being built, and plans made from his knowledge -----------------

def _task_272():
    t = store.create_task("Prepare for new customs status references -> isCustomsCleared",
                          "code", "update iscustomsCleared from the UI statuses", "booking")
    store.kv_set(f"task_plan:{t['id']}", "CUSTOMS_NOT_RELEASED and CUSTOMS_NOT_SELECTED "
                                          "on CONTAINER_CUSTOMS_EXECUTION")
    return t


def test_an_ask_a_running_task_covers_is_not_offered_again(phone):
    t = _task_272()
    shown = asyncio.run(answers.offer_plan(who="Vinish Kumar", chat="Vinish Kumar",
                                           need=VINISH_ASK, summary="", thread=""))
    assert not shown and offers.pending() is None
    assert f"task #{t['id']}" in phone[-1]
    asyncio.run(answers.offer_plan(who="Vinish Kumar", chat="Vinish Kumar",
                                   need=VINISH_ASK, summary="", thread=""))
    assert len(phone) == 1, "told once, not every time he restates it"


def test_one_shared_word_is_not_the_same_work():
    _task_272()
    assert tasks.covering_task("customs screen needs a new colour for the badge") is None


def test_the_plan_is_handed_his_flow_knowledge(monkeypatch):
    t = _task_272()
    monkeypatch.setattr("app.project_knowledge.lookup",
                        lambda q, ws="", budget=0, channel="": "CUSTOMS_UPDATE → manualCustomsUpdate"
                        " → sendEndBookingEventToIOM")
    pack = context_pack.build(t["id"], "plan")
    assert "sendEndBookingEventToIOM" in pack and "upstream and downstream" in pack
    assert "How this flow works" not in context_pack.build(t["id"], "implement")


def test_the_plan_must_name_every_reader_of_what_it_changes():
    assert "EVERY place it goes" in tasks.CODE_OVERRIDES
    assert "CONTEXT USED:" in tasks.CODE_OVERRIDES and "ASSUMPTIONS:" in tasks.CODE_OVERRIDES
    assert "test that fails" in tasks.CODE_OVERRIDES


def test_the_context_boot_points_at_the_workspace_from_a_worktree(tmp_path, monkeypatch):
    ws = tmp_path / "booking-workspace"
    (ws / ".asta-context").mkdir(parents=True)
    (ws / ".asta-context" / "boot.sh").write_text("echo ok\n")
    wt = ws / ".asta-worktrees" / "task-272"
    wt.mkdir(parents=True)
    monkeypatch.setattr(tasks, "code_cwd", lambda w: str(ws))
    out = tasks.context_boot_paths(tasks.CODE_OVERRIDES, str(wt), "booking")
    assert f"sh {ws}/.asta-context/boot.sh" in out
    assert "sh .asta-context/boot.sh" not in out
    # Run from the workspace itself, the relative path already works.
    assert tasks.context_boot_paths(tasks.CODE_OVERRIDES, str(ws), "booking") == tasks.CODE_OVERRIDES


# --- 5. every brain, and the way he wants a case debugged ----------------------------

class _Captured(Exception):
    pass


def test_the_api_key_brain_gets_the_same_turn_context_as_the_cli_brains(monkeypatch):
    """Claude CLI and Copilot CLI put copilot_cli.turn_context ahead of his words;
    an API-key turn did not, so a booking question reached it with no rule."""
    seen = {}

    class _Agent:
        def run_stream_events(self, prompt, **kw):
            seen["prompt"] = prompt
            raise _Captured()

    monkeypatch.setattr(main.agent_mod, "get_model", lambda n: object())
    monkeypatch.setattr(main.agent_mod, "build_agent", lambda sel: _Agent())
    monkeypatch.setattr(main, "AGENT", _Agent())
    monkeypatch.setattr(main, "_load_history", lambda conv: [])
    conv = {"id": "api1", "summary": "", "workspace": "booking"}
    with pytest.raises(_Captured):
        asyncio.run(main._run_turn_streaming(None, conv, BOOKING_Q, "claude"))
    assert "HYNBFDPWY7R" in seen["prompt"] and "grafana_logs" in seen["prompt"]
    assert seen["prompt"].rstrip().endswith("can you check") or BOOKING_Q in seen["prompt"]


def test_a_why_question_about_a_booking_is_debugged_his_way():
    rule = booking_case.rule(BOOKING_Q)
    for step in ("State and trail", "kept about 8 weeks", "Class:line",
                 "temporal_workflow", "Not confirmed"):
        assert step in rule, step
    assert {"resolve_context", "read_workspace_file"} <= set(tool_index.required_for(BOOKING_Q))


def test_a_status_question_keeps_the_short_brief():
    rule = booking_case.rule("have we sent TMS for H7SL7T6CZLP in sit?")
    assert "State and trail" not in rule


def test_a_debugging_task_is_briefed_with_the_case_method():
    from app import roles
    assert roles.CASE_METHOD in roles.briefed("debugging")
    assert roles.CASE_METHOD not in roles.briefed("review")
    assert roles.CASE_METHOD not in roles.brief("debugging"), "the brief stays short"
    from app import copilot_cli
    sysp = copilot_cli._first_turn_context({"id": "x", "summary": "", "workspace": "booking"},
                                           user_text="why is the email service throwing 500s")
    assert "State and trail" in sysp
