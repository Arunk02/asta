"""A question about one booking is checked in the logs — never answered blind.

Arun, 2 Oct: "u have to use logs to confirm if he share any bookingid … if they
split and send in as two messages, first message and then bookingid, wait and
understand, ask them and then respond either question or debugging. dont be
blind in this." And: "ask user which env have to check irrespective of always
checking in prod", and "even if someone asks in call also check in logs, ask
them to wait for couple of mints, check and update".

Every scenario below is one of those sentences.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app import booking_case, store

Q = "does manual customs reach billing for this booking"
ID = "MH65W8JZNVNT"


# --- the rule itself ------------------------------------------------------------------

def test_ids_are_found_in_any_case_and_words_never_are():
    assert booking_case.ids(f"check {ID.lower()} pls") == [ID]
    assert booking_case.ids("H65ZMWX52B2 and CBK123456") == ["H65ZMWX52B2", "CBK123456"]
    assert booking_case.ids("provides the reconciliation implementation") == []


@pytest.mark.parametrize("said,env", [
    ("in preprod", "preprod"), ("pre-prod", "preprod"), ("on PROD", "prod"),
    ("sit env", "sit"), ("uat", "uat"), ("lower env", "lower"), ("no env here", ""),
])
def test_the_environment_is_read_as_people_say_it(said, env):
    assert booking_case.env_of(said) == env


def test_a_question_naming_a_booking_it_does_not_include_waits_for_the_id():
    assert booking_case.waiting_for_more(Q)
    assert booking_case.waiting_for_more("check this booking:")


def test_an_id_with_no_question_waits_for_the_question():
    assert booking_case.waiting_for_more(ID)
    assert not booking_case.waiting_for_more(ID, before=Q), "the question came first"


def test_a_complete_ask_or_a_general_question_does_not_wait():
    assert not booking_case.waiting_for_more(f"{Q} {ID} in preprod")
    assert not booking_case.waiting_for_more("does manual customs reach billing?")
    assert not booking_case.waiting_for_more("send this to Vinish")


def test_a_general_question_is_never_asked_which_booking():
    """The blind reply in the other direction: "which booking?" to a question
    about how the flow works."""
    assert booking_case.missing("does manual customs reach billing?") == []
    assert booking_case.ask_line("does manual customs reach billing?") == ""


def test_what_is_missing_is_asked_for_both_at_once():
    assert booking_case.missing(Q) == ["booking", "environment"]
    line = booking_case.ask_line(Q)
    assert "which booking" in line and "preprod" in line and "logs" in line


def test_an_id_without_its_environment_is_asked_which_never_assumed_prod():
    line = booking_case.ask_line(ID, Q)
    assert ID in line and "which environment" in line


def test_an_id_on_its_own_is_asked_what_to_check():
    assert booking_case.ask_line(ID).startswith(f"Sure — what would you like me to check on {ID}")


def test_the_check_rule_puts_the_logs_over_the_documents():
    r = booking_case.rule(f"{Q} {ID} in preprod")
    assert ID in r and "preprod" in r
    assert "what SHOULD happen" in r and "what DID happen" in r and "grafana_logs" in r and "Temporal" in r
    assert "the logs win" in r and "not confirmed in the logs" in r
    assert "observed start/end, env and services" in r and "never extrapolate" in r


def test_an_unnamed_environment_is_searched_everywhere_and_said_never_prod():
    r = booking_case.rule(f"{Q} {ID}")
    assert 'namespace="all"' in r and "never assume prod" in r


def test_ids_spelled_out_loud_are_joined():
    assert booking_case.spoken_ids("it is M H 6 5 W 8 J Z N V N T in preprod") == [ID]
    assert booking_case.spoken_ids("booking MH65 W8JZ NVNT please") == [ID]
    assert booking_case.spoken_ids("I have 2 at 5 today") == []


# --- Teams: the investigation ---------------------------------------------------------

def test_a_booking_question_with_its_id_is_investigated_not_nothing(monkeypatch):
    """Before: "nothing checkable in it" — no investigation, one line."""
    from app import responder
    assert responder.what_it_asks(f"{Q.replace('this', 'the')} {ID}") == "debug"


def test_the_investigation_searches_the_named_environment_and_checks_the_rule():
    from app import responder
    p = responder.playbook(f"{Q} {ID} in sit")
    assert 'namespace="sit"' in p and "the logs win" in p


def test_the_investigation_never_defaults_to_production():
    from app import responder
    assert "never assume production" in responder._HOW
    assert "Production unless" not in responder._HOW


# --- Teams: split messages, end to end through the sweep -----------------------------

@pytest.fixture
def rail(monkeypatch):
    from app import chat_watch, responder, teams_bridge, understand
    monkeypatch.setenv("ASTA_THREADS", "1")
    monkeypatch.setenv("ASTA_UNDERSTAND_MODEL", "haiku")
    monkeypatch.setenv("ASTA_ASK_BACK", "1")
    rows: dict[str, list[dict]] = {}
    script: dict[str, dict] = {}
    sent: list[tuple[str, str]] = []
    asked: list[dict] = []

    async def _candidates():
        return list(rows)

    async def _new_in(chat, advance=True):
        return rows.pop(chat, [])

    async def _send(chat, text, *a, **k):
        sent.append((chat, text))
        return True

    async def _call(prompt):
        return json.dumps({"threads": [{"id": tid, **d} for tid, d in script.items()]})

    async def _notify(*a, **k):
        return {}

    def _respond(source, who, text, **k):
        asked.append({"who": who, "text": text})
        return None

    monkeypatch.setattr(chat_watch, "candidates", _candidates)
    monkeypatch.setattr(chat_watch, "new_in", _new_in)
    monkeypatch.setattr(teams_bridge, "send_message", _send)
    monkeypatch.setattr(understand, "_call", _call)
    monkeypatch.setattr(responder, "respond", _respond)

    class R:
        pass
    r = R()
    r.rows, r.script, r.sent, r.asked = rows, script, sent, asked
    r.sweep = lambda: asyncio.run(chat_watch.sweep(_notify))
    return r


def _msg(text, who="Vinish Kumar"):
    return {"key": f"{who}:{text}", "sender": who, "text": text, "sent_at": time.time()}


def _ask(need):
    return {"state": "ask", "need": need, "closing_confidence": 0.0, "summary": need,
            "entities": [], "work": "check", "subject": "stated"}


def _expire_holds():
    from app import chat_watch
    allh = chat_watch._held_all()
    for h in allh.values():
        h["since"] = time.time() - chat_watch.HOLD_SECONDS - 1
    store.kv_set(chat_watch._HELD_KEY, json.dumps(allh))


def test_question_then_id_are_read_together_and_investigated(rail):
    rail.rows["Vinish Kumar"] = [_msg(f"{Q} in preprod")]
    rail.script["teams:Vinish Kumar"] = _ask("check manual customs to billing for a booking")
    rail.sweep()
    assert not rail.asked and not rail.sent, "the first half waits for the second"
    rail.rows["Vinish Kumar"] = [_msg(ID)]
    rail.sweep()
    assert len(rail.asked) == 1, "one investigation, for the whole question"
    text = rail.asked[0]["text"]
    assert "manual customs" in text and ID in text and "preprod" in text
    assert not rail.sent


def test_question_then_id_without_an_environment_asks_which_one(rail):
    rail.rows["Vinish Kumar"] = [_msg(Q)]
    rail.script["teams:Vinish Kumar"] = _ask("check manual customs to billing for a booking")
    rail.sweep()
    rail.rows["Vinish Kumar"] = [_msg(ID)]
    rail.sweep()
    assert not rail.asked, "not searched in prod by assumption"
    assert rail.sent and f"which environment is {ID} in" in rail.sent[0][1]


def test_a_question_whose_id_never_comes_is_asked_for_it_after_the_wait(rail):
    rail.rows["Vinish Kumar"] = [_msg(Q)]
    rail.script["teams:Vinish Kumar"] = _ask("check manual customs to billing for a booking")
    rail.sweep()
    assert not rail.sent
    _expire_holds()
    rail.sweep()
    assert not rail.asked, "never answered from the documents as if it were this booking"
    assert rail.sent and "which booking is it" in rail.sent[0][1]


def test_an_id_on_its_own_is_asked_what_to_check_after_the_wait(rail):
    rail.rows["Vinish Kumar"] = [_msg(ID)]
    rail.script["teams:Vinish Kumar"] = {**_ask("look at a booking"), "state": "opener"}
    rail.sweep()
    assert not rail.sent
    _expire_holds()
    rail.sweep()
    assert rail.sent and f"what would you like me to check on {ID}" in rail.sent[0][1]


def test_the_environment_answering_asta_question_starts_the_investigation(rail):
    """"preprod", answering "which environment?", reads like a status line. It
    is the rest of their question."""
    from app import threads
    threads.open("teams", "Vinish Kumar", chat="Vinish Kumar")
    threads.update("teams:Vinish Kumar", status="clarifying", asta_spoke=1,
                   need=f"whether manual customs reached billing for booking {ID}")
    rail.rows["Vinish Kumar"] = [_msg("preprod")]
    rail.script["teams:Vinish Kumar"] = {"state": "status", "need": "", "closing_confidence": 0.2,
                                         "summary": "", "entities": []}
    rail.sweep()
    assert len(rail.asked) == 1
    text = rail.asked[0]["text"]
    assert ID in text and "preprod" in text and "manual customs" in text


def test_a_complete_question_is_investigated_at_once(rail):
    rail.rows["Vinish Kumar"] = [_msg(f"{Q} {ID} in uat")]
    rail.script["teams:Vinish Kumar"] = _ask("check manual customs to billing for a booking")
    rail.sweep()
    assert len(rail.asked) == 1 and not rail.sent


def test_a_general_flow_question_is_neither_held_nor_asked_which_booking(rail):
    rail.rows["Vinish Kumar"] = [_msg("does manual customs reach billing?")]
    rail.script["teams:Vinish Kumar"] = _ask("how manual customs reaches billing")
    rail.sweep()
    from app import chat_watch
    assert not chat_watch.held("Vinish Kumar")
    assert not any("which booking" in t for _, t in rail.sent)


def test_a_held_half_ask_wakes_the_watcher_when_its_wait_is_over():
    from app import chat_watch
    chat_watch.hold("Vinish Kumar", [_msg(Q)], "names a booking", time.time())
    nxt = chat_watch._next_held(time.time())
    assert nxt is not None and 0 < nxt <= chat_watch.HOLD_SECONDS
    _expire_holds()
    assert "Vinish Kumar" in chat_watch.take_hot()


# --- WhatsApp and voice jobs: Claude and Copilot alike --------------------------------

def test_a_chat_turn_about_one_booking_carries_the_rule_or_the_question():
    assert "ask for it in one short line" in booking_case.for_turn(Q)
    assert "grafana_logs" in booking_case.for_turn(f"{Q} {ID} in preprod")
    assert booking_case.for_turn("does manual customs reach billing?") == ""


def test_the_chat_brains_get_it_in_their_turn_context():
    from app import copilot_cli
    assert "what DID happen" in copilot_cli.turn_context(f"{Q} {ID} in preprod")


def test_voice_does_not_answer_this_booking_from_the_documents():
    from app import voice_mode
    assert not voice_mode.knows_about(Q)
    assert voice_mode.knows_about("does manual customs reach billing?")


def test_a_spelled_id_reaches_the_voice_job():
    from app import voice_mode
    out = voice_mode.with_context("check booking M H 6 5 W 8 J Z N V N T in preprod", [])
    assert ID in out and "may be misheard" in out


# --- calls: ask, hold on, check, tell ---------------------------------------------------

def _lines(*theirs):
    return [{"speaker": "Vinish Kumar", "text": t} for t in theirs]


def test_on_a_call_a_booking_question_without_its_number_asks_for_it():
    from app import conversation
    state: dict = {}
    note = conversation.booking_turn("Vinish Kumar", Q, _lines(Q), state)
    assert "read out the booking number" in note and state.get("asked")


def test_on_a_call_an_id_without_environment_is_read_back_and_asked():
    from app import conversation
    said = f"{Q} M H 6 5 W 8 J Z N V N T"
    note = conversation.booking_turn("Vinish Kumar", said, _lines(said), {})
    assert ID in note and "which environment" in note


def test_on_a_call_the_check_starts_and_they_are_asked_to_hold_on(monkeypatch):
    from app import conversation, responder
    started = []
    monkeypatch.setattr(responder, "check_for_call",
                        lambda who, q, ids, env: started.append((who, ids, env)) or {"id": 7})
    state: dict = {}
    conversation.booking_turn("Vinish Kumar", Q, _lines(Q), state)
    note = conversation.booking_turn("Vinish Kumar", f"{ID}, preprod",
                                     _lines(Q, f"{ID}, preprod"), state)
    assert started == [("Vinish Kumar", [ID], "preprod")]
    assert "couple of minutes" in note and "hold on" in note
    assert state["check"]["id"] == 7


def test_on_a_call_the_finding_is_said_when_the_check_is_done(monkeypatch):
    from app import conversation
    state = {"check": {"id": 7, "ids": [ID], "env": "preprod", "at": time.time(),
                       "told": False, "still": False}}
    monkeypatch.setattr(store, "get_task", lambda i: {"status": "running"})
    assert conversation.check_ready(state) == ""
    monkeypatch.setattr(store, "get_task", lambda i: {
        "status": "done", "result": "Manual customs update at 10:02, AP called, billing event "
                                    "sent at 10:03 — S4 country."})
    note = conversation.check_ready(state)
    assert "billing event" in note and "the answer first" in note
    assert state["check"]["told"]


def test_on_a_call_silence_while_checking_is_waiting_then_goes_to_chat():
    from app import conversation
    now = time.time()
    state = {"check": {"id": 7, "ids": [ID], "env": "preprod", "at": now - 5,
                       "told": False, "still": False}}
    assert conversation.waiting_line(state) == ""
    state["check"]["at"] = now - conversation.CHECK_STILL_SECONDS - 1
    assert conversation.waiting_line(state) == conversation._STILL_CHECKING
    assert conversation.waiting_line(state) == "", "said once"
    state["check"]["at"] = now - conversation.CHECK_WAIT_SECONDS - 1
    assert "chat" in conversation.waiting_line(state) and state["check"]["told"]


def test_a_call_check_is_the_same_read_only_investigation(monkeypatch):
    from app import responder, tasks
    monkeypatch.setattr(responder, "enabled", lambda: True)
    spawned = []
    monkeypatch.setattr(tasks, "spawn", lambda title, brief, kind, ws, **k: spawned.append(
        (kind, brief, k)) or {"id": 41})
    t = responder.check_for_call("Vinish Kumar", Q, [ID], "preprod")
    assert t["id"] == 41
    kind, brief, k = spawned[0]
    assert kind == "analysis", "read-only, never code"
    assert "LIVE CALL" in brief and "the logs win" in brief and 'namespace="preprod"' in brief
    assert k["teams_chat"] == "Vinish Kumar", "too slow for the call: the answer reaches their chat"


def test_the_call_brain_gets_the_note_with_their_line():
    from app import call_mind
    sent = []
    mind = call_mind.Mind.__new__(call_mind.Mind)

    async def stream(text, timeout):
        sent.append(text)
        yield "Sure."
        yield call_mind._COMPLETE
    mind._stream = stream

    async def go():
        return [s async for s in mind.sentences("", note="Your log check is done")]
    asyncio.run(go())
    assert sent[0].startswith("[Your log check is done]") and "said nothing new" in sent[0]


def test_the_call_brain_never_answers_one_booking_from_the_knowledge():
    from app import call_mind
    assert "never answered from the project knowledge" in call_mind.persona("V", "x")


def test_a_hold_that_comes_to_nothing_is_let_go(rail):
    from app import chat_watch
    chat_watch.hold("Someone Else", [_msg(Q, who="Someone Else")], "names a booking",
                    time.time() - chat_watch.HOLD_SECONDS - 1)
    import app.chat_watch as cw
    orig = cw.addressed_to_him
    cw.addressed_to_him = lambda *a, **k: False
    try:
        rail.sweep()
    finally:
        cw.addressed_to_him = orig
    assert not chat_watch.held("Someone Else")


def test_the_check_looks_both_ways_at_every_hand_off_and_summarises():
    """2 Oct: "have we sent TMS for this booking? then customs UNITED? …
    check both wise, forward and reverse ack happens or not … proper
    summarisation" — and "some places only one way, like receiving execution
    from TMS": a one-way flow is never reported as "no ack"."""
    r = booking_case.rule(f"have we sent TMS for {ID} in sit? and customs UNITED?")
    for x in ("two-way (sent, then ack back)", "SEND_TO_TMS → SAP_TMS_ACK_FEEDBACK", "CIP_GOT",
              "inbound only (received or not — never 'no ack')", "SAP_TMS_EXECUTION_STATUS",
              "outbound only", "booking→IOM", "SEND_DOCUMENTS"):
        assert x in r, x
    assert "with the id ALONE first" in r, "never 'not sent' from one keyword filter"
    assert len(r) < 2400, "a brief, not a manual"


