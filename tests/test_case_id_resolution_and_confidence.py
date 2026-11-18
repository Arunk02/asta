"""Any id is traced under its bookingNumber; CONFIRMED never hides its own gap (18 Nov).

Live re-test of the VTS case: the question named the 12-character
servicePlanNumber, evidence was fetched under it, and booking-service's deciding
line — written under the 11-character bookingNumber — was never read. The answer
named the wrong mechanism, said "Not confirmed: I didn't pull booking-service's
status line", and still ended "Confidence: CONFIRMED". Synthetic ids only.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app import agent, booking_case, debugkit, temporal, verdict

PLAN = "MXAAAA1111BB"        # 12: servicePlanNumber (upstream)
BOOKING = "JXCCCC2222D"      # 11: bookingNumber (ours)


@pytest.mark.parametrize("identifier, booking", [
    (BOOKING, True), (PLAN, False), ("TO8384577520093", False), ("12345678901", False)])
def test_which_ids_are_booking_numbers(identifier, booking):
    assert booking_case.is_booking_number(identifier) is booking


def _row(message, logger="VesselTrackingService"):
    ts = dt.datetime(2026, 11, 17, 8, 42, 41, tzinfo=dt.timezone.utc).timestamp()
    return {"ts": ts, "pod": "vts-abc-1", "app": "vessel-tracking-service", "logger": logger,
            "message": message}


@pytest.fixture
def logs(monkeypatch):
    asked = []

    async def lines(env, service, terms, logger, start, end, limit, *a, **k):
        asked.append((env, tuple(terms)))
        if PLAN in terms and env == "sit":
            return {"rows": [_row(f"Lookup for servicePlan {PLAN} completed for "
                                  f"order_id={BOOKING} with status=PENDING")]}
        return {"rows": []}

    monkeypatch.setattr(debugkit, "lines", lines)
    monkeypatch.setattr(temporal, "envs", lambda: {"sit": {}, "uat": {}})
    return asked


def test_a_service_plan_number_resolves_to_its_booking(logs):
    booking, where = asyncio.run(booking_case.booking_for(PLAN, "sit"))
    assert booking == BOOKING and where.startswith("sit")


def test_a_booking_number_needs_no_lookup(logs):
    assert asyncio.run(booking_case.booking_for(BOOKING, "sit")) == (BOOKING, "")
    assert logs == []


def test_no_environment_named_searches_each_one(logs):
    booking, _ = asyncio.run(booking_case.booking_for(PLAN, ""))
    assert booking == BOOKING and {e for e, _ in logs} == {"sit", "uat"}


def test_evidence_is_fetched_under_the_booking_number(logs, monkeypatch):
    queried = []

    async def counts(term, env="all", hours=72, **k):
        queried.append(term)
        return f"Lines with “{term}” by app: sit booking-service 40"

    async def workflows(env="", query="", limit=8, **k):
        queried.append(query)
        return ""

    monkeypatch.setattr(agent, "log_counts", counts)
    monkeypatch.setattr(agent, "temporal_workflows", workflows)
    out = asyncio.run(booking_case.turn_evidence(
        f"why did {PLAN} go to TMS while VTS was pending in sit?"))
    assert f"its booking is {BOOKING}" in out and f"under {BOOKING}" in out
    assert BOOKING in queried and PLAN not in queried
    assert any(BOOKING in q for q in queried if "WorkflowId" in q)


VTS_LIVE = (
    "**Answer: by design — VTS and SEND_TO_TMS are independent.**\n\n"
    "**Why:** VesselTrackingRegistrationActivityImpl.java:140 arms a hold.\n\n"
    "**Not confirmed:** I didn't pull the exact status line from booking-service for this "
    "booking.\n\n**Next step:** none needed.\n\nConfidence: CONFIRMED (for the mechanism).")


def test_confirmed_with_its_own_gap_is_flagged():
    gap = verdict.overclaims(VTS_LIVE)
    assert gap.startswith("I didn't pull the exact status line from booking-service")


@pytest.mark.parametrize("answer", [
    "Why: X.java:12\nTimeline: 10:11:12 …\nNot confirmed: nothing.\nConfidence: CONFIRMED",
    "Why: X.java:12\nNot confirmed: the stored snapshot.\nConfidence: NOT CONFIRMED",
    "A plain answer with no verdict at all.",
])
def test_an_honest_answer_is_left_alone(answer):
    assert verdict.overclaims(answer) == ""


class _Sink:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


def test_the_flag_reaches_him_in_the_same_breath():
    import time
    from app import main, store
    conv = store.create_conversation(model="claude_cli", workspace=None)
    sink = _Sink()
    asyncio.run(main._correct_claims(sink, conv, VTS_LIVE, time.time()))
    notes = [p["text"] for p in sink.sent if p.get("type") == "note"]
    assert any(n.startswith("⚠️ Confidence check") and "booking-service" in n for n in notes)
    sink2 = _Sink()
    asyncio.run(main._correct_claims(sink2, conv, "Why: X.java:3\nNot confirmed: nothing.\n"
                                                  "Confidence: CONFIRMED", time.time()))
    assert not any("Confidence check" in str(p) for p in sink2.sent)
