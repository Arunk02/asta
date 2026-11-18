"""A colleague's ask is answered about the booking the thread is about (18 Nov).

A reporter posted a Solar link to one booking ("one of UAT orders", but the link
was a preprod host). Twenty minutes later another colleague asked Arun "could you
check this". The ask had no id, so the analysis had no case, stayed in UAT on the
words, and "confirmed" an unrelated error logged under another booking. These
pin the seams: case from the thread, environment from the link, the cause on
the asked booking, a reply that stops at another team's boundary, a draft that
says who already answered, and a correction that keeps its draft and its case.
"""
from __future__ import annotations

import asyncio
import json
import time

from app import answers, booking_case, debug_hints, loop, responder, store, verdict

ASKED = "MQWXZBKRTP7V"          # synthetic 12-char servicePlanNumber
OTHER = "JKWPZXRTB9N"           # synthetic 11-char bookingNumber of another case
LINK = f"https://solar-preprod.maersk.io/inland/booking/{ASKED}"
THREAD = (f"Reporter: a uat order of ours is waiting on the TMS step, does it clear by "
          f"itself? {LINK}Maersk :: Inland BookingSolarsolar-preprod.maersk.io {LINK}\n"
          "Lead: mine cleared on their own\n"
          "Reporter: it has not moved yet, quite urgent")
ASK = "Colleague A, Arun can you both take a look (replying to an earlier message)"


def test_solar_link_names_the_environment_and_the_id():
    assert booking_case.link_env(LINK) == "preprod"
    assert booking_case.link_env("https://solar-uat.maersk.io/inland/booking/x") == "uat"
    assert booking_case.link_env("https://solar.maersk.io/inland/booking/x") == "prod"
    # Teams glues the preview title onto the link.
    assert booking_case.ids(f"{LINK}Maersk :: Inland Booking") == [ASKED]
    assert booking_case.ids(f"https://solar-uat.maersk.io/inland/booking/{OTHER}Maersk") == [OTHER]


def test_the_link_beats_the_words():
    assert booking_case.env_of(f"one of UAT orders {LINK}") == "preprod"
    assert booking_case.env_of("one of UAT orders") == "uat"


def test_an_ask_with_no_id_takes_the_case_from_the_thread():
    assert booking_case.thread_ids(ASK, THREAD) == [ASKED]
    assert booking_case.thread_env(ASK, THREAD) == "preprod"
    found, env = responder.case_of("Lead", ASK, THREAD)
    assert found == [ASKED] and env == "preprod"


def test_the_brief_carries_the_case_and_its_environment():
    brief = responder._waiting_brief("Lead", ASK, THREAD, need="check send to TMS")
    assert ASKED in brief and 'namespace="preprod"' in brief
    assert "logged under THIS booking" in brief
    assert "another team's system" in brief


def test_the_hint_follows_the_link_and_lets_evidence_move_it():
    hint = debug_hints.hints("", f"check this stuck booking in UAT {LINK}",
                             f"check this stuck booking in UAT {LINK}")
    assert "Environment: PREPROD (from the Solar link)" in hint
    assert "follow the id there" in hint
    words_only = debug_hints.hints("", "check why it is stuck in UAT", "check why it is stuck in UAT")
    assert "Environment: UAT" in words_only and "only if he names it" not in words_only


def test_an_answer_about_another_booking_is_flagged():
    wrong = (f"Confirmed cause: NPE in the receiver on every callback. One failing booking "
             f"is {OTHER}.")
    assert verdict.off_case(wrong, [ASKED]) == [OTHER]
    assert verdict.off_case(f"{ASKED}: sent fine, ack late. Also saw {OTHER}.", [ASKED]) == []
    assert verdict.off_case("TO1234567890123 was sent fine.", [ASKED]) == []
    assert verdict.off_case(wrong, []) == []


def test_the_off_case_note_uses_the_remembered_case():
    booking_case.remember_case(9101, [ASKED], "preprod", "send to TMS still not working")
    note = answers.off_case_note(9101, f"NPE on {OTHER}", "")
    assert OTHER in note and ASKED in note
    assert answers.off_case_note(9101, f"{ASKED} sent fine", "") == ""


