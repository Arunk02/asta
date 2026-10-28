"""A review staged while CI ran is still posted when CI finishes on the same commit.

28 Oct: a colleague's PR review was staged while CI was running; by the time he
said yes, CI had finished on the very same commit, and the post was refused as
'PR head or CI changed'. Only a new commit means the review is stale."""
from __future__ import annotations

import asyncio

import pytest

from app import ops, review

REF = "acme/booking#1409"
HEAD = "a" * 40
STAGED = f"{REF}@{HEAD}:ci-running"            # fingerprint taken while CI ran


@pytest.fixture
def posted(monkeypatch):
    out = []

    async def post(pr, workspace="", repo="", action="comment", body="", comments=None):
        out.append(action)
        return f"{action} posted"

    monkeypatch.setattr(review, "post_inline_review", post)
    return out


def at(current: str, good: bool, monkeypatch):
    async def revision(pr):
        return current, good
    monkeypatch.setattr(review, "revision", revision)


def run(action):
    return asyncio.run(ops.REGISTRY["pr_review_inline"]["run"](
        pr="https://github.com/acme/booking/pull/1409", action=action, body="VERDICT",
        comments=[{"path": "a.py", "line": 1, "body": "x"}],
        review_origin={"ref": REF, "revision": STAGED}))


@pytest.mark.parametrize("action", ["comment", "request_changes", "approve"])
def test_ci_finishing_on_the_same_commit_does_not_block_the_post(posted, monkeypatch, action):
    at(f"{REF}@{HEAD}:ci-finished", True, monkeypatch)
    assert "posted" in run(action) and posted == [action]


def test_a_new_commit_means_review_again(posted, monkeypatch):
    at(f"{REF}@{'b' * 40}:ci-finished", True, monkeypatch)
    with pytest.raises(RuntimeError, match="head changed"):
        run("comment")
    assert posted == []


def test_an_approval_still_needs_green_ci_when_it_is_posted(posted, monkeypatch):
    at(f"{REF}@{HEAD}:ci-red", False, monkeypatch)
    with pytest.raises(RuntimeError, match="not green"):
        run("approve")
    assert run("comment") and posted == ["comment"], "a comment does not wait for CI"


def test_same_commit_compares_heads_only():
    assert review.same_commit(f"{REF}@{HEAD}:x", f"{REF}@{HEAD}:y")
    assert not review.same_commit(f"{REF}@{HEAD}:x", f"{REF}@{'b' * 40}:x")
    assert not review.same_commit("", f"{REF}@{HEAD}:x")
