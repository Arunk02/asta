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
    assert "workOrderStatus=ACCEPTED transportOrderNumber=TO1234567890123" in out
    assert "executionStarted" not in out, "a stale flag on the TO copy, not TMS's word"
    assert "SAP_TMS_ACK_FEEDBACK ×2" in out and "SAP_TMS_EXECUTION_STATUS" not in out
    assert "consumer took the same message for other bookings" in out


def test_booking_events_is_a_read_tool_every_investigation_has():
    from app import capabilities, mcp_server, tasks, worker_auth
    assert capabilities.get("booking_events") is not None
    assert "booking_events" in worker_auth.READ_TOOLS and "booking_events" in mcp_server.INVESTIGATION
    assert "booking_events" in tasks.PINNED_TOOLS["analysis"]
    assert "booking_events(env, booking)" in booking_case.rule(f"did {ASKED} get the TMS ack in sit?")


def test_worked_then_failed_points_at_the_event_in_between():
    r = booking_case.rule(f"why is billing failing for {ASKED} in sit? it worked yesterday")
    assert "Worked, then failed?" in r and "between the last success and the first failure" in r
    assert "the logs win" in r and len(booking_case.rule(
        "have we sent TMS for H65ZMWX52B2 in sit? and customs UNITED?")) < 3400
    from app import temporal
    out = temporal.render_booking_events({"env": "sit", "booking": ASKED, "total": 1, "runs": [
        {"started": "2026-11-18T12:26:39", "type": "bookingEventsWorkflow", "status": "COMPLETED",
         "facts": {"eventName": "PRICING_UPDATE"}}]})
    assert "PRICING_UPDATE" in out and "runs between the last success and the failure" in out


def _row(ts, app, msg):
    return {"ts": ts, "app": app, "logger": "", "message": msg, "pod": ""}


def test_decisions_reach_a_pass_then_fail_in_the_middle_of_a_busy_booking(monkeypatch):
    from app import debugkit
    early = [_row(1000 + i, "telikos-billing-service", f"Trying to publishBillingEvent {i}") for i in range(30)]
    late = [_row(9000 + i, "telikos-activityplanworkflow-service",
                 f"Returning activityPlan: ActivityPlanDomain(status=PENDING {i})") for i in range(30)]
    verdict = [_row(5000, "telikos-billing-service",
                    f"RevalidationServiceImpl - TMS milestone is not executed for jobLineNumber: {OTHER}X")]
    calls = []

    async def lines(env, service="", terms=(), logger="", start=None, end=None, limit=40,
                    direction="forward", pod="", matching=""):
        calls.append((direction, matching))
        if matching == booking_case._VERDICT_LOKI:
            return {"rows": verdict}
        return {"rows": late if direction == "backward" else early}
    monkeypatch.setattr(debugkit, "lines", lines)
    out = asyncio.run(booking_case.outcome_lines(OTHER, "sit", hours=40))
    assert "TMS milestone is not executed" in out, "the first failure, wherever it falls"
    assert "Returning activityPlan" not in out, "a read echo is not a decision"
    assert {c[1] for c in calls} == {booking_case._OUTCOME_LOKI, booking_case._VERDICT_LOKI}
    assert ("backward", booking_case._OUTCOME_LOKI) in calls, "the newest end is read too"


def test_the_echo_filter_tolerates_double_spaces():
    assert booking_case._ECHO.search("ActivityPlan data  to build EventHistory :x")
    assert not booking_case._ECHO.search("Invoice trigger event is missing for jobLineNumber: X")


