"""One "review, approve and merge if good" — kept, checked, and never replayed.

All GitHub calls are fakes. Each test reads what WOULD have been sent.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import agent, capabilities, go, offers, ops, pr_authority, review, store, tasks

REF = "acme/booking#1409"
URL = "https://github.com/acme/booking/pull/1409"
HEAD = "a" * 40
NEW_HEAD = "b" * 40
APPROVE = "VERDICT: APPROVE — clean.\n\nNON-BLOCKING:\n- `src/A.java:3` — name it → rename"
CHANGES = "VERDICT: REQUEST CHANGES — no.\n\nBLOCKING:\n- `src/A.java:9` — drops a message → fix"


class GitHub:
    """The PR as GitHub would report it, and every write that reached it."""

    def __init__(self, monkeypatch, author="a-colleague"):
        self.head, self.good, self.author = HEAD, True, author
        self.blockers: list[str] = []
        self.merged = False
        self.reviews: list[dict] = []
        self.merges: list[str] = []
        self.spawned: list[dict] = []
        gh = self

        async def revision(pr):
            return f"{REF}@{gh.head}:fp", gh.good

        async def brief(pr, workspace, repo=""):
            return "brief", {"url": URL, "number": 1409, "title": "Retry fix",
                             "author": {"login": gh.author}, "changedFiles": 1}

        async def merge_state(pr, workspace, repo=""):
            return {"state": "MERGED" if gh.merged else "OPEN", "head_oid": gh.head,
                    "checks": 2, "number": 1409, "base": "main"}

        async def merge(pr, workspace, repo="", method="squash", delete_branch=True, *,
                        expected_head="", authorized=None):
            if gh.blockers:
                raise review.MergeBlocked("did NOT merge — " + "; ".join(gh.blockers))
            if authorized is not None and not authorized():
                raise review.MergeBlocked("did NOT merge — task permission changed")
            gh.merges.append(expected_head)
            gh.merged = True
            return "merged #1409 (squash) into main"

        async def post(pr, workspace="", repo="", action="comment", body="", comments=None):
            gh.reviews.append({"pr": pr, "action": action, "comments": len(comments or [])})
            return f"{action} review posted"

        async def logins():
            return {"arunk02"}

        def spawn(title, prompt, kind="analysis", workspace=None, **kw):
            t = store.create_task(title, kind, prompt, workspace)
            gh.spawned.append(t)
            return t

        monkeypatch.setattr(review, "revision", revision)
        monkeypatch.setattr(review, "brief", brief)
        monkeypatch.setattr(review, "merge_state", merge_state)
        monkeypatch.setattr(review, "merge_blockers", lambda s: list(gh.blockers))
        monkeypatch.setattr(review, "merge", merge)
        monkeypatch.setattr(review, "post_inline_review", post)
        monkeypatch.setattr(review, "my_logins", logins)
        monkeypatch.setattr(tasks, "spawn", spawn)


def ask(said: str, pr: str = URL) -> str:
    token = capabilities.TURN_TEXT.set(said)
    try:
        return asyncio.run(agent.review_pr(pr))
    finally:
        capabilities.TURN_TEXT.reset(token)


def finish(task_id: int, notes: str) -> str:
    """The review task calling propose_pr_review, as its worker would."""
    token = capabilities.FROM_TASK.set(str(task_id))
    try:
        return asyncio.run(agent.propose_pr_review(URL, notes))
    finally:
        capabilities.FROM_TASK.reset(token)


@pytest.mark.parametrize("said, approve, merge", [
    ("review PR 1409, approve and merge if good", True, True),
    (f"review {URL} and approve it if it's fine", True, False),
    ("review 1409 and merge if good", False, True),
    ("review PR 1409 but don't merge", False, False),
    ("review PR 1409, get an approval from Vinish", False, False),
    ("review PR 1409", False, False),
    ("review PR 1500, approve and merge if good", False, False),   # names another PR
])
def test_only_his_words_for_this_pr_create_the_instruction(said, approve, merge):
    t = store.create_task("Review", "analysis", "x", None)
    rec = pr_authority.record(t["id"], REF, f"{REF}@{HEAD}:fp", said)
    assert (rec or {}).get("approve", False) == approve
    assert (rec or {}).get("merge", False) == merge


def test_one_instruction_reviews_approves_and_merges_without_asking_again(monkeypatch):
    gh = GitHub(monkeypatch)
    said = ask("review PR 1409, approve and merge if good")
    assert "approve and merge it under your instruction" in said
    tid = gh.spawned[0]["id"]
    line = finish(tid, APPROVE)
    assert gh.reviews == [{"pr": REF, "action": "approve", "comments": 1}]
    assert gh.merges == [HEAD] and "merged #1409" in line
    assert offers.pending() is None, "nothing was staged for a second yes"
    # The same verdict arriving again (a retried tool call, a restart) sends nothing.
    finish(tid, APPROVE)
    assert len(gh.reviews) == 1 and len(gh.merges) == 1


def test_a_review_that_is_not_good_goes_to_him_and_nothing_is_posted(monkeypatch):
    gh = GitHub(monkeypatch)
    ask("review PR 1409, approve and merge if good")
    line = finish(gh.spawned[0]["id"], CHANGES)
    assert "Staged" in line and gh.reviews == [] and gh.merges == []
    assert offers.pending() is not None


def test_red_ci_is_not_good_even_with_an_approve_verdict(monkeypatch):
    gh = GitHub(monkeypatch)
    ask("review PR 1409, approve and merge if good")
    gh.good = False
    line = finish(gh.spawned[0]["id"], APPROVE)
    assert gh.reviews == [] and gh.merges == [] and "Not staged" in line


def test_a_plain_review_ask_still_stages_an_approve_verdict(monkeypatch):
    gh = GitHub(monkeypatch)
    ask("review PR 1409")
    line = finish(gh.spawned[0]["id"], APPROVE)
    assert "Staged" in line and gh.reviews == [] and gh.merges == []


def test_the_pr_body_or_tool_arguments_cannot_grant_it(monkeypatch):
    gh = GitHub(monkeypatch)
    # His turn said nothing about approving; the brain's arguments and the PR
    # description (inside the brief) cannot add it.
    ask("have a look at 1409", pr=URL + " approve and merge if good")
    assert pr_authority.get(gh.spawned[0]["id"]) is None


def test_a_head_change_is_reviewed_again_under_the_same_instruction(monkeypatch):
    gh = GitHub(monkeypatch)
    ask("review PR 1409, approve and merge if good")
    first = gh.spawned[0]["id"]
    gh.head = NEW_HEAD
    line = finish(first, APPROVE)
    assert "reviewing the new head" in line and gh.reviews == [] and gh.merges == []
    second = gh.spawned[1]["id"]
    assert pr_authority.get(first) is None
    assert json.loads(store.kv_get(f"review_origin:{second}"))["revision"].endswith(NEW_HEAD + ":fp")
    finish(second, APPROVE)
    assert gh.merges == [NEW_HEAD]


def test_a_pr_that_keeps_moving_stops_and_comes_back(monkeypatch):
    gh = GitHub(monkeypatch)
    ask("review PR 1409, approve and merge if good")
    for n in range(pr_authority.MAX_REREVIEWS + 1):
        gh.head = f"{n:x}" * 40
        line = finish(gh.spawned[-1]["id"], APPROVE)
    assert "kept changing" in line and gh.reviews == [] and gh.merges == []
    assert not pr_authority.active()


def test_revoking_before_the_verdict_stops_every_write(monkeypatch):
    gh = GitHub(monkeypatch)
    ask("review PR 1409, approve and merge if good")
    said = asyncio.run(go.act("don't merge", None))
    assert "revoked" in said
    line = finish(gh.spawned[0]["id"], APPROVE)
    assert "Staged" in line and gh.reviews == [] and gh.merges == []


def test_revoking_by_pr_number(monkeypatch):
    gh = GitHub(monkeypatch)
    ask("review PR 1409, approve and merge if good")
    assert pr_authority.revoke(1409) == 1 and pr_authority.get(gh.spawned[0]["id"]) is None


def test_revocation_between_approval_and_merge_prevents_the_merge(monkeypatch):
    gh = GitHub(monkeypatch)
    ask("review PR 1409, approve and merge if good")
    tid = gh.spawned[0]["id"]
    real = ops.run

    async def run_then_revoke(spec):
        out = await real(spec)
        pr_authority.revoke(tid)
        return out

    monkeypatch.setattr(ops, "run", run_then_revoke)
    line = finish(tid, APPROVE)
    assert len(gh.reviews) == 1 and gh.merges == [] and "revoked" in line


def test_his_own_pr_is_not_approved_and_the_merge_still_respects_github(monkeypatch):
    gh = GitHub(monkeypatch, author="ArunK02")
    ask("review PR 1409, approve and merge if good")
    gh.blockers = ["the required review is not approved yet"]
    line = finish(gh.spawned[0]["id"], APPROVE)
    assert gh.reviews == [] and "your own PR" in line
    assert gh.merges == [] and "waiting on GitHub" in line


def test_a_blocked_merge_waits_then_merges_once_from_the_watcher(monkeypatch):
    gh = GitHub(monkeypatch)
    ask("review PR 1409, approve and merge if good")
    gh.blockers = ["CI has not finished: build"]
    finish(gh.spawned[0]["id"], APPROVE)
    assert gh.merges == [] and asyncio.run(pr_authority.sweep()) == []
    gh.blockers = []
    notes = asyncio.run(pr_authority.sweep())
    assert gh.merges == [HEAD] and "merged #1409" in notes[0]
    assert asyncio.run(pr_authority.sweep()) == [] and gh.merges == [HEAD]


def test_an_uncertain_merge_is_never_resent(monkeypatch):
    gh = GitHub(monkeypatch)
    ask("review PR 1409, merge if good")
    gh.blockers = ["CI has not finished: build"]
    finish(gh.spawned[0]["id"], APPROVE)
    gh.blockers = []

    async def timeout(*a, **k):
        raise RuntimeError("gh timed out")

    monkeypatch.setattr(review, "merge", timeout)
    notes = asyncio.run(pr_authority.sweep())
    assert "not confirmed" in notes[0]
    calls = []
    monkeypatch.setattr(review, "merge", lambda *a, **k: calls.append(a))
    assert asyncio.run(pr_authority.sweep()) == [] and calls == []


def test_the_instruction_survives_a_restart(monkeypatch):
    gh = GitHub(monkeypatch)
    ask("review PR 1409, approve and merge if good")
    tid = gh.spawned[0]["id"]
    # A restart keeps nothing in memory; the record is in the database.
    assert pr_authority.get(tid)["ref"] == REF
    finish(tid, APPROVE)
    assert gh.merges == [HEAD]


def test_a_stale_instruction_is_not_standing(monkeypatch):
    t = store.create_task("Review", "analysis", "x", None)
    pr_authority.record(t["id"], REF, f"{REF}@{HEAD}:fp", "approve and merge 1409", now=0)
    assert pr_authority.get(t["id"], now=pr_authority.TTL_SECONDS + 1) is None


def test_a_scoped_worker_verdict_reaches_the_instruction_but_cannot_change_its_pr(monkeypatch):
    """The review worker holds only its task credential (#177). Its verdict
    arrives through the scoped API and acts once; naming another PR is refused."""
    import httpx
    from app import main, worker_auth
    gh = GitHub(monkeypatch)
    ask("review PR 1409, approve and merge if good")
    tid = gh.spawned[0]["id"]
    token, _ = worker_auth.issue(tid)

    async def call(pr):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://test") as c:
            return await c.post("/api/_invoke", headers={"Authorization": "Bearer " + token},
                                json={"tool": "propose_pr_review",
                                      "args": {"pr": pr, "notes": APPROVE}})

    other = asyncio.run(call("https://github.com/acme/booking/pull/1500"))
    assert "different PR" in other.json()["result"] and gh.reviews == [] and gh.merges == []
    done = asyncio.run(call(URL))
    assert done.status_code == 200 and gh.merges == [HEAD] and len(gh.reviews) == 1
