"""Reviews that finish — Round 3, P11.

"PR review it telling it analysing but it doesnt able to do and summarise or add
comments in PR". Three separate faults sat behind that one sentence:

  * "please review my PR" was read as "a colleague left feedback on YOUR PR", so
    the worker went off to check whether the author's own points about their own
    code were right;
  * a PR could only be read from a local clone, so a complete request — a link —
    came back as "name the repo as well";
  * findings written as `path:line` were flattened into one body, when GitHub
    takes them as comments on the lines they are about.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import agent, offers, responder, review, store


# --- a link is a complete request ------------------------------------------------------

@pytest.mark.parametrize("said, number, target", [
    ("https://github.com/acme/booking/pull/1409", "1409", "acme/booking"),
    # Teams strips the punctuation out of links in its feed — the common shape.
    ("hi arun please review https github com acme booking pull 1409 thanks",
     "1409", "acme/booking"),
    ("acme/booking#1409", "1409", "acme/booking"),
    ("1409", "1409", ""),
    ("#1409", "1409", ""),
])
def test_a_pr_is_found_however_he_was_sent_it(said, number, target):
    assert review.pr_target(said) == (number, target)


def test_a_link_needs_no_clone(monkeypatch):
    """The old path demanded a workspace checkout for a request that already
    carried everything: "is not a git repository — name the repo as well"."""
    seen: list[list[str]] = []

    async def gh(cwd, *args, timeout=120, stdin=""):
        seen.append(list(args))
        if "view" in args:
            return 0, json.dumps({"number": 1409, "title": "Fix the consumer",
                                  "author": {"login": "a-colleague"}, "files": [],
                                  "additions": 10, "deletions": 2, "changedFiles": 1})
        return 0, ""

    monkeypatch.setattr(review.repo_ops, "git", gh)
    meta = asyncio.run(review.gather("https://github.com/acme/booking/pull/1409"))
    assert meta["number"] == 1409 and meta["target"] == "acme/booking"
    assert ["gh", "pr", "view", "1409", "-R", "acme/booking"] == seen[0][:6]


# --- whose pull request is it? ---------------------------------------------------------

@pytest.mark.parametrize("said, kind", [
    ("hi arun please review my PR https github com acme booking pull 1409", "review_request"),
    ("can you review this PR? https github com acme booking pull 1409", "review_request"),
    ("PR is up for the consumer fix, pull 1409", "review_request"),
    ("I left some comments on your PR 1409", "pr_review"),
    ("arun, my comments on your pull request 1409 — the retry looks wrong", "pr_review"),
    ("i reviewed your PR, one nit", "pr_review"),
])
def test_asking_for_a_review_is_not_the_same_as_leaving_one(said, kind):
    assert responder.what_it_asks(said) == kind


def test_each_framing_gets_the_job_it_actually_is():
    theirs = responder.brief_for("review_request", "A colleague", "please review my PR 1409")
    assert "REVIEW their pull request" in theirs
    assert "review_pr" in theirs and "propose_pr_review" in theirs
    assert "do not approve anything yourself" in theirs.lower()
    his = responder.brief_for("pr_review", "A colleague", "left comments on your PR 1409")
    assert "review feedback" in his and "whether it is a real defect" in his


def test_the_title_says_which_job_it_is():
    assert responder.title_for("review_request", "Sam", "review my PR 1409") \
        == "Review Sam's PR #1409"
    assert "is it right?" in responder.title_for("pr_review", "Sam",
                                                 "comments on your PR 1409")


def test_both_of_his_github_accounts_are_him(monkeypatch):
    """He has two logins, and `gh` can only be active as one — so his own pull
    request under the other account read as somebody else's, which is the one
    fact the whole framing turns on."""
    monkeypatch.setenv("ASTA_GITHUB_LOGINS", "workacct, personalacct")

    async def gh(cwd, *args, timeout=120, stdin=""):
        return 0, "github.com\n  - Active account: true\n  - account workacct (keyring)"

    async def active_login():
        return "workacct"

    from app import ci_watch
    monkeypatch.setattr(review.repo_ops, "git", gh)
    monkeypatch.setattr(ci_watch, "my_login", active_login)
    assert asyncio.run(review.whose_pr({"author": {"login": "PersonalAcct"}})) == "his"
    assert asyncio.run(review.whose_pr({"author": {"login": "workacct"}})) == "his"
    assert asyncio.run(review.whose_pr({"author": {"login": "a-colleague"}})) == "theirs"


# --- findings become comments on lines -------------------------------------------------

_NOTES = """VERDICT: REQUEST CHANGES — the retry loop can drop a message.

