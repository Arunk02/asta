"""12 Nov: a PR comment went up as "Review (task #572) — …" with none of its
points — its recorded review had expired overnight and a brain improvised.
Synthetic PRs and text."""
from __future__ import annotations

import asyncio
import time

import pytest

from app import main, offers, review, store, writing


@pytest.mark.parametrize("raw, clean", [
    ("Review (task #572) — CAMS container allocation: amendment-only.",
     "Review — CAMS container allocation: amendment-only."),
    ("Verdict: COMMENT — no blockers.\nWaiting on your yes to post.", "Verdict: COMMENT — no blockers."),
    ("task #572: the null check at Foo.java:10", "the null check at Foo.java:10"),
    ("See the Missions tab for the rest.\nReal point.", "Real point."),
])
def test_nothing_internal_goes_outside(raw, clean):
    assert writing.outward(raw) == clean


def test_pr_and_issue_numbers_stay():
    s = "Same fix as PR #1527; see #1532 for the amend path."
    assert writing.outward(s) == s


def test_a_review_body_and_its_comments_are_cleaned_on_the_way_out(monkeypatch, tmp_path):
    sent = {}

    async def git(cwd, *args, stdin="", timeout=0):
        import json
        sent["payload"] = json.loads(stdin) if stdin else {}
        return 0, "{}"

    async def gh(cwd, *args, timeout=0):
        return 1, ""
    monkeypatch.setattr(review, "_where", lambda pr, ws, repo: ("4321", ["-R", "org/svc"], tmp_path))
    monkeypatch.setattr(review.repo_ops, "git", git)
    monkeypatch.setattr(review, "_gh", gh)
    asyncio.run(review.post_inline_review(
        "https://github.com/org/svc/pull/4321", action="comment",
        body="Review (task #572) — two points.",
        comments=[{"path": "A.java", "line": 3, "body": "task #572: guard null"}]))
    p = sent["payload"]
    assert "#572" not in p["body"] and "#572" not in p["comments"][0]["body"]


def test_a_recorded_review_still_waits_the_next_morning():
    o = offers.staged_write("pr_review_inline", {"pr": "org/svc#4321", "action": "comment",
                                                  "body": "x", "comments": []},
                            "🔎 Comment on PR #4321", "x", "Post?", kind="pr_write")
    o.created = time.time() - 10 * 3600
    assert not o.expired(), "a review waits until its PR moves, up to 3 days"
    o.created = time.time() - 4 * 86400
    assert o.expired()


class Sink:
    def __init__(self):
        self.sent = []

    async def send(self, p):
        self.sent.append(p)


def test_post_with_nothing_recorded_is_answered_not_improvised(monkeypatch):
    brain = []
    monkeypatch.setattr(main, "_start_turn", lambda c, t, s, ch: brain.append(t))
    conv = store.create_conversation("claude_cli", None)
    sink = Sink()
    asyncio.run(main._dispatch(conv, "PR 4321 post it", sink, "whatsapp"))
    said = " ".join(p.get("text") or "" for p in sink.sent)
    assert "isn't waiting any more" in said and "review PR 4321" in said and not brain


@pytest.mark.parametrize("text", ["send the PR to Priya", "raise PR 4321", "post the PR 4321 link",
                                  "don't post PR 4321"])
def test_other_messages_naming_a_pr_are_not_caught(text):
    assert main._review_no_longer_waiting(text) is None