def test_analysis_evidence_carries_decisions_and_received_events(monkeypatch):
    from app import temporal

    async def decided(booking, env, hours=72):
        return [_row(1795000008.0, "telikos-billing-service",
                     f"RevalidationServiceImpl - TMS milestone is not executed for jobLineNumber: {OTHER}X")]

    def run(t, kind, name, refs=None):
        f = {"eventName": name}
        if refs is not None:
            f["_refs"] = refs
        return {"started": t, "type": kind, "status": "COMPLETED", "facts": f}

    async def events(env, booking, limit=40):
        return {"env": env, "booking": booking, "total": 3, "runs": [
            run("2026-11-18T12:00:00", "BillingWorkflow", "SERVICE_DELIVERY_EXECUTION", ["ATA", "LAST_LEG_DATE"]),
            run("2026-11-18T12:26:39", "bookingEventsWorkflow", "PRICING_UPDATE", ["ATD"]),
            run("2026-11-18T12:26:41", "BillingWorkflow", "BOOKING_UPDATED", ["ATD"])]}
    monkeypatch.setattr(booking_case, "decision_rows", decided)
    monkeypatch.setattr(temporal, "booking_events", events)
    monkeypatch.setattr(temporal, "_seconds", lambda s: 1795000000.0 if "12:26:41" in s
                        else 1794999998.0 if "12:26:39" in s else 1794998400.0)
    got = asyncio.run(booking_case._decided_and_received(OTHER, "sit", 4320))
    assert any("DECIDED" in g for g in got) and any("PRICING_UPDATE" in g for g in got)
    kept = booking_case.regressions_for(OTHER)["items"]
    assert kept and kept[0]["after"] == "PRICING_UPDATE", "a failure followed it within the window"
    assert "PRICING_UPDATE" in booking_case.snapshot_for(OTHER)["text"]


def test_booking_events_shows_what_each_run_added_and_dropped():
    from app import temporal

    def run(t, kind, refs, name="SERVICE_DELIVERY_EXECUTION"):
        return {"started": t, "type": kind, "status": "COMPLETED",
                "facts": {"eventName": name, "_refs": refs}}
    out = temporal.render_booking_events({"env": "sit", "booking": OTHER, "total": 4, "runs": [
        run("2026-11-17T17:37:57", "BillingWorkflow", ["ATD", "LAST_LEG_DATE"]),
        run("2026-11-18T12:26:39", "bookingEventsWorkflow", ["ATD"], "PRICING_UPDATE"),
        run("2026-11-18T12:26:41", "BillingWorkflow", ["ATD"], "BOOKING_UPDATED"),
    ]})
    billing = next(l for l in out.splitlines() if "12:26:41 [COMPLETED] BillingWorkflow" in l)
    update = next(l for l in out.splitlines() if "[COMPLETED] bookingEventsWorkflow" in l)
    assert "−LAST_LEG_DATE" in billing, "billing's run lost what the update did not carry"
    assert "refs" not in update, "a different workflow type is not compared"


def test_the_booking_story_runs_are_kept_first():
    from app import temporal
    rows = ([{"type": {"name": "ActivityPlanWorkflow"}, "startTime": f"2026-11-18T10:{i:02d}:00Z"}
             for i in range(50)]
            + [{"type": {"name": "bookingEventsWorkflow"}, "startTime": "2026-11-18T12:26:39Z"},
               {"type": {"name": "BillingWorkflow"}, "startTime": "2026-11-18T12:26:41Z"}])
    picked = temporal._pick_runs(sorted(rows, key=lambda r: r["startTime"]), 10)
    kinds = [temporal._of(r, "type", "name") for r in picked]
    assert len(picked) == 10 and "bookingEventsWorkflow" in kinds and "BillingWorkflow" in kinds
    assert picked == sorted(picked, key=lambda r: r["startTime"])


def test_booking_events_reads_tms_execution_from_the_work_processes():
    from app import temporal
    body = {"newServicePlan": {"transportOrders": [{"executionStarted": False}],
                               "workProcesses": [{"workProcessStatus": {
                                   "workProcessName": "CONTAINER_DELIVERY_EXECUTION",
                                   "workProcessStatus": "EXECUTED"}}]},
            "oldServicePlan": {"workProcesses": [{"workProcessStatus": {
                "workProcessName": "CONTAINER_DELIVERY_EXECUTION", "workProcessStatus": "NOT_STARTED"}}]}}
    import base64
    ev = [{"workflowExecutionStartedEventAttributes": {"input": {"payloads": [
        {"data": base64.b64encode(json.dumps("SAP_TMS_EXECUTION_STATUS").encode()).decode()},
        {"data": base64.b64encode(json.dumps(body).encode()).decode()}]}}}]
    facts = temporal._start_facts(ev)
    assert facts["_executions"] == {"CONTAINER_DELIVERY_EXECUTION": "EXECUTED"}
    assert "executionStarted" not in facts