def test_pairing_clears_an_answer_that_names_the_booking_number(monkeypatch):
    booking_case.remember_case(9102, [ASKED], "preprod", "q")

    async def paired(identifier, env="", budget=6.0):
        return OTHER, "booking-service"
    monkeypatch.setattr(booking_case, "booking_for", paired)
    note = asyncio.run(answers._with_its_pair(9102, f"{OTHER}: sent fine, ack late", ""))
    assert note == ""


def test_the_reply_is_simple_and_stops_at_their_boundary():
    rider = answers.brief_rider("Lead")
    assert "one or two plain lines" in rider
    assert "exact cause not sure yet" in rider
    assert "asked TMS for the reason" in rider


def test_a_draft_says_who_already_answered(monkeypatch):
    now = time.time()
    rows = [{"sender": "Colleague A", "text": "fixed on our side now", "sent_at": now - 60},
            {"sender": "Somebody Else", "text": "unrelated", "sent_at": now - 50},
            {"sender": "Lead", "text": "thanks", "sent_at": now - 40}]
    monkeypatch.setattr(store, "teams_messages", lambda **k: rows)
    got = answers.answered_since("Deploy group", now - 600, ASK, "Lead")
    assert "Colleague: “fixed on our side now”" in got
    assert "unrelated" not in got and "thanks" not in got
    assert answers.answered_since("Deploy group", None, ASK, "Lead") == ""


def test_a_correction_keeps_the_draft_it_corrects():
    loop.note_rejected({"what": "draft for the group", "to": "Deploy group"})
    assert loop.last_rejected_to() == "Deploy group"


def test_a_correction_restates_the_case():
    from app import main
    booking_case.remember_case(9103, [ASKED], "preprod", "send to TMS still not working")
    anchor = main._case_anchor({"task_id": 9103, "who": "Lead"})
    assert ASKED in anchor and "preprod" in anchor and "send to TMS still not working" in anchor
    assert main._case_anchor({"task_id": 9999}) == ""


def test_the_case_survives_as_json():
    booking_case.remember_case(9104, [ASKED, OTHER], "uat", "x" * 1000)
    case = json.loads(store.kv_get("case_of_task:9104"))
    assert case["ids"] == [ASKED, OTHER] and len(case["question"]) == 400
    assert booking_case.case_for(None) == {}


def test_words_in_the_ask_do_not_beat_the_link_in_the_thread():
    brief = responder._waiting_brief("Lead", "is UAT also stuck? can you both take a look", THREAD,
                                     need="check send to TMS")
    assert 'namespace="preprod"' in brief and 'namespace="uat"' not in brief


def test_the_case_line_is_not_read_as_his_words():
    from app import main, resume, scorecard
    booking_case.remember_case(9105, [ASKED], "preprod", "is the TMS step done")
    prompt = ("Arun did NOT approve sending the draft as-is. His feedback:\nnot billing\n\n"
              "Revise accordingly — change only what he asked for."
              + main._case_anchor({"task_id": 9105, "who": "Lead"}))
    assert resume.ranking_text(prompt).strip() == "not billing"
    assert scorecard.his_words(prompt).strip() == "not billing"


def test_the_case_starts_from_the_flow_documents():
    from app import tasks
    r = booking_case.rule(f"has {ASKED} reached TMS in preprod?")
    assert "search_knowledge" in r and "NAM" in r and "TMS or non-TMS" in r
    assert r.index("Pick its flow") < r.index("what DID happen")
    assert "Mark each step done" in r
    assert "search_knowledge" in tasks.PINNED_TOOLS["analysis"]


def test_a_reply_that_promises_work_is_flagged():
    assert answers.promises_work("nothing reached the containers. we'll fix that null case, will update u")
    assert answers.promises_work("I'll raise a PR for it today")
    assert answers.promises_work("sent fine, the ack from TMS came late — asked TMS for the reason") == ""
    assert answers.promises_work("will update u once I hear back") == ""


def test_a_jira_smart_link_keeps_its_booking():
    from app import jira
    adf = {"type": "doc", "content": [{"type": "paragraph", "content": [
        {"type": "text", "text": "Booking Number: "},
        {"type": "inlineCard", "attrs": {"url": f"https://solar-cdt.maersk.io/inland/booking/?ref={ASKED}"}}]},
        {"type": "paragraph", "content": [
            {"type": "text", "text": "the booking",
             "marks": [{"type": "link", "attrs": {"href": "https://example.test/b/1"}}]}]}]}
    text = jira._adf_to_text(adf)
    assert booking_case.ids(text) == [ASKED]
    assert "the booking (https://example.test/b/1)" in text