BLOCKING (must fix):
- `src/Consumer.java:88` — the offset is committed before the handler returns → commit after
- `src/Consumer.java:120` — unbounded retry → cap it and dead-letter

NON-BLOCKING (worth raising, not gating):
- `src/Config.java:14` — magic number → name it
- this one names no file at all, so it cannot be attached anywhere

TESTS:
- `src/test/ConsumerTest.java:1` — no test for the crash path → add one

QUESTIONS (only what the diff truly cannot answer):
- `src/Consumer.java:60` — is this topic partitioned by booking id?
"""


def test_every_finding_lands_on_its_own_line():
    found = review.parse_findings(_NOTES)
    assert [(f["path"], f["line"]) for f in found] == [
        ("src/Consumer.java", 88), ("src/Consumer.java", 120), ("src/Config.java", 14),
        ("src/test/ConsumerTest.java", 1), ("src/Consumer.java", 60)]
    assert [f["blocking"] for f in found] == [True, True, False, False, False]
    assert found[-1]["body"].startswith("Question:")


def test_a_point_with_no_line_is_left_out_rather_than_guessed_at():
    """GitHub silently drops a comment it cannot attach, and a review that half
    lands is worse than one that does not."""
    assert all("names no file" not in f["body"] for f in review.parse_findings(_NOTES))


@pytest.mark.parametrize("notes, action", [
    ("VERDICT: APPROVE — clean.", "approve"),
    ("VERDICT: REQUEST CHANGES — no.", "request_changes"),
    ("VERDICT: COMMENT — mostly fine.", "comment"),
    ("I think it's fine honestly", "comment"),          # never an approval by default
    ("", "comment"),
])
def test_the_verdict_is_read_and_never_assumed(notes, action):
    assert review.verdict_of(notes) == action


# --- staged, never posted --------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db", raising=False)
    store.init()
    yield


def test_a_review_is_staged_for_his_yes_and_posts_nothing(monkeypatch):
    posted: list = []

    async def never(*a, **k):
        posted.append(a)
        return "posted"

    monkeypatch.setattr(review, "post_inline_review", never)
    said = asyncio.run(agent.propose_pr_review(
        "https://github.com/acme/booking/pull/1409", _NOTES))
    assert "Staged" in said and "5 inline comment" in said and "2 blocking" in said
    assert posted == []
    pending = offers.pending()
    assert pending is not None and "1409" in pending.render()


def test_notes_with_nothing_attachable_say_so_instead_of_staging_a_blank_review():
    said = asyncio.run(agent.propose_pr_review("1409", "VERDICT: COMMENT\n- looks fine"))
    assert "nothing to attach" in said and offers.pending() is None


def test_the_review_goes_up_as_one_call_with_a_comment_per_line(monkeypatch):
    sent: dict = {}

    async def gh(cwd, *args, timeout=120, stdin=""):
        sent["args"] = list(args)
        sent["body"] = json.loads(stdin) if stdin else {}
        return 0, "{}"

    monkeypatch.setattr(review.repo_ops, "git", gh)
    said = asyncio.run(review.post_inline_review(
        "https://github.com/acme/booking/pull/1409", action="request_changes",
        body="VERDICT: REQUEST CHANGES", comments=review.parse_findings(_NOTES)))
    assert "gh" == sent["args"][0] and "api" == sent["args"][1]
    assert "repos/acme/booking/pulls/1409/reviews" in sent["args"]
    assert sent["body"]["event"] == "REQUEST_CHANGES"
    assert len(sent["body"]["comments"]) == 5
    assert sent["body"]["comments"][0] == {
        "path": "src/Consumer.java", "line": 88, "side": "RIGHT",
        "body": "the offset is committed before the handler returns → commit after"}
    assert "5 inline comment" in said


def test_when_github_refuses_the_inline_comments_the_findings_still_arrive(monkeypatch):
    """A comment on a line outside the diff is rejected together with the whole
    review. Losing the findings at that point is the one unacceptable outcome."""
    calls: list[list[str]] = []

    async def gh(cwd, *args, timeout=120, stdin=""):
        calls.append(list(args))
        if "api" in args:
            return 1, "422 line must be part of the diff"
        return 0, ""

    monkeypatch.setattr(review.repo_ops, "git", gh)
    said = asyncio.run(review.post_inline_review(
        "acme/booking#1409", action="comment", body="VERDICT: COMMENT",
        comments=review.parse_findings(_NOTES)))
    assert "as one comment" in said and "422" in said
    fallback = " ".join(calls[-1])
    assert "pr review" in fallback and "src/Consumer.java:88" in fallback