def test_anything_that_got_worse_is_summarised_first_with_the_event_before():
    from app import temporal

    def run(t, kind, name, refs=None, status="COMPLETED", **facts):
        f = {"eventName": name, **facts}
        if refs is not None:
            f["_refs"] = refs
        return {"started": t, "type": kind, "status": status, "facts": f}
    out = temporal.render_booking_events({"env": "sit", "booking": OTHER, "total": 6, "runs": [
        run("2026-11-17T17:37:57", "BillingWorkflow", "SERVICE_DELIVERY_EXECUTION", ["ATA", "LAST_LEG_DATE"]),
        run("2026-11-18T12:26:39", "bookingEventsWorkflow", "PRICING_UPDATE", ["ATD"]),
        run("2026-11-18T12:26:39", "ActivityPlanWorkflow", "BOOKING_UPDATED"),
        run("2026-11-18T12:26:41", "BillingWorkflow", "BOOKING_UPDATED", ["ATD"]),
        run("2026-11-18T13:00:00", "ActivityPlanWorkflow", "SEND_TO_TMS", status="COMPLETED", feedback="SUCCESS"),
        run("2026-11-18T13:05:00", "bookingEventsWorkflow", "CUSTOMS_UPDATE"),
        run("2026-11-18T13:05:02", "ActivityPlanWorkflow", "SEND_TO_TMS", status="FAILED", feedback="REJECTED"),
    ]})
    lines = out.splitlines()
    assert lines[1].startswith("What got worse between runs")
    assert "lost ref ATA; lost ref LAST_LEG_DATE — 2s after PRICING_UPDATE (12:26:39)" in out
    assert "feedback SUCCESS→REJECTED" in out and "run COMPLETED→FAILED" in out, "not only references"
    assert "2s after CUSTOMS_UPDATE" in out


def test_tms_moving_backward_counts_as_worse():
    from app import temporal
    ex = lambda t, v: {"started": t, "type": "bookingEventsWorkflow", "status": "COMPLETED",
                       "facts": {"eventName": "SAP_TMS_EXECUTION_STATUS",
                                 "_executions": {"CONTAINER_DELIVERY_EXECUTION": v}}}
    out = temporal.render_booking_events({"env": "sit", "booking": OTHER, "total": 2, "runs": [
        ex("2026-11-18T10:00:00", "EXECUTED"), ex("2026-11-18T11:00:00", "IN_EXECUTION")]})
    assert "TMS CONTAINER_DELIVERY EXECUTED→IN_EXECUTION" in out
    calm = temporal.render_booking_events({"env": "sit", "booking": OTHER, "total": 2, "runs": [
        ex("2026-11-18T10:00:00", "IN_EXECUTION"), ex("2026-11-18T11:00:00", "EXECUTED")]})
    assert "What got worse" not in calm, "progress is not a regression"


def test_an_event_that_carries_no_references_is_not_losing_them():
    from app import temporal
    runs = [{"started": "2026-11-18T12:00:00", "type": "bookingEventsWorkflow", "status": "COMPLETED",
             "facts": {"eventName": "PRICING_UPDATE", "_refs": ["ATA", "ATD"]}},
            {"started": "2026-11-18T12:01:00", "type": "bookingEventsWorkflow", "status": "COMPLETED",
             "facts": {"eventName": "ACTIVITYPLAN_FEEDBACK", "workProcessStatus": "COMPLETED"}}]
    out = temporal.render_booking_events({"env": "sit", "booking": OTHER, "total": 2, "runs": runs})
    assert "What got worse" not in out


# --- read once per case; the answer is checked against what code found -------

def _reg(after="PRICING_UPDATE", items=("lost ref ATA", "lost ref LAST_LEG_DATE"), at=1000.0):
    return {"started": "2026-11-18T12:26:41", "at": at, "run": "BillingWorkflow",
            "event": "BOOKING_UPDATED", "items": list(items), "after": after,
            "after_started": "2026-11-18T12:26:39", "gap": 2}


def test_only_regressions_a_failure_followed_count():
    verdict = [_row(1500.0, "billing", "TMS milestone is not executed for jobLineNumber: X")]
    assert booking_case.failing_regressions([_reg(at=1000.0)], verdict)
    assert not booking_case.failing_regressions([_reg(at=1000.0)], []), "nothing failed after"
    assert not booking_case.failing_regressions([_reg(at=1000.0)],
                                                [_row(9000.0, "billing", "TMS milestone is not executed for x")])
    bad = _reg(items=("run COMPLETED→FAILED",))
    assert booking_case.failing_regressions([bad], []), "moving to a failure is itself the failure"


