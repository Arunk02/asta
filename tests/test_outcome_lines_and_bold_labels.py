"""The answer starts from what the system logged it decided (18 Nov, live).

Re-test of the VTS case after #294: the evidence was fetched under the right
booking, the code was read right (import without dates -> FAILED), and the
answer still concluded FAILED from "status=PENDING" -- skipping the line one
millisecond later: "Ready-for-planning resolved SUCCESS able to fetch VTS
dates". It ended CONFIRMED, and the check missed "**Not confirmed**:".
Synthetic ids only.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app import agent, booking_case, debugkit, temporal, verdict

BOOKING = "JXCCCC2222D"


def _row(second, message, logger="VesselTrackingRegistrationActivityImpl"):
    ts = dt.datetime(2026, 11, 17, 8, 42, second, tzinfo=dt.timezone.utc).timestamp()
    return {"ts": ts, "pod": "booking-abc-1", "app": "booking-service", "logger": logger,
            "message": message}


ROWS = [
    _row(41, f"Registering vessel tracking for orderId {BOOKING}"),
    _row(41, f"Updating vessel tracking registration response for orderId {BOOKING}: "
             "status=PENDING, message=Vessel data not yet available"),
    _row(41, f"Ready-for-planning resolved SUCCESS able to fetch VTS dates for orderId {BOOKING}"),
    _row(43, "TMS message published successfully with payload {...}", "KafkaProducerServiceImpl"),
    _row(44, "Fetched rules of tasks for eventName SEND_TO_TMS", "ActivityPlanWorkflowImplV2"),
]


@pytest.fixture
def logs(monkeypatch):
    asked = []

    async def lines(env, service, terms, logger, start, end, limit, *a, **k):
        asked.append((env, tuple(terms), limit))
        return {"rows": ROWS if BOOKING in terms else []}

    monkeypatch.setattr(debugkit, "lines", lines)
    return asked


def test_only_the_decisions_are_kept_oldest_first(logs):
    out = asyncio.run(booking_case.outcome_lines(BOOKING, "sit"))
    assert "resolved SUCCESS able to fetch VTS dates" in out
    assert "status=PENDING" in out and "published successfully" in out
    assert "Registering vessel tracking" not in out and "Fetched rules" not in out
    assert out.index("status=PENDING") < out.index("resolved SUCCESS")
    assert "not what a status field implies" in out


@pytest.fixture
def world(logs, monkeypatch):
    async def counts(term, env="all", hours=72, **k):
        return f"Lines with “{term}” by app: sit booking-service 40"

    async def workflows(env="", query="", limit=8, **k):
        if env == "sit":
            return (f'1 workflow(s) in sit matching WorkflowId STARTS_WITH "{BOOKING}":\n'
                    f"  [WORKFLOW_EXECUTION_STATUS_COMPLETED] BookingEventsWorkflow · "
                    f"{BOOKING} · started 2026-11-17T08:42:41")
        return ""

    async def lines_near(*a, **k):
        return "  (lines around the start)"

    monkeypatch.setattr(agent, "log_counts", counts)
    monkeypatch.setattr(agent, "temporal_workflows", workflows)
    monkeypatch.setattr(agent, "log_lines", lines_near)
    monkeypatch.setattr(temporal, "envs", lambda: {"sit": {}, "uat": {}})
    return logs


def test_the_decision_leads_the_evidence_in_the_named_env(world):
    out = asyncio.run(booking_case.turn_evidence(
        f"why did {BOOKING} go to TMS while VTS was pending in sit?"))
    assert "resolved SUCCESS able to fetch VTS dates" in out
    assert out.index("logged as DECIDED") < out.index("Lines with")


def test_with_no_env_named_it_reads_where_temporal_found_the_run(world):
    out = asyncio.run(booking_case.turn_evidence(f"why did {BOOKING} go to TMS?"))
    assert "logged as DECIDED for " + BOOKING + " in sit" in out
    assert all(env == "sit" for env, terms, _ in world if BOOKING in terms)


LIVE_REPLY = (
    "Confirmed: VTS response was PENDING, so it resolved FAILED.\n\n"
    "**Why**: ProcessSendToTmsImpl.java:55 has no VTS check.\n\n"
    "**Not confirmed**: whether a later VTS webhook callback ever arrived.\n\n"
    "Confidence: CONFIRMED (code and logs both read this turn).")


@pytest.mark.parametrize("reply", [
    LIVE_REPLY,
    LIVE_REPLY.replace("**Not confirmed**:", "**Not confirmed:**"),
    LIVE_REPLY.replace("Confidence: CONFIRMED", "**Confidence**: CONFIRMED"),
])
def test_every_way_of_writing_the_labels_is_read(reply):
    assert verdict.overclaims(reply).startswith("whether a later VTS webhook callback")


def test_a_why_turn_is_told_to_read_the_decision_and_may_go_through_everything():
    rule = booking_case.rule(f"why did {BOOKING} go to TMS while VTS was pending in sit?")
    assert "logged what it DECIDED" in rule and "A status field is not the outcome" in rule
    assert "You may read any code in the registered workspace" in rule
