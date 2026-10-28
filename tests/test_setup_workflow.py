"""Read a guide, get the missing access once, set up, and prove it — against a
fake portal that redirects, expires sessions, changes pages, loses receipts and
approves later. Every outward act is counted on the portal's own side."""
from __future__ import annotations

import asyncio
import json

import pytest

from app import setup_flow, store
from app.graph import engine, setup
from tests.fake_portal import Backend, FakePortal

FULL = {"request_access": True, "setup": True}


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("ASTA_GRAPHS", "1")


@pytest.fixture
def portal(monkeypatch):
    backend = Backend()
    monkeypatch.setitem(setup.PORTALS, "fake", FakePortal(backend))
    return backend


def restart(monkeypatch, backend):
    """A new process: a fresh adapter over the same portal, nothing in memory."""
    monkeypatch.setitem(setup.PORTALS, "fake", FakePortal(backend))


def go(wid, authorized=FULL):
    return asyncio.run(setup_flow.start(wid, "fake", "https://docs.example.test/dh", "DH onboarding",
                                        authorized))


def test_missing_access_is_requested_once_then_setup_runs_after_approval_and_a_restart(portal, monkeypatch):
    rec = go("w1")
    assert rec["outcome"] == "waiting" and rec["pending"] == ["DH-READ"]
    assert portal.submitted == ["DH-READ"]
    asyncio.run(setup_flow.tick("w1"))
    restart(monkeypatch, portal)
    asyncio.run(setup_flow.tick("w1"))
    assert portal.submitted == ["DH-READ"], "one request, however often it looks"
    assert portal.done_calls == [], "no setup before access"
    portal.approve("DH-READ")
    restart(monkeypatch, portal)
    lines = asyncio.run(setup_flow.tick_all())
    assert setup_flow.status("w1")["outcome"] == "done"
    assert portal.done_calls == ["create-profile", "add-rule"]
    assert lines and "Set up and verified" in lines[0] and "ACTUAL_BILLING" in lines[0]
    assert asyncio.run(setup_flow.tick_all()) == []


def test_access_already_held_goes_straight_to_setup(portal):
    portal.access.add("DH-READ")
    assert go("w2")["outcome"] == "done" and portal.submitted == []


def test_a_lost_receipt_for_a_request_that_was_made_is_reconciled_not_resent(portal):
    portal.lost_receipt = "made"
    rec = go("w3")
    assert rec["outcome"] == "waiting" and portal.submitted == ["DH-READ"]
    receipt = json.loads(store.kv_get("setup_receipt:w3:request:DH-READ"))
    assert receipt["state"] == "sent" and receipt["id"] == "REQ-1" and receipt["reconciled"]


def test_a_lost_receipt_with_no_request_on_the_portal_stops_and_never_resubmits(portal):
    portal.lost_receipt = "not_made"
    rec = go("w4")
    assert rec["outcome"] == "blocked" and "will not be submitted again" in rec["detail"]
    portal.lost_receipt = ""
    asyncio.run(setup_flow.tick("w4"))
    assert portal.submitted == [] and portal.requests == {}


def test_a_request_someone_already_made_is_adopted_not_duplicated(portal):
    portal.requests["DH-READ"] = {"id": "REQ-9", "state": "pending"}
    rec = go("w5")
    assert rec["outcome"] == "waiting" and portal.submitted == []
    assert json.loads(store.kv_get("setup_receipt:w5:request:DH-READ"))["id"] == "REQ-9"


def test_a_rejected_request_blocks_with_the_reason(portal):
    go("w6")
    portal.reject("DH-READ")
    assert asyncio.run(setup_flow.tick("w6"))["outcome"] == "blocked"
    assert "rejected (REQ-1)" in setup_flow.status("w6")["detail"]


def test_without_authority_the_request_waits_for_his_yes(portal):
    rec = go("w7", authorized={"setup": True})
    assert rec["waiting_at"] == "request:DH-READ" and portal.submitted == []
    assert "request access to DH-READ" in setup_flow.report("w7", rec)
    rec = asyncio.run(setup_flow.answer("w7", {"yes": True}))
    assert rec["outcome"] == "waiting" and portal.submitted == ["DH-READ"]


def test_his_no_declines_and_requests_nothing(portal):
    go("w8", authorized={"setup": True})
    rec = asyncio.run(setup_flow.answer("w8", {"yes": False}))
    assert rec["outcome"] == "declined" and portal.submitted == []


