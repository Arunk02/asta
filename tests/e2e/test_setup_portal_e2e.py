"""The setup workflow against a portal over real HTTP, one OS process per step.

Each driver call is a fresh Python process: the only thing it shares with the
one before is the database and the graph checkpoint. The portal counts every
POST it receives, so "requested once" is measured where it would matter.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tests.e2e.harness import ROOT, clean_env
from tests.e2e.http_portal import Portal

FULL = {"request_access": True, "setup": True}


@pytest.fixture
def portal():
    p = Portal().start()
    yield p
    p.stop()


@pytest.fixture
def drive(tmp_path, portal):
    db = tmp_path / "setup.db"

    def call(cmd, wid="w", extra=None):
        out = subprocess.run([sys.executable, "-m", "tests.e2e.setup_driver", str(db), portal.url,
                              cmd, wid, json.dumps(extra if extra is not None else {})],
                             cwd=str(ROOT), env=clean_env(), capture_output=True, text=True,
                             timeout=60)
        assert out.returncode == 0, out.stderr[-2000:]
        return json.loads(out.stdout.strip().splitlines()[-1])

    call.db = db
    return call


def requests_posted(portal):
    return [p for p in portal.posts if p.startswith("/requests/")]


def test_request_once_wait_across_processes_then_set_up_and_validate(portal, drive):
    rec = drive("start", extra=FULL)
    assert rec["outcome"] == "waiting" and rec["pending"] == ["DH-READ"]
    for _ in range(3):
        assert drive("tick")["outcome"] == "waiting"
    assert requests_posted(portal) == ["/requests/DH-READ"]
    assert portal.done == set(), "no setup before access"
    portal.approve("DH-READ")
    rec = drive("tick")
    assert rec["outcome"] == "done" and "ACTUAL_BILLING" in rec["detail"]
    assert portal.done == {"create-profile", "add-rule"}
    assert requests_posted(portal) == ["/requests/DH-READ"]


def test_a_real_timeout_after_the_request_was_created_is_reconciled_not_resent(portal, drive):
    portal.slow_submit = 2.0                       # the adapter gives up after 1s
    rec = drive("start", extra=FULL)
    assert rec["outcome"] == "waiting"
    portal.slow_submit = 0
    drive("tick")
    assert requests_posted(portal) == ["/requests/DH-READ"]
    assert portal.requests["DH-READ"]["id"] == "REQ-1"


def test_an_expired_session_waits_for_him_across_a_restart(portal, drive):
    portal.access.add("DH-READ")
    portal.session_ok = False
    rec = drive("start", extra=FULL)
    assert rec["waiting_at"] == "login"
    portal.session_ok = True                       # he signed in, in his browser
    rec = drive("answer", extra={"text": "done"})
    assert rec["outcome"] == "done"


def test_a_redirect_to_another_host_stops_before_any_post(portal, drive):
    portal.redirect_to = "https://login.example-evil.test/sso"
    rec = drive("start", extra=FULL)
    assert rec["outcome"] == "blocked" and "example-evil.test" in rec["detail"]
    assert portal.posts == []


def test_a_redesigned_portal_stops_with_what_it_saw(portal, drive):
    portal.redesigned = True
    rec = drive("start", extra=FULL)
    assert rec["outcome"] == "blocked" and "the page changed" in rec["detail"]


def test_without_authority_nothing_is_posted_until_his_yes(portal, drive):
    rec = drive("start", extra={"setup": True})
    assert rec["waiting_at"] == "request:DH-READ" and portal.posts == []
    rec = drive("answer", extra={"yes": True})
    assert rec["outcome"] == "waiting" and requests_posted(portal) == ["/requests/DH-READ"]


def test_failed_validation_is_not_reported_as_done(portal, drive):
    portal.access.add("DH-READ")
    portal.validate_ok = False
    rec = drive("start", extra=FULL)
    assert rec["outcome"] == "blocked" and "NO_MATCH" in rec["detail"]


def test_the_portal_secret_never_reaches_disk(portal, drive):
    portal.access.add("DH-READ")
    drive("start", extra=FULL)
    for path in (drive.db, drive.db.with_name("graphs.sqlite")):
        assert b"hunter2" not in path.read_bytes()
