"""15 Oct: a booking question got only "where it is logged" — and Loki keeps 24 h,
so a booking that ran yesterday read "none" and the brain spent three more
log_lines calls (31.5 s) finding when. Temporal knows when and how each workflow
ended; the lines at that moment follow. All fetched in code, inside the cap.
"""

import asyncio
import time

import pytest

from app import booking_case

COUNTS = ("Lines with “H7Y5BWNBNX9” by app, window ending 2026-10-15 04:38 UTC:\n"
          "- prod (telikos-prod, last 24h00m): telikos-billing-service 41, telikos-booking-service 2\n"
          "- qa (telikos-qa, last 24h00m): none\n"
          "- sit (telikos-sit, last 24h00m): telikos-booking-service 3")
STATE = ("2 workflow(s) in {env} matching WorkflowId STARTS_WITH \"H7Y5BWNBNX9\":\n"
         "  [WORKFLOW_EXECUTION_STATUS_COMPLETED] BillingCostWorkflow · H7Y5BWNBNX9_COST · started 2026-10-13T23:00:29\n"
         "  [WORKFLOW_EXECUTION_STATUS_RUNNING] ActivityPlanWorkflow · H7Y5BWNBNX9 · started 2026-10-13T07:33:47")


@pytest.fixture
def world(monkeypatch):
    from app import agent
    calls = {"temporal": [], "lines": []}

    async def log_counts(term="", env="", **k):
        return COUNTS

    async def temporal_workflows(env="", query="", limit=10):
        calls["temporal"].append((env, query))
        if env == "dev":
            return "Temporal — no certificate for dev"
        if env == "qa":
            return 'No workflows in qa matching WorkflowId STARTS_WITH "H7Y5BWNBNX9".'
        return STATE.format(env=env)

    async def log_lines(env, contains=(), at="", **k):
        calls["lines"].append((env, tuple(contains), at))
        return "  2026-10-13 23:00:29 UTC telikos-billing-service :: Successfully publishBillingEvent"

    monkeypatch.setattr(agent, "log_counts", log_counts)
    monkeypatch.setattr(agent, "temporal_workflows", temporal_workflows)
    monkeypatch.setattr(agent, "log_lines", log_lines)
    from app import temporal
    monkeypatch.setattr(temporal, "envs", lambda: {"dev": {}, "qa": {}, "prod": {}})
    return calls


def test_the_named_env_gets_its_workflows_and_the_lines_when_it_last_ran(world):
    out = asyncio.run(booking_case.turn_evidence("why is booking H7Y5BWNBNX9 not reaching billing in prod"))
    assert world["temporal"] == [("prod", 'WorkflowId STARTS_WITH "H7Y5BWNBNX9"')]
    assert world["lines"] == [("prod", ("H7Y5BWNBNX9",), "2026-10-13T23:00:29")]
    assert "STATUS_RUNNING] ActivityPlanWorkflow" in out and "publishBillingEvent" in out


def test_no_env_named_searches_every_env_and_keeps_only_where_it_is(world):
    out = asyncio.run(booking_case.turn_evidence("check booking H7Y5BWNBNX9 status"))
    assert sorted(e for e, _ in world["temporal"]) == ["dev", "prod", "qa"]
    assert "workflow(s) in prod" in out
    assert "no certificate" not in out and "No workflows in qa" not in out


def test_older_than_lokis_day_is_still_found_in_temporal(world, monkeypatch):
    from app import agent

    async def none(*a, **k):
        return "- prod (telikos-prod, last 24h00m): none\n- qa (telikos-qa, last 24h00m): none"

    monkeypatch.setattr(agent, "log_counts", none)
    out = asyncio.run(booking_case.turn_evidence("check booking H7Y5BWNBNX9 status"))
    assert "BillingCostWorkflow" in out and world["lines"][0][0] == "prod"


def test_counts_and_state_run_side_by_side(world, monkeypatch):
    from app import agent

    async def slow_counts(*a, **k):
        await asyncio.sleep(0.4)
        return "- prod: telikos-billing-service 41"

    async def slow_state(env="", query="", limit=10):
        await asyncio.sleep(0.4)
        return STATE.format(env=env)

    monkeypatch.setattr(agent, "log_counts", slow_counts)
    monkeypatch.setattr(agent, "temporal_workflows", slow_state)
    t = time.monotonic()
    asyncio.run(booking_case.turn_evidence("why booking H7Y5BWNBNX9 stuck in prod"))
    assert time.monotonic() - t < 0.75


def test_slow_logs_keep_the_state(world, monkeypatch):
    from app import agent

    async def slow(*a, **k):
        await asyncio.sleep(5)

    monkeypatch.setattr(agent, "log_counts", slow)
    monkeypatch.setattr(booking_case, "TURN_EVIDENCE_SECONDS", 2.0)
    out = asyncio.run(booking_case.turn_evidence("why booking H7Y5BWNBNX9 stuck in prod"))
    assert "BillingCostWorkflow" in out


def test_slow_temporal_keeps_the_counts(world, monkeypatch):
    from app import agent

    async def slow(*a, **k):
        await asyncio.sleep(5)

    monkeypatch.setattr(agent, "temporal_workflows", slow)
    monkeypatch.setattr(booking_case, "TURN_EVIDENCE_SECONDS", 2.0)
    t = time.monotonic()
    out = asyncio.run(booking_case.turn_evidence("why booking H7Y5BWNBNX9 stuck in prod"))
    assert time.monotonic() - t < 2.5
    assert "telikos-billing-service 41" in out and world["lines"] == []


def test_a_failing_log_read_keeps_the_state(world, monkeypatch):
    from app import agent

    async def boom(*a, **k):
        raise RuntimeError("loki down")

    monkeypatch.setattr(agent, "log_lines", boom)
    out = asyncio.run(booking_case.turn_evidence("why booking H7Y5BWNBNX9 stuck in prod"))
    assert "BillingCostWorkflow" in out


def test_parsers():
    assert booking_case._newest_start(STATE.format(env="qa")) == ("qa", "2026-10-13T23:00:29")
    assert booking_case._newest_start("Temporal — no certificate") is None