def test_whatever_they_ask_is_answered_simply_not_a_form_filled():
    """3 Oct: "dont restrict u … whatever the user asks, based on that analyse from
    the logs and summarise simple"."""
    r = booking_case.rule(f"why is {ID} stuck in uat?")
    assert "Answer WHATEVER they asked" in r and "Summarise SIMPLY" in r
    assert "answer to their question first" in r
    assert "No table or per-flow list unless they asked what happened overall" in r


def test_billing_is_checked_only_when_they_ask_about_it():
    """2 Oct: "dont depend more on billing … keep it for debugging if we ask"."""
    plain = booking_case.rule(f"what happened to {ID} in uat?")
    assert "Billing is out of scope unless they ask" in plain and "Invoice Triggered" not in plain
    asked = booking_case.rule(f"did {ID} reach billing in uat? invoice triggered?")
    assert "Invoice Triggered) are inbound only" in asked and "S4 countries only" in asked


def test_a_holiday_today_starts_the_quiet_now_not_at_saturday():
    import datetime as dt
    from app import instructions
    now = dt.datetime(2026, 10, 2, 23, 25).timestamp()
    spec = instructions.quiet_spec("Today it is government holiday .. don’t send plus next two "
                                   "days are weekend . So be in silent dont need to notify me "
                                   "anything", now)
    start, until = (int(x.split("=")[1]) for x in spec.split(";"))
    assert start == int(now), "starts now"
    assert dt.datetime.fromtimestamp(until) == dt.datetime(2026, 10, 5, 9, 0)