def _stored(task_id=9201, question="why is billing rejecting it, where does it break?"):
    import json
    booking_case.remember_case(task_id, [OTHER], "sit", question)
    store.kv_set(f"case_regressions:{OTHER}", json.dumps({"env": "sit", "at": time.time(),
                                                          "items": [_reg()]}))
    return task_id


def test_an_answer_that_walks_past_the_regression_is_caught():
    tid = _stored()
    miss = answers.ignores_regression(tid, "Billing rejects it: chassis filtered by a charge-type gate.")
    assert "PRICING_UPDATE" in miss and "lost ref ATA" in miss
    assert answers.ignores_regression(tid, "the pricing update dropped them") == ""
    assert answers.ignores_regression(tid, "LAST_LEG_DATE was wiped at 12:26") == ""


def test_the_check_stays_out_of_questions_that_are_not_about_a_break():
    tid = _stored(9202, "did it reach TMS and get the ack?")
    assert answers.ignores_regression(tid, "yes, reached TMS, ack back") == ""


def test_one_reask_then_the_second_answer_stands(monkeypatch):
    from app import tasks
    tid = _stored(9203)
    calls = []

    async def run(task_id, t, prompt):
        calls.append(prompt)
        return "ANALYSIS:\nthe PRICING_UPDATE at 12:26 dropped ATA\nREPLY:\nok"
    monkeypatch.setattr(tasks, "_run_simple", run)
    first = "ANALYSIS:\nchassis filtered in AP\nREPLY:\nchassis filtered"
    out = asyncio.run(tasks._reask_on_ignored_regression(tid, {}, "brief", first))
    assert "PRICING_UPDATE" in out and len(calls) == 1
    assert "Your previous answer" in calls[0] and "lost ref ATA" in calls[0]
    calls.clear()
    assert asyncio.run(tasks._reask_on_ignored_regression(tid, {}, "brief", out)) == out
    assert not calls, "an answer that addresses it is not asked again"


def test_evidence_is_read_once_per_case_and_refetched_on_recheck(monkeypatch):
    import json
    store.kv_set(f"case_snapshot:{OTHER}", json.dumps({"env": "sit", "at": time.time(),
                                                       "text": "SNAPSHOT-TEXT"}))
    brief = f"This is about a specific case — {OTHER}, in the sit environment."
    assert "SNAPSHOT-TEXT" in asyncio.run(booking_case.evidence(brief))
    from app import grafana
    monkeypatch.setattr(grafana, "enabled", lambda: False)
    assert asyncio.run(booking_case.evidence(brief + " please recheck it")) == ""
    store.kv_set(f"case_snapshot:{OTHER}", json.dumps({"env": "sit", "at": time.time() - 3600,
                                                       "text": "OLD"}))
    assert asyncio.run(booking_case.evidence(brief)) == "", "a stale snapshot is not reused"


def test_log_reads_fall_back_to_the_grafana_mcp(monkeypatch):
    from app import debugkit, grafana

    async def broken(path, params, uid=""):
        raise grafana.GrafanaError("API down", 502)

    async def via_mcp(query, start, end, limit, why, ns=""):
        return [{"timestamp": 1.0, "line": "hello", "labels": {"app": "telikos-x"}}], str(why)
    monkeypatch.setattr(debugkit, "_ask", broken)
    monkeypatch.setattr(grafana, "_via_mcp", via_mcp)
    got = asyncio.run(debugkit.lines("sit", "", (OTHER,), "", None, None, 10))
    assert got["via"] == "mcp fallback" and got["rows"] and not got.get("error")


def test_a_correction_carries_the_snapshot_and_the_regression():
    import json
    from app import main
    tid = _stored(9204)
    store.kv_set(f"case_snapshot:{OTHER}", json.dumps({"env": "sit", "at": time.time(),
                                                       "text": "DECIDED LINES"}))
    anchor = main._case_anchor({"task_id": tid, "who": "Lead"})
    assert "What got worse before the failure" in anchor and "PRICING_UPDATE" in anchor
    assert "DECIDED LINES" in anchor
