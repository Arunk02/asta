"""13 Oct: "see how u analysed the findings now, i want same to asta".

What made the difference on Vinish's two TMS acks (12 Oct), and what went wrong
for Deepa, each pinned here. Grafana and Prometheus are faked; the shapes are
the real ones Loki and Prometheus returned that day.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import datetime, timezone

import pytest

from app import (answers, booking_case, chat_watch, copilot_cli, debugkit, grafana, main,
                 roles, store, temporal, tool_index, understand)

UTC = timezone.utc
DEEPA_LINE = ("Hi Deepa, no link - logs are in QA not prod for this one. Billing did get "
              "published but got a 400 back, status PENDING. Want full trail?")


@pytest.fixture(autouse=True)
def _grafana_env(monkeypatch):
    monkeypatch.setenv("ASTA_GRAFANA_URL", "https://grafana.example")
    monkeypatch.setenv("ASTA_GRAFANA_NAMESPACE", "telikos-prod")
    monkeypatch.setenv("ASTA_GRAFANA_CLUSTERS", "ohp-prod-west-1")
    monkeypatch.setenv("ASTA_GRAFANA_ENVS", "prod,preprod,uat,sit,qa,dev")
    store.init()


# --- the queries are built right ------------------------------------------------------

def test_perf_logs_are_read_where_they_live():
    assert grafana.resolve_namespace("perf") == "telikos-spt"
    assert grafana.resolve_namespace("prod") == "telikos-prod"
    assert grafana.resolve_namespace("qa") == "telikos-qa"


def test_two_services_are_two_alternatives_not_one_name_with_a_pipe():
    sel = debugkit.selector("telikos-prod", "booking|activityplan")
    assert 'app=~".*(booking|activityplan).*"' in sel
    assert 'k8s_cluster="ohp-prod-west-1"' in sel and 'container!~"kuma-sidecar' in sel


def test_no_environment_means_every_environment():
    assert debugkit.envs_for("") == ["prod", "preprod", "uat", "sit", "qa", "dev"]
    assert debugkit.envs_for("qa") == ["qa"]


def test_every_line_has_utc_and_local_time():
    s = debugkit.stamp(datetime(2026, 10, 12, 9, 50, 3, 962000, tzinfo=UTC).timestamp())
    assert s.startswith("2026-10-12 09:50:03.962 UTC (") and "local)" in s


def test_the_log_link_is_a_grafana_explore_link_for_the_same_query():
    link = debugkit.explore_link('{namespace="telikos-qa"} |= "H7YVFKPT6HM"',
                                 datetime(2026, 10, 12, 6, 30, tzinfo=UTC),
                                 datetime(2026, 10, 12, 6, 40, tzinfo=UTC))
    assert link.startswith("https://grafana.example/explore?") and "H7YVFKPT6HM" in link


# --- where: counts, and a window that is too big ---------------------------------------

def test_a_window_too_big_for_loki_is_shortened_and_says_so(monkeypatch):
    asked = []

    async def fake(path, params, uid=""):
        asked.append(params["query"])
        if "[2880m]" in params["query"]:
            raise grafana.GrafanaError("the query would read too many bytes (264 GiB)", 400)
        return {"data": {"result": [{"metric": {"app": "telikos-billing-service"}, "value": [0, "294"]}]}}
    monkeypatch.setattr(grafana, "_get", fake)
    found = asyncio.run(debugkit.counts("H7Y5BWNBNX9", "prod", minutes=2880))
    text = debugkit.render_counts(found)
    assert "telikos-billing-service 294" in text and "last 16h00m" in text
    assert "not that it never happened" in text


def test_a_rate_limit_is_waited_out(monkeypatch):
    calls = {"n": 0}

    async def fake(path, params, uid=""):
        calls["n"] += 1
        if calls["n"] < 3:
            raise grafana.GrafanaError("Rate limit exceeded for client", 429)
        return {"data": {"result": []}}

    async def no_wait(*_):
        return None
    monkeypatch.setattr(grafana, "_get", fake)
    monkeypatch.setattr(debugkit.asyncio, "sleep", no_wait)
    asyncio.run(debugkit.counts("x", "prod"))
    assert calls["n"] == 3


# --- the id that was typed wrong ---------------------------------------------------------

def test_a_mistyped_id_is_found_by_how_it_starts(monkeypatch):
    line = json.dumps({"message": "Successfully consumed … bookingNumber: H7QKN8NGN9R"})

    async def fake(path, params, uid=""):
        return {"data": {"result": [{"stream": {"app": "telikos-booking-service"},
                                     "values": [["1791771854037575041", line]] * 3}]}}
    monkeypatch.setattr(grafana, "_get", fake)
    text = debugkit.render_find(asyncio.run(debugkit.find_id("H7QKN8NGNOR", "prod")))
    assert "H7QKN8NGN9R (3 lines)" in text


def test_look_alike_characters_count_as_closer():
    assert debugkit.near("H7QKN8NGNOR", "H7QKN8NGN9R") < debugkit.near("H7QKN8NGNOR", "H7QKN8NGNXR")


# --- missing log, or missing event? -----------------------------------------------------

def _gaps_fakes(monkeypatch):
    start = datetime(2026, 10, 12, 9, 46, tzinfo=UTC)
    minutes = [int(start.timestamp()) + 60 * i for i in range(1, 11)]
    # 09:47 … 09:50 logged (09:49 nearly silent), 09:51 and 09:52 ABSENT, then back.
    vm5rh = [[t, str(v)] for t, v in zip(minutes[:4], [5316, 2758, 211, 1966])]
    vm5rh += [[minutes[6], "3632"], [minutes[7], "2042"], [minutes[8], "1733"], [minutes[9], "1561"]]
    steady = [[t, "4400"] for t in minutes]

    async def fake(path, params, uid=""):
        q = params["query"]
        if uid:                                                   # Prometheus
            if "loki_write_dropped_entries_total" in q:
                return {"data": {"result": [{"metric": {"node_name": "vmss0000s7", "reason": "ingester_error"},
                                             "values": [[minutes[3], "5637"], [minutes[4], "2030"],
                                                        [minutes[5], "237"]]}]}}
            return {"data": {"result": []}}
        return {"data": {"result": [
            {"metric": {"pod": "booking-vm5rh", "node_name": "vmss0000s7"}, "values": vm5rh},
            {"metric": {"pod": "booking-r92jm", "node_name": "vmss0000og"}, "values": steady}]}}
    monkeypatch.setattr(grafana, "_get", fake)
    return start


def test_a_healthy_pod_that_went_quiet_while_its_node_dropped_lines_lost_logs_not_events(monkeypatch):
    start = _gaps_fakes(monkeypatch)
    found = asyncio.run(debugkit.gaps("prod", "booking-service", start,
                                      datetime(2026, 10, 12, 9, 56, tzinfo=UTC)))
    text = debugkit.render_gaps(found)
    assert "booking-vm5rh kept running but its logs did not all arrive" in text
    assert "09:51" in text and "09:52" in text, "an absent minute is a zero, not skipped"
    assert "7904 lines (ingester_error)" in text
    assert "booking-r92jm kept running" not in text


# --- what actually ran ------------------------------------------------------------------

def test_a_workflow_history_shows_what_started_it_and_with_what():
    payload = base64.b64encode(b'{"uniqueIdentifier":"C8C300D8C62111F1B0B200000EC59EBB"}').decode()
    events = [{"eventTime": "2026-10-12T09:50:03.927Z", "eventType": "EVENT_TYPE_WORKFLOW_EXECUTION_STARTED",
               "workflowExecutionStartedEventAttributes": {
                   "workflowType": {"name": "bookingEventsWorkflow"}, "identity": "ohp-telikos",
                   "input": {"payloads": [{"data": payload}]}}},
              {"eventTime": "2026-10-12T09:50:05.038Z", "eventType": "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED",
               "activityTaskScheduledEventAttributes": {"activityType": {"name": "sendEventToIomForToAck"}}}]
    text = temporal.render_history(events, "prod", "H7Y5BWNBNX9_BOOKINGEVENTS")
    assert "C8C300D8C62111F1B0B200000EC59EBB" in text and "sendEventToIomForToAck" in text


# --- the tools stay with the conversation -----------------------------------------------

VINISH = ('"T00078580047221", - H7QKN8NGNOR   TO2934106728115. - H7Y5BWNBNX9   for this two '
          'bookings can you check i see kafka event logs are missing')


@pytest.mark.parametrize("follow_up", ["prod", "check in qa", "in sit", "uat"])
def test_a_one_word_answer_keeps_the_debugging_tools(follow_up):
    tool_index._sticky.pop("c13", None)
    tool_index.select_sticky("c13", VINISH)
    kept = tool_index.select_sticky("c13", follow_up)
    assert set(tool_index.DEBUG_TOOLS) | {"grafana_logs"} <= set(kept)


def test_prod_on_its_own_still_carries_the_booking_it_answers(monkeypatch):
    conv = store.create_conversation("claude_cli", None)
    store.add_ui_message(conv["id"], "user", "H7Y5BWNBNX9 why is the consume log missing", {})
    ctx = copilot_cli.turn_context("prod", conv["id"])
    assert "H7Y5BWNBNX9" in ctx and "prod environment" in ctx and "log_gaps" in ctx


def test_a_why_question_about_a_booking_gets_the_whole_toolkit():
    need = set(tool_index.required_for("why is the ack for H7Y5BWNBNX9 not consumed in prod?"))
    assert set(tool_index.DEBUG_TOOLS) <= need


def test_why_did_you_send_brings_the_conversation_record():
    assert "conversation_status" in tool_index.required_for("why send deepa without my approval")


# --- what goes out without his yes ------------------------------------------------------

@pytest.mark.parametrize("line, may", [
    (DEEPA_LINE, False),
    ("Hi Deepa, no AP workflow for H7YVFKPT6HM in preprod/sit, so not sent to billing. "
     "Which env is this in?", False),
    ("Billing got a 400 back, want full trail?", False),
    ("Sure — which environment is H7YVFKPT6HM in: prod, preprod, sit or uat? I'll check the logs there.", True),
    ("Sure — which booking is it? Share the booking number and I'll check the logs.", True),
    ("Is there anything else you need from my side?", True),
    ("Was it sent to billing?", True),
    ("hi Shabda, yes tell me", True),
])
def test_only_a_line_that_asks_may_go_alone(line, may):
    assert (understand.unapproved_line_problem(line) == "") is may


def test_deepas_message_is_held_for_his_yes(monkeypatch):
    shown, sent = [], []

    async def present(**kw):
        shown.append(kw)
        return True

    async def he_replied(*a, **k):
        return False

    async def send(chat, line):
        sent.append(line)
    monkeypatch.setattr(answers, "present", present)
    monkeypatch.setattr(chat_watch, "he_replied_since", he_replied)
    monkeypatch.setattr("app.teams_bridge.send_automatic", send)
    assert asyncio.run(chat_watch._say("Deepa Singh", DEEPA_LINE)) is False
    assert not sent and shown[0]["reply"] == DEEPA_LINE
    assert "states something" in shown[0]["analysis"]


def test_a_finished_task_cannot_slip_findings_out_as_a_question():
    assert not answers._is_clarifying(DEEPA_LINE, "Deepa Singh")
    assert answers._is_clarifying("Which environment is H7YVFKPT6HM in?", "Deepa Singh")


def test_nothing_goes_alone_when_he_says_so(monkeypatch):
    monkeypatch.setenv("ASTA_SAY_WITHOUT_YES", "0")
    assert not answers._is_clarifying("Which environment is H7YVFKPT6HM in?", "Deepa Singh")


# --- and Asta does not deny what it did --------------------------------------------------

def test_a_reply_denying_a_send_asta_made_is_corrected():
    store.record_outcome("thread", "said", subject="Deepa Singh", detail=DEEPA_LINE)
    reply = ("Nothing's been sent to Deepa — the last message in that chat (\"no link - logs "
             "are in QA...\") was your own, typed by you.")
    found = main.denied_send(reply)
    assert found and found["to"] == "Deepa Singh" and "logs are in QA" in found["text"]
    assert main.denied_send("I'll draft a reply to Deepa for your yes.") is None


def test_the_method_names_its_tools_and_never_guesses_an_environment():
    for tool in tool_index.DEBUG_TOOLS:
        assert tool in roles.CASE_METHOD
    assert "never guess one" in roles.CASE_METHOD
    assert "exceptionCode 400" in roles.CASE_METHOD
    assert booking_case.WHY.search("why is the consume log missing")


# --- 13 Oct, 11:31: Vinish's Temporal link -----------------------------------------------

VINISH_LINK = ("https://prod-westeurope-01-web.service-composition.maersk.io/namespaces/"
               "telikos-temporal-prod/workflows/H22TCMNTBTW_BOOKINGEVENTS/"
               "01a00056-5b7b-75c9-a123-9c6c227586e0/history")
ASKED = ("Vinish shared one temporal workflow , billing team saying location function is "
         "missing , can you debug and analyse whether from booking it send , and where it "
         "get lost like that")


def test_a_temporal_link_carries_env_workflow_and_run():
    assert temporal.parse_link(VINISH_LINK) == (
        "prod", "H22TCMNTBTW_BOOKINGEVENTS", "01a00056-5b7b-75c9-a123-9c6c227586e0")
    assert "H22TCMNTBTW" in booking_case.ids(VINISH_LINK)


def test_the_workflow_tool_takes_the_link_itself(monkeypatch):
    seen = {}

    async def history(env, wid, run=""):
        seen.update(env=env, wid=wid, run=run)
        return []
    monkeypatch.setattr(temporal, "history", history)
    from app import agent
    asyncio.run(agent.temporal_workflow(workflow_id=VINISH_LINK, history=True))
    assert seen == {"env": "prod", "wid": "H22TCMNTBTW_BOOKINGEVENTS",
                    "run": "01a00056-5b7b-75c9-a123-9c6c227586e0"}


def test_teams_keeps_the_whole_link_not_its_shortened_text():
    from app import teams_bridge
    js = [v for v in vars(teams_bridge).values()
          if isinstance(v, str) and "workflow id Vinish shared" in v][0]
    assert "a.textContent = a.getAttribute('href')" in js


def test_a_link_is_something_to_check_not_a_hello():
    assert chat_watch._concrete("Vinish Kumar: " + VINISH_LINK)


def test_what_vinish_shared_is_read_from_his_chat_not_asked_for():
    need = set(tool_index.required_for(ASKED))
    assert {"teams_read_chat", "temporal_workflow"} <= need
    assert set(tool_index.DEBUG_TOOLS) <= need
    ctx = copilot_cli.turn_context(ASKED)
    assert 'teams_read_chat "Vinish"' in ctx and "Never ask him to paste" in ctx
    assert roles.role_for(ASKED) == "debugging"