def test_the_voice_bench_never_reaches_his_whatsapp():
    """2 Oct 23:21-23:26: four bench answers landed in his WhatsApp while he
    was asking for silence."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts" / "voice_bench.py").read_text()
    assert "_notify.wa_send = _kept" in src and "_notify.notify = _kept" in src


# --- the log tool shows an id's trail, not only its errors ---------------------------

def _rec(t, service, message, level="info"):
    line = json.dumps({"timestamp": "x", "level": level.upper(), "logger": f"a.b.{service}Impl",
                       "message": message})
    return {"timestamp": t, "line": line, "service": f"telikos-{service}", "level": level,
            "trace_id": ""}


def test_a_clean_send_and_its_ack_are_in_the_trail_with_the_id_readable():
    """2 Oct: a clean SEND_TO_TMS is an INFO line; the tool kept errors only, so
    the same booking read "TMS: yes" once and "cannot confirm" the next time."""
    from app import grafana
    recs = [_rec(1, "booking-service", f"Starting workflow for {ID} and eventName :SEND_TO_TMS"),
            _rec(2, "activityplanworkflow-service", "TMS message published successfully"),
            _rec(3, "booking-service", f"Starting workflow for {ID} and eventName :SAP_TMS_ACK_FEEDBACK"),
            _rec(4, "billing-service", "No billing financialJobLines found", "error")]
    out = grafana.render_trail(grafana.trail(recs, [ID]))
    assert out.index("SEND_TO_TMS") < out.index("TMS message published") < out.index("SAP_TMS_ACK_FEEDBACK")
    assert ID in out and "ZQTERM" not in out, "the traced id reads as itself"
    assert "[error]" in out


def test_a_long_trail_keeps_every_named_event_and_no_service_crowds_it():
    from app import grafana
    recs = [_rec(i, "billing-service", f"billing step {chr(65 + i % 26)}{chr(65 + i // 26)} SEND_TO_TMS") for i in range(60)]
    recs += [_rec(100, "booking-service", f"eventName :SAP_TMS_ACK_FEEDBACK for {ID}"),
             _rec(101, "email-service", f"Received ActivityPlan for {ID}")]
    rows = [x for x in grafana.trail(recs, [ID]) if "omitted" not in x]
    text = grafana.render_trail(grafana.trail(recs, [ID]))
    assert len(rows) <= grafana.TRAIL_ROWS
    assert "SAP_TMS_ACK_FEEDBACK" in text and "email-service" in text
    assert sum(1 for x in rows if x["service"] == "telikos-billing-service") <= grafana.TRAIL_PER_SERVICE + 1
    assert "routine ones left out" in text


def test_hunting_errors_carries_no_trail():
    from app import grafana
    found = grafana.summarise([_rec(1, "booking-service", "fine")])
    assert "trail" not in found


# --- the evidence Asta reads before the investigation starts --------------------------

def test_each_flow_is_reported_in_its_own_direction():
    recs = [_rec(1, "booking-service", "ActivityPlan temporal workflow initialized/signaled"),
            _rec(2, "activityplanworkflow-service", "TMS message published successfully"),
            _rec(3, "booking-service", "Starting workflow for x and eventName :SAP_TMS_ACK_FEEDBACK"),
            _rec(4, "booking-service", "Starting workflow for x and eventName :SAP_TMS_EXECUTION_STATUS"),
            _rec(5, "booking-service", "Sending booking data to IOM for bookingId: x")]
    out = booking_case.flows(recs)
    assert "booking → TMS (two-way): sent" in out and "· ack " in out.split("booking → TMS")[1].split("\n")[0]
    assert "TMS execution status → booking (inbound): received" in out
    assert "AP → customs (UNITED) (two-way): not seen (sent) · ack not seen" in out
    assert "booking → IOM (outbound): sent" in out
    assert "billing" not in out, "billing only when asked"
    assert "AP → billing" in booking_case.flows(recs, billing=True)


def test_payload_words_are_not_a_customs_send():
    """"customs: yes, touched" came from payload fields and an email PDF heading."""
    recs = [_rec(1, "email-service", "Creation of customs product details section completed"),
            _rec(2, "booking-service", "Trying to save customsServiceOrder if absent"),
            _rec(3, "billing-service", '{"customer": "x", "eventName": "SEND_TO_TMS"}')]
    assert "AP → customs (UNITED) (two-way): not seen (sent) · ack not seen" in booking_case.flows(recs)


def test_the_investigation_brief_gets_the_evidence(monkeypatch):
    from app import grafana, responder
    monkeypatch.setenv("ASTA_GRAFANA", "1")
    monkeypatch.setattr(grafana, "enabled", lambda: True)
    asked = []

    async def records_for(term, ns, minutes=4320):
        asked.append((term, ns))
        return [_rec(1, "activityplanworkflow-service", "TMS message published successfully"),
                _rec(2, "booking-service", f"eventName :SAP_TMS_ACK_FEEDBACK for {term}")]
    monkeypatch.setattr(grafana, "records_for", records_for)
    brief = responder._waiting_brief("V", f"have we sent {ID} to TMS in uat?", "", need="x")
    out = asyncio.run(booking_case.evidence(brief))
    assert asked == [(ID, "uat")], "only the named environment — never prod by assumption"
    assert "booking → TMS (two-way): sent" in out and "SAP_TMS_ACK_FEEDBACK" in out
    assert asyncio.run(booking_case.evidence("a brief about a PR")) == ""


def test_the_worker_adds_the_evidence_to_an_analysis():
    import inspect
    from app import tasks
    assert "booking_case.evidence(prompt)" in inspect.getsource(tasks._worker)


# --- milestones and the event log, as the UI shows them ------------------------------

def _raw(t, service, line):
    return {"timestamp": t, "line": line, "service": f"telikos-{service}", "level": "info",
            "trace_id": ""}


def test_milestones_are_read_as_the_ui_tracks_them():
    """3 Oct: "always focus on event log as well as milestone … all tracked by
    milestone, as u see in the UI"."""
    recs = [_raw(1, "booking-service", json.dumps({
                "logger": "x.UpdateFeedbackActivityImpl", "message": f"Total time taken for bookingId : {ID}",
                "TIME_TAKEN_MILLIS": "1000", "WORK_PROCESS_NAME": "READY_FOR_PLANNING",
                "MILESTONE": "BOOKING_MILESTONE", "WORK_PROCESS_STATUS": "COMPLETED"})),
            _raw(9, "booking-service", json.dumps({
                "message": "Total time taken", "TIME_TAKEN_MILLIS": "6136000",
                "WORK_PROCESS_NAME": "SEND_TO_TMS", "MILESTONE": "BOOKING_MILESTONE",
                "WORK_PROCESS_STATUS": "COMPLETED"}))]
    out = booking_case.milestones(recs)
    assert out.index("READY_FOR_PLANNING · COMPLETED") < out.index("SEND_TO_TMS · COMPLETED")
    assert "took 6136.0s" in out
    assert "none logged" in booking_case.milestones([])


