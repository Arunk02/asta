"""The endpoints a CLI brain curls, and what their answers promise.

A brain reaching Asta over HTTP has nothing but the JSON to go on. So a response
that says one thing in a flag and another in a message is not a cosmetic problem:
the brain resolves the contradiction by believing the flag, and tells Arun a
question is waiting for him that does not exist.

These also pin the parity that the capability table promises — the curl path and
the in-process tool must reach the SAME staging, or the rule only holds for
whichever brain happened to answer.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import jira, main, offers

H = {"Authorization": "Bearer qa-token"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ASTA_TOKEN", "qa-token")
    monkeypatch.setenv("JIRA_BASE_URL", "https://co.atlassian.net")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    offers.clear()
    return TestClient(main.app)


def test_a_write_endpoint_needs_the_token(client):
    assert client.post("/api/jira/issue/P-1/comment", json={"text": "x"}).status_code == 401


def test_the_curl_path_stages_exactly_like_the_tool(client, monkeypatch):
    """Parity. A CLI brain must not have a write path the in-process brain lacks."""
    monkeypatch.setattr(jira, "add_comment",
                        lambda *a: pytest.fail("the endpoint must not post"))
    r = client.post("/api/jira/issue/PROJ-412/comment",
                    json={"text": "Blocked on schema."}, headers=H)
    assert r.status_code == 200 and r.json()["staged"] is True
    assert offers.pending().op["args"]["text"] == "Blocked on schema."


def test_offered_reports_what_happened_not_that_it_was_asked(client):
    """The bug: a blank step came back as offered=true alongside a message saying
    it had been rejected."""
    r = client.post("/api/propose-next", json={"next_step": "   "}, headers=H)
    assert r.status_code == 400
    assert offers.pending() is None


def test_a_real_step_is_offered_and_says_so(client):
    r = client.post("/api/propose-next",
                    json={"next_step": "Implement PROJ-412 on a branch.", "why": "AC is clear"},
                    headers=H)
    assert r.json()["offered"] is True
    assert "Implement PROJ-412" in offers.pending().action


def test_a_rejected_review_verb_stages_nothing(client):
    r = client.post("/api/pr-review", json={"pr": "31", "action": "merge"}, headers=H)
    assert "must be one of" in r.json()["message"]
    assert offers.pending() is None


def test_a_missing_required_field_is_a_400_not_a_staged_guess(client):
    assert client.post("/api/pr-review", json={"pr": "31"}, headers=H).status_code == 400
    assert client.post("/api/leave", json={}, headers=H).status_code == 400
    assert client.post("/api/meetings", json={"subject": "x"}, headers=H).status_code == 400
    assert offers.pending() is None


def test_an_unparseable_time_answers_rather_than_500s(client):
    """The model gave a date nobody can be sure of. That is a conversation, not a
    server error — a 500 tells it nothing it can act on."""
    r = client.post("/api/meetings", json={"subject": "x", "when": "thursday"}, headers=H)
    assert r.status_code == 200 and "Can't build" in r.json()["message"]
    assert offers.pending() is None


# --- staged means staged ------------------------------------------------------
#
# Found by probing the running server, not by the unit tests: every staging
# endpoint hardcoded staged:true and put the real outcome in the message. So a
# refusal came back flagged as staged, and a brain that believes the flag tells
# Arun something is waiting for his yes when nothing is.

def test_a_refused_invite_is_not_reported_as_staged(client):
    r = client.post("/api/meetings", json={"subject": "x", "when": "thursday"}, headers=H)
    assert r.json()["staged"] is False
    assert "Can't build" in r.json()["message"]
    assert offers.pending() is None


def test_a_refused_leave_request_is_not_reported_as_staged(client):
    r = client.post("/api/leave", json={"start_date": "2026-02-30"}, headers=H)
    assert r.json()["staged"] is False
    assert offers.pending() is None


def test_a_refused_review_is_not_reported_as_staged(client):
    r = client.post("/api/pr-review", json={"pr": "31", "action": "merge"}, headers=H)
    assert r.json()["staged"] is False


def test_an_unconfigured_jira_comment_is_not_reported_as_staged(client, monkeypatch):
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    r = client.post("/api/jira/issue/P-1/comment", json={"text": "x"}, headers=H)
    assert r.json()["staged"] is False
    assert "not configured" in r.json()["message"]


def test_a_real_invite_is_reported_as_staged(client):
    r = client.post("/api/meetings",
                    json={"subject": "Design sync", "when": "2026-07-30 15:00"}, headers=H)
    assert r.json()["staged"] is True
    assert offers.pending() is not None


def test_a_refusal_does_not_claim_an_unrelated_older_offer(client):
    """The subtle one: an offer was already open from something else. A refusal
    must not look at 'is anything pending?' and answer yes."""
    offers.propose("older", "ctx", "Do that?", "do that")
    r = client.post("/api/meetings", json={"subject": "x", "when": "thursday"}, headers=H)
    assert r.json()["staged"] is False
    assert offers.pending().subject == "older"        # and it is left alone