def test_solar_cdt_is_sit():
    assert booking_case.link_env(f"https://solar-cdt.maersk.io/inland/booking/?ref={ASKED}") == "sit"


def test_an_earlier_finding_that_only_named_them_is_not_their_case():
    t = store.create_task("Lead asked: Colleague A, could you check this", "analysis", "p", None)
    store.update_task(t["id"], status="done", teams_chat="Deploy group",
                      result=f"ANALYSIS: NPE on {OTHER}\nREPLY: x")
    out = responder.case_so_far("Colleague A", "Colleague A", case_ids=[ASKED])
    assert OTHER not in out
    mine = store.create_task("Colleague A asked: check it", "analysis", "p", None)
    store.update_task(mine["id"], status="done", teams_chat="Colleague A",
                      result=f"ANALYSIS: {ASKED} sent fine\nREPLY: y")
    assert ASKED in responder.case_so_far("Colleague A", "Colleague A", case_ids=[ASKED])


def test_a_ticket_with_no_booking_in_the_ask_is_read_first():
    out = responder.playbook("https://example.atlassian.net/browse/BEPTELIKOS-1234 it is a blocker")
    assert "BEPTELIKOS-1234" in out and "jira_issue" in out and "solar-cdt is sit" in out
    assert "jira_issue" not in responder.playbook(f"BEPTELIKOS-1234 for {ASKED} in sit")


def test_a_tanglish_promise_is_flagged_too():
    assert answers.promises_work("idhu code bug, fix pannittu ungaluku ping panren")
    assert answers.promises_work("naan deploy panren evening")
    assert answers.promises_work("check panren, update panren") == ""


def test_booking_events_lists_every_event_decoded(monkeypatch):
    import base64
    from app import temporal

    def b64(v):
        return base64.b64encode(json.dumps(v).encode()).decode()
    rows = [{"execution": {"workflowId": f"{OTHER}_BOOKINGEVENTS", "runId": f"r{i}"},
             "type": {"name": "bookingEventsWorkflow"}, "status": "WORKFLOW_EXECUTION_STATUS_COMPLETED",
             "startTime": f"2026-11-18T10:4{i}:00Z"} for i in range(3)]
    inputs = {"r0": ["READY_FOR_PLANNING", {"booking": {"bookingStatus": "BOOKED"}}],
              "r1": ["SAP_TMS_ACK_FEEDBACK", {"to": {"workOrderStatus": "ACCEPTED",
                                                     "executionStarted": False,
                                                     "transportOrderNumber": "TO1234567890123"}}],
              "r2": ["SAP_TMS_ACK_FEEDBACK", {"to": {"workOrderStatus": "ACCEPTED", "version": "002"}}]}

    async def workflows(env, query="", limit=20):
        assert f'STARTS_WITH "{OTHER}"' in query
        return list(reversed(rows))

    async def history(env, wid, run=""):
        return [{"workflowExecutionStartedEventAttributes": {"input": {"payloads": [
            {"data": b64(v)} for v in inputs[run]]}}}]
    monkeypatch.setattr(temporal, "workflows", workflows)
    monkeypatch.setattr(temporal, "history", history)
    out = temporal.render_booking_events(asyncio.run(temporal.booking_events("sit", OTHER)))
    lines = out.splitlines()
    assert "3 workflow run(s)" in lines[0] and "READY_FOR_PLANNING" in lines[1], "oldest first"
    assert "workOrderStatus=ACCEPTED executionStarted=False transportOrderNumber=TO1234567890123" in out
    assert "SAP_TMS_ACK_FEEDBACK ×2" in out and "SAP_TMS_EXECUTION_STATUS" not in out
    assert "consumer took the same message for other bookings" in out


def test_booking_events_is_a_read_tool_every_investigation_has():
    from app import capabilities, mcp_server, tasks, worker_auth
    assert capabilities.get("booking_events") is not None
    assert "booking_events" in worker_auth.READ_TOOLS and "booking_events" in mcp_server.HOT
    assert "booking_events" in tasks.PINNED_TOOLS["analysis"]
    assert "booking_events(env, booking)" in booking_case.rule(f"did {ASKED} get the TMS ack in sit?")