def test_the_event_log_lists_entries_with_their_source_and_billing_only_when_asked():
    recs = [_raw(1, "activityplanworkflow-service", json.dumps({"message":
                f"At send event history for bookingId {ID} and orderId MX1 and eventName Booking Created"})),
            _raw(2, "email-service", json.dumps({"message":
                f"Published event history for orderId MX1, booking id {ID} and event name Booking Confirmed"})),
            _raw(3, "billing-service", 'EventHistory={"eventType": "Calculate preferred billing date"}'),
            _raw(4, "event-history-service", json.dumps({"message":
                f"Data saved successfully to database with orderId: MX1 and bookingNumber: {ID}"}))]
    out = booking_case.event_log(recs)
    assert "Booking Created (activityplanworkflow)" in out and "Booking Confirmed (email)" in out
    assert "saved by event-history-service ×1" in out
    assert "billing date" not in out
    assert "Calculate preferred billing date (billing)" in booking_case.event_log(recs, billing=True)


def test_the_rule_asks_for_milestones_and_the_event_log():
    r = booking_case.rule(f"what happened to {ID} in uat?")
    assert "Milestones and the event log show upstream progress" in r
    assert "not proof of downstream completion" in r
    assert "failure, retry and eventual success" in r


def test_an_unrelated_preprod_message_cannot_change_this_cases_environment():
    from app import chat_watch
    other = "MH34CD56EF78"
    conversation = [f"Someone: {other} failed in preprod",
                    f"Vinish: booking {ID} failed in uat",
                    "Someone: the preprod issue is different"]
    answer = chat_watch._with_the_case(f"can you check booking {ID}?", "", conversation)
    assert "(environment: uat)" in answer
    assert "preprod" not in answer
    unknown = chat_watch._with_the_case(f"can you check booking {ID}?",
                                        "", [conversation[0]])
    assert "environment:" not in unknown


def test_existing_case_finding_skips_repeating_the_automatic_log_fetch(monkeypatch):
    import asyncio
    from app import grafana
    monkeypatch.setattr(grafana, "enabled", lambda: True)

    async def unexpected(*args):
        raise AssertionError("same identifier queried twice")

    monkeypatch.setattr(grafana, "records_for", unexpected)
    prompt = (booking_case.rule(f"check {ID} in uat") +
              "\n\n[Prior task #2 for this case: use the finding]")
    assert asyncio.run(booking_case.evidence(prompt)) == ""


def test_log_query_failure_is_not_reported_as_no_matching_logs(monkeypatch):
    import asyncio
    from app import grafana
    monkeypatch.setattr(grafana, "enabled", lambda: True)

    async def failed(*args):
        raise RuntimeError("Loki unavailable")

    monkeypatch.setattr(grafana, "records_for", failed)
    result = asyncio.run(booking_case.evidence(booking_case.rule(f"check {ID} in uat")))
    assert "Query failed: uat: RuntimeError: Loki unavailable" in result
    assert "no log lines returned" in result
