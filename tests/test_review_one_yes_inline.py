"""11 Nov, booking PR 1536: two "yes" to post one review, and GitHub refused the
inline comments ("Path could not be resolved"). Synthetic PR and files."""
from __future__ import annotations

import asyncio
import json

from app import main, offers, ops, review, store, tasks

FULL = "service/src/main/java/org/x/OneClickValidator.java"
TEST = "service/src/test/java/org/x/OneClickValidatorTest.java"
PATCH = "@@ -40,3 +40,5 @@ class X {\n ctx\n-old\n+new1\n+new2\n ctx2\n"


def test_new_side_lines_are_where_comments_attach():
    assert review.new_side_lines(PATCH) == {40, 41, 42, 43}


def test_a_short_path_is_made_exact_and_an_off_diff_line_goes_to_the_body():
    files = {FULL: PATCH, TEST: "@@ -1,1 +1,2 @@\n a\n+b\n"}
    inline, rest = review.attachable([
        {"path": "OneClickValidator.java", "line": 41, "body": "null check"},
        {"path": FULL, "line": 42, "body": "exact path"},
        {"path": "OneClickValidator.java", "line": 99, "body": "outside the change"},
        {"path": "Missing.java", "line": 1, "body": "not in the PR"},
    ], files)
    assert [(c["path"], c["line"]) for c in inline] == [(FULL, 41), (FULL, 42)]
    assert [c["body"] for c in rest] == ["outside the change", "not in the PR"]


def test_an_ambiguous_short_path_is_not_guessed():
    files = {"a/Foo.java": PATCH, "b/Foo.java": PATCH}
    inline, rest = review.attachable([{"path": "Foo.java", "line": 41, "body": "x"}], files)
    assert not inline and rest


def test_the_inline_review_posts_what_attaches_and_lists_the_rest(monkeypatch, tmp_path):
    sent = {}

    async def gh(cwd, *args, timeout=0):
        return 0, json.dumps({"filename": FULL, "patch": PATCH})

    async def git(cwd, *args, stdin="", timeout=0):
        sent["payload"] = json.loads(stdin)
        return 0, "{}"
    monkeypatch.setattr(review, "_gh", gh)
    monkeypatch.setattr(review, "_where", lambda pr, ws, repo: ("1536", ["-R", "org/svc"], tmp_path))
    monkeypatch.setattr(review.repo_ops, "git", git)
    out = asyncio.run(review.post_inline_review(
        "https://github.com/org/svc/pull/1536", action="comment", body="VERDICT: Comment",
        comments=[{"path": "OneClickValidator.java", "line": 41, "body": "null check"},
                  {"path": "OneClickValidator.java", "line": 99, "body": "outside"}]))
    p = sent["payload"]
    assert [(c["path"], c["line"]) for c in p["comments"]] == [(FULL, 41)]
    assert "outside" in p["body"] and "1 inline comment" in out


# --- one yes ----------------------------------------------------------------------

def test_the_note_that_shows_a_review_counts_as_showing_it(monkeypatch):
    from app import capabilities
    token = capabilities.FROM_TASK.set(1)            # staged by a task: not yet shown
    try:
        o = offers.staged_write("pr_review_inline",
                                {"pr": "org/svc#4321", "action": "comment", "body": "x",
                                 "comments": [], "review_origin": {"ref": "org/svc#4321",
                                                                   "revision": "r"}},
                                "🔎 Comment on PR #4321", "x", "Post?", kind="pr_write")
    finally:
        capabilities.FROM_TASK.reset(token)
    assert offers.pending().shown is False
    t = store.create_task("Review booking PR 4321", "analysis", "p", None)
    assert "Say “yes”" in tasks._how_to_post(t["id"], store.get_task(t["id"]))
    assert offers.pending().shown is True, "his next yes posts it — not a second showing"


def test_an_elided_path_is_resolved_by_what_follows_the_dots():
    files = {FULL: PATCH}
    inline, _ = review.attachable(
        [{"path": "service/src/main/java/.../OneClickValidator.java", "line": 41, "body": "x"}], files)
    assert [c["path"] for c in inline] == [FULL]


def test_the_brief_asks_for_full_paths_and_changed_lines():
    assert 'never shorten it with "..."' in review.REVIEW_BRIEF and "NEW-side line" in review.REVIEW_BRIEF
