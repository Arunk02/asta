"""Reviews the way he asked for them (11 Nov).

Approve when clean; after findings, wait for the author's fix and review again;
check every change against his standard; pick up review requests for his own
repos without being told, once per PR head. Synthetic repos, PRs and people.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app import ops, pr_authority, responder, review, store, tasks

URL = "https://github.com/org/svc-booking/pull/4321"
REF = "org/svc-booking#4321"
OLD = REF + "@" + "a" * 40 + ":f1"
NEW = REF + "@" + "b" * 40 + ":f2"


# --- A2: after findings, the author's fix is reviewed again -------------------------

@pytest.fixture
def pr_head(monkeypatch):
    state = {"head": OLD, "spawned": []}

    async def revision(pr):
        return state["head"], True

    async def brief(pr, ws="", repo=""):
        return "BRIEF", {"state": "OPEN", "author": {"login": "priya"}}

    def spawn(title, prompt, kind, ws=None, **kw):
        t = store.create_task(title, kind, prompt, ws)
        state["spawned"].append((t["id"], prompt))
        return t
    monkeypatch.setattr(review, "revision", revision)
    monkeypatch.setattr(review, "brief", brief)
    monkeypatch.setattr(tasks, "spawn", spawn)
    return state


def test_a_review_with_findings_waits_for_the_authors_fix(pr_head):
    pr_authority.watch_after_post(URL, "request_changes", OLD, body="- A.java:10 — null check")
    assert asyncio.run(pr_authority.watch_sweep()) == [] and not pr_head["spawned"], \
        "nothing new from the author: nothing re-reviewed"
    pr_head["head"] = NEW
    notes = asyncio.run(pr_authority.watch_sweep())
    (tid, prompt), = pr_head["spawned"]
    assert "approves if clean" in notes[0]
    assert "THIS IS A RE-REVIEW" in prompt and "A.java:10" in prompt
    rec = pr_authority.get(tid)
    assert rec["post"] and rec["ref"] == REF, "the re-review approves if clean, comments if not"
    assert asyncio.run(pr_authority.watch_sweep()) == [], "one re-review per new head"


def test_an_approval_ends_the_watch(pr_head):
    pr_authority.watch_after_post(URL, "request_changes", OLD)
    pr_authority.watch_after_post(URL, "approve", NEW)
    pr_head["head"] = NEW
    assert asyncio.run(pr_authority.watch_sweep()) == [] and not pr_head["spawned"]


def test_re_reviews_stop_after_the_cap(pr_head):
    pr_authority.watch_after_post(URL, "request_changes", OLD)
    for i in range(pr_authority.MAX_ROUNDS):
        pr_head["head"] = REF + "@" + str(i) * 40 + ":x"
        asyncio.run(pr_authority.watch_sweep())
    pr_head["head"] = NEW
    notes = asyncio.run(pr_authority.watch_sweep())
    assert len(pr_head["spawned"]) == pr_authority.MAX_ROUNDS and "stopped re-reviewing" in notes[0]


def test_a_watch_expires(pr_head):
    pr_authority.watch_after_post(URL, "request_changes", OLD,
                                  now=time.time() - (pr_authority.WATCH_DAYS + 1) * 86400)
    pr_head["head"] = NEW
    assert asyncio.run(pr_authority.watch_sweep()) == [] and not pr_head["spawned"]


def test_posting_findings_starts_the_watch(monkeypatch):
    async def post(*a, **k):
        return "posted"

    async def revision(pr):
        return OLD, True
    monkeypatch.setattr(review, "post_inline_review", post)
    monkeypatch.setattr(review, "revision", revision)
    asyncio.run(ops._pr_review_inline(pr=URL, action="request_changes", body="VERDICT: Request changes",
                                      comments=[{"path": "A.java", "line": 10, "body": "null check"}],
                                      review_origin={"ref": REF, "revision": OLD}))
    w = json.loads(store.kv_get(pr_authority.WATCH + REF))
    assert w["revision"] == OLD and "A.java:10" in w["last"]


# --- A3: his review standard reaches every review ----------------------------------

def test_every_review_checks_his_standard():
    b = review.REVIEW_BRIEF
    for x in ("existing utility", "duplicate", "trace every caller", "component", "Logs to debug",
              "Memory", "map/set", "O(n²)", "idempotency", "Temporal determinism",
              "APPROVE only when nothing is BLOCKING"):
        assert x in b, x


# --- A4: review requests are picked up for his repos, once per PR head -----------------

@pytest.fixture
def his_repos(monkeypatch):
    monkeypatch.setattr(responder, "_workspace_repos", lambda: {"svc-booking", "svc-email"})


def test_a_pr_outside_his_workspace_is_named_not_reviewed(his_repos):
    assert responder.outside_repo("pls review https://github.com/org/other-svc/pull/9") == "org/other-svc#9"
    assert responder.outside_repo(f"pls review {URL}") == ""
    assert responder.outside_repo("pls review PR 4321") == "", "a bare number is his workspace's"


def test_a_review_request_is_not_dropped_as_old_or_as_a_room(monkeypatch):
    monkeypatch.setattr(responder, "enabled", lambda: True)
    now = time.time()
    two_hours = now - 2 * 3600
    assert responder.should_respond("review_request", None, "k1", now=now, broadcast=True,
                                    sent_at=two_hours) == ""
    assert "not a live ask" in responder.should_respond("debug", None, "k2", now=now,
                                                         sent_at=two_hours)
    assert "room" in responder.should_respond("debug", None, "k3", now=now, broadcast=True)


class _Proc:
    def __init__(self, rows):
        self.rows = rows

    async def communicate(self):
        return json.dumps(self.rows).encode(), b""


def _gh_rows(*prs):
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return [{"url": f"https://github.com/org/{repo}/pull/{n}", "number": n, "title": f"PR {n}",
             "repository": {"name": repo}, "author": {"login": "priya"},
             "updatedAt": updated or iso} for repo, n, updated in prs]


def test_github_review_requests_are_picked_up_once_per_head_two_at_a_time(his_repos, monkeypatch):
    stale = "2020-01-01T00:00:00Z"
    rows = _gh_rows(("svc-booking", 1, None), ("other-svc", 2, None), ("svc-email", 3, None),
                    ("svc-booking", 4, None), ("svc-booking", 5, stale))

    async def exec_(*a, **k):
        return _Proc(rows)

    async def revision(pr):
        return f"{pr.split('github.com/')[1].replace('/pull/', '#')}@{'c' * 40}:x", True
    started = []

    def respond(source, who, text, **kw):
        started.append(text)
        return {"id": len(started)}
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_)
    monkeypatch.setattr(review, "revision", revision)
    monkeypatch.setattr(responder, "respond", respond)
    first = asyncio.run(responder.github_review_requests())
    assert [t for t in started] == [f"please review https://github.com/org/svc-booking/pull/1",
                                    f"please review https://github.com/org/svc-email/pull/3"]
    asyncio.run(responder.github_review_requests())
    assert started[2:] == ["please review https://github.com/org/svc-booking/pull/4"], \
        "the next pass takes the next one; seen ones are not reviewed twice; stale ones never"
    assert len(first) == 2
