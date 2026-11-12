"""One yes, in the words he actually uses (11 Nov).

"approve and raise PR" is one yes for plan → build → PR; "review and send" is
one yes for review → post; a posted review never asks again. His sentences from
that day, with synthetic tasks and PRs.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import go, offers, ops, pr_authority, review, store


@pytest.mark.parametrize("said, task", [
    ("567 approve raise PR", 567),
    ("task 567 approve and raise PR", 567),
    ("#567 ship", 567),
    ("approve raise PR 567", 567),                      # already worked — still does
    ("Once u done plan go ahead and implement and raise PR", None),
    ("ok go ahead and raise the PR", None),
])
def test_his_go_ahead_in_any_order_is_one_yes(said, task):
    assert go.command(said) == (True, task)


@pytest.mark.parametrize("said", [
    "dont raise PR yet, go ahead with the plan",
    "why did you raise PR?",
    "Priya wants to know if we can raise PR after approval of the design doc",
    "make sure the PR has tests",
    "go ahead",                                          # answers whatever was asked, not a PR
    "1527 merge",                                        # a bare number with merge may be a PR
])
def test_what_is_not_a_go_ahead_stays_not(said):
    assert go.command(said)[0] is False


def test_send_him_the_pr_in_the_ask_grants_the_pr_from_the_start():
    t = store.create_task("Fix P2", "code", "p", "ws")
    assert go.on_spawn(t["id"], "Now fix the P2 and send him the Pr")
    assert go.ships(t["id"])


def test_a_go_ahead_to_a_running_task_is_kept_for_when_it_finishes(monkeypatch):
    t = store.create_task("Fix P2", "code", "p", "ws")
    store.update_task(t["id"], status="running")
    out = asyncio.run(go.act("567 approve raise PR".replace("567", str(t["id"])), t["id"]))
    assert "raise the PR the moment it's done" in out and go.ships(t["id"])


# --- review: "review and send" is one yes; "review" alone tells him first -------------

@pytest.mark.parametrize("said, posts", [
    ("review PR 4321 and send", True),
    ("review 4321 and post the comments", True),
    ("review PR 4321", False),
    ("review PR 4321 but don't post anything", False),
    ("send the PR to Priya", False),                     # not a review
])
def test_review_and_send_is_a_post_grant(said, posts):
    assert pr_authority.wants_post(said) is posts


REF = "org/booking-service#4321"
REV = REF + "@abc123def456"


@pytest.fixture
def posted(monkeypatch):
    calls = []

    async def run(op):
        calls.append(op)
        return f"✅ {op['args']['action']} posted on {op['args']['pr']}"
    monkeypatch.setattr(ops, "run", run)
    return calls


def _granted(words="review PR 4321 and send"):
    t = store.create_task("Review PR 4321", "analysis", "p", None)
    rec = pr_authority.record(t["id"], REF, REV, words)
    return t, rec


def test_review_and_send_posts_the_review_as_concluded_once(posted):
    t, rec = _granted()
    assert rec and rec["post"] and not rec["approve"]
    findings = [{"path": "A.java", "line": 10, "body": "null check", "blocking": True}]
    out = asyncio.run(pr_authority.on_review(t["id"], current=REV, good=True,
                                             action="request_changes", findings=findings,
                                             body="VERDICT: Request changes"))
    assert "as you asked" in out
    assert [(c["args"]["action"], c["args"]["pr"]) for c in posted] == [("request_changes", REF)]
    again = asyncio.run(pr_authority.on_review(t["id"], current=REV, good=True,
                                               action="request_changes", findings=findings, body=""))
    assert again is None or "already posted" in again
    assert len(posted) == 1, "posted once, never twice"


def test_a_clean_review_under_review_and_send_is_an_approval(posted, monkeypatch):
    """11 Nov: "if changes are all good it has to approve as well"."""
    async def logins():
        return {"someone-else"}
    monkeypatch.setattr(review, "my_logins", logins)
    t, _ = _granted()
    out = asyncio.run(pr_authority.on_review(t["id"], current=REV, good=True, action="approve",
                                             findings=[], body="VERDICT: Approve"))
    assert [c["args"]["action"] for c in posted] == ["approve"] and "under your instruction" in out


def test_an_approve_verdict_on_red_ci_is_not_approved(posted):
    t, _ = _granted()
    out = asyncio.run(pr_authority.on_review(t["id"], current=REV, good=False, action="approve",
                                             findings=[], body="VERDICT: Approve"))
    assert not [c for c in posted if c["args"]["action"] == "approve"]


def test_review_alone_grants_nothing(posted):
    t, rec = _granted("review PR 4321")
    assert rec is None


# --- a posted review never asks again ----------------------------------------------

def _review_offer(pr, comments):
    return offers.staged_write("pr_review_inline", {"pr": pr, "action": "request_changes",
                                                    "body": comments, "comments": []},
                               f"🔎 Request changes on PR #{pr[-4:]}", comments, "Post?",
                               kind="pr_write")


def test_posting_a_review_drops_every_other_copy_of_it(monkeypatch):
    other = "https://example.test/org/booking-service/pull/4300"
    mine = "https://example.test/org/booking-service/pull/4321"
    _review_offer(other, "Approve.")
    _review_offer(mine, "one comment")
    _review_offer(mine, "five comments")

    async def post(*a, **k):
        return "request changes posted"
    monkeypatch.setattr(review, "post_inline_review", post)
    asyncio.run(ops._pr_review_inline(pr=mine, action="request_changes", body="x", comments=[]))
    assert offers.review_for_pr("4321") is None
    assert offers.review_for_pr("4300") is not None, "another PR's review is untouched"


# --- an approval that keeps it local (12 Nov: it re-planned and asked again) ---------

@pytest.mark.parametrize("said, task", [
    ("approve task 567, don't push or raise PR", 567),
    ("approve task 567 but don't raise PR", 567),
    ("567 approve, no PR", 567),
    ("approve 567 no pr yet", 567),
])
def test_approve_without_the_pr_is_still_one_yes(said, task):
    assert go.command(said) == (True, task)
    assert not go.wants_pr(said)


@pytest.mark.parametrize("said", [
    "don't approve 567",
    "approve task 567 but change the test name",          # feedback, not a bare approval
    "approve, don't merge",
])
def test_these_are_not_a_local_approval(said):
    assert go.approves_locally(said)[0] is False


def test_a_local_approval_approves_the_plan_and_ships_nothing(monkeypatch):
    from app import tasks
    t = store.create_task("Add a unit test", "code", "p", "ws")
    store.update_task(t["id"], status="awaiting_approval")
    approved = []

    async def approve(task_id):
        approved.append(task_id)
    monkeypatch.setattr(tasks, "approve", approve)
    out = asyncio.run(go.act(f"approve task {t['id']}, don't push or raise PR", t["id"]))
    assert approved == [t["id"]] and "going ahead now" in out
    assert not go.ships(t["id"])


def test_no_pr_said_now_outranks_a_pr_granted_earlier():
    t = store.create_task("Add a unit test", "code", "p", "ws")
    go.grant(t["id"], "approve and raise PR", ship=True)
    go.grant(t["id"], "approve, don't raise PR", ship=False)
    assert not go.ships(t["id"])