def test_an_answer_recorded_before_a_restart_is_applied_after_it(portal, monkeypatch):
    go("w9", authorized={"setup": True})
    engine.record_answer(setup_flow.KIND, "w9", {"yes": True})
    restart(monkeypatch, portal)
    asyncio.run(setup_flow.resume("w9"))
    assert portal.submitted == ["DH-READ"]


def test_an_expired_sign_in_parks_for_him_and_asta_never_signs_in(portal):
    portal.access.add("DH-READ")
    portal.login_expired = True
    rec = go("w10")
    assert rec["waiting_at"] == "login" and "Sign in, then say done" in setup_flow.report("w10", rec)
    portal.sign_in()                                       # he does it, in the browser
    rec = asyncio.run(setup_flow.answer("w10", {"text": "done"}))
    assert rec["outcome"] == "done" and portal.signins == 1


def test_a_changed_page_stops_with_what_it_saw(portal):
    portal.page_changed = True
    rec = go("w11")
    assert rec["outcome"] == "blocked" and "the page changed" in rec["detail"]
    assert portal.submitted == []


def test_a_redirect_off_the_site_stops_before_anything(portal):
    portal.redirect_off_site = True
    rec = go("w12")
    assert rec["outcome"] == "blocked" and "redirected off the allowed site" in rec["detail"]


def test_a_step_that_did_not_take_effect_is_not_called_done(portal):
    portal.access.add("DH-READ")
    portal.no_effect.add("add-rule")
    rec = go("w13")
    assert rec["outcome"] == "blocked" and "add-rule ran but did not take effect" in rec["detail"]


def test_a_submitting_step_left_uncertain_is_never_repeated(portal, monkeypatch):
    portal.access.add("DH-READ")
    portal.no_effect.add("create-profile")
    go("w14")
    portal.no_effect.clear()
    # Run it again from the top as a new workflow thread over the same receipts.
    asyncio.run(engine.drive(setup_flow.KIND, "w14", setup.build, {"outcome": "", "detail": ""}))
    assert portal.done_calls == ["create-profile"], "a submit is not repeated blind"


def test_failed_validation_is_reported_as_not_set_up(portal):
    portal.access.add("DH-READ")
    portal.validate_ok = False
    rec = go("w15")
    assert rec["outcome"] == "blocked" and "NO_MATCH" in rec["detail"]
    assert "not set up" in setup_flow.report("w15", rec)


def test_setup_needs_its_own_authorization(portal):
    portal.access.add("DH-READ")
    rec = go("w16", authorized={"request_access": True})
    assert rec["outcome"] == "blocked" and portal.done_calls == []


def test_no_secret_reaches_the_evidence(portal):
    portal.login_expired = True
    go("w17")
    portal.sign_in()
    asyncio.run(setup_flow.answer("w17", {"text": "done"}))
    raw = json.dumps(store.kv_like("%"))
    assert "hunter2" not in raw and "abc123" not in raw
    from app.graph.runner import db_path
    checkpoints = db_path().read_bytes()
    assert b"sign-in expired: session=[removed]" in checkpoints, "recorded, with the secret removed"
    assert b"abc123" not in checkpoints and b"hunter2" not in checkpoints


@pytest.mark.parametrize("said, gate, submitted", [
    ("yes request it", "request", ["DH-READ"]),
    ("don't request it", "request", []),
])
def test_his_one_line_answer_reaches_the_parked_setup(portal, said, gate, submitted):
    from app import frontdesk
    go("w20", authorized={"setup": True})
    assert frontdesk.answer_from_state(said)
    assert portal.submitted == [], "recorded, not driven, in the front desk"
    asyncio.run(setup_flow.tick_all())
    assert portal.submitted == submitted


def test_signed_in_resumes_only_a_setup_waiting_for_a_sign_in(portal):
    from app import frontdesk
    portal.access.add("DH-READ")
    portal.login_expired = True
    go("w21")
    portal.sign_in()
    assert frontdesk.answer_from_state("I've signed in")
    asyncio.run(setup_flow.tick_all())
    assert setup_flow.status("w21")["outcome"] == "done"
    assert frontdesk.answer_from_state("signed in") == "", "nothing is waiting any more"
