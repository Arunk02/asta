"""19 Nov: a colleague asked in a group for four prod-config PRs to be approved.
He said "Approve all the PRs he shared", "Just approve all the PR", "Yes approve",
"Yes", "Yes go ahead and approve" — and not one approval reached GitHub:

  * "all the PRs he shared" named no PR, so no approval was granted up front;
  * the three APPROVE reviews queued behind an older COMMENT review, and "Yes
    approve" posted the comment;
  * "Yes go ahead and approve" was taken by the code-task go-ahead, which
    answered about an old finished task;
  * the reply draft he redirected came back as "Asking again".

Synthetic people, repos and PR numbers throughout.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app import answers, capabilities, loop, main, offers, ops, pr_authority, store

A = "https://github.com/acme/svc-a/pull/4101"
B = "https://github.com/acme/svc-b/pull/4102"
C = "https://github.com/acme/svc-c/pull/4103"
OLD = "https://github.com/acme/svc-d/pull/4104"


def _stage(pr: str, action: str, said: str = "", from_task: bool = False):
    args = {"pr": pr, "action": action, "body": f"VERDICT: {action.upper()}", "comments": [],
            "review_origin": {"ref": pr, "revision": "r"}}
    t1 = capabilities.TURN_TEXT.set(said)
    t2 = capabilities.FROM_TASK.set(1 if from_task else None)
    try:
        return offers.staged_write("pr_review_inline", args, f"🔎 {action} {pr}", "x",
                                   "Post?", kind="pr_write")
    finally:
        capabilities.FROM_TASK.reset(t2)
        capabilities.TURN_TEXT.reset(t1)


@pytest.fixture
def github(monkeypatch):
    posted: list[tuple[str, str]] = []

    async def run(op):
        a = op["args"]
        posted.append((a["pr"], a["action"]))
        return f"✅ {a['action']} on {a['pr']}"

    async def notify(*a, **k):
        pass

    from app import notify as notify_mod
    monkeypatch.setattr(ops, "run", run)
    monkeypatch.setattr(notify_mod, "notify", notify)
    return posted


def _queue_like_19_nov():
    """An older comment review in front, then the three approvals his turn staged."""
    _stage(OLD, "comment")
    for pr in (A, B, C):
        _stage(pr, "approve", said="Just approve all the PR")


# --- "all the PRs he shared" is an approval of each of them ------------------------

def test_all_the_prs_he_shared_names_each_pr_in_front_of_him():
    view = {"acme/svc-a#4101", "acme/svc-b#4102"}
    assert pr_authority._names("Approve all the PRs he shared", "acme/svc-a#4101", view)
    assert pr_authority._names("just approve all the PR", "acme/svc-b#4102", view)
    assert not pr_authority._names("Approve all the PRs he shared", "acme/svc-x#9", view)
    assert not pr_authority._names("approve it", "acme/svc-a#4101", frozenset())


def test_the_prs_in_view_include_a_colleagues_recent_ask():
    t = store.create_task("ask", "analysis", "p", None)
    store.kv_set(f"answer_meta:{t['id']}", json.dumps(
        {"who": "Colleague One", "group": True, "source_text": f"{A} {B}"}))
    assert {"acme/svc-a#4101", "acme/svc-b#4102"} <= pr_authority.in_view()


def test_the_grant_is_recorded_for_a_pr_in_view():
    t = store.create_task("Review", "analysis", "p", None)
    rec = pr_authority.record(t["id"], "acme/svc-a#4101", "acme/svc-a#4101@h", "Approve all the PRs",
                              view={"acme/svc-a#4101"})
    assert rec and rec["approve"]


def test_a_chat_turn_that_asks_for_the_approval_posts_it():
    view_ref = "acme/svc-a#4101"
    t = store.create_task("ask", "analysis", "p", None)
    store.kv_set(f"answer_meta:{t['id']}", json.dumps({"source_text": A}))
    assert pr_authority.asked_for("Just approve all the PR", view_ref, "approve")
    assert not pr_authority.asked_for("don't approve them yet", view_ref, "approve")
    assert not pr_authority.asked_for("Just approve all the PR", view_ref, "comment")


# --- one review per PR, and one yes for the batch -----------------------------------

def test_a_newer_review_of_the_same_pr_replaces_the_older_one_whatever_its_verdict():
    _stage(OLD, "comment")
    _stage(A, "comment")
    _stage(A, "approve")
    rows = [(o.op["args"]["pr"], o.op["args"]["action"]) for o in offers.waiting_reviews()]
    assert rows.count((A, "approve")) == 1 and (A, "comment") not in rows


def test_his_approve_takes_every_approval_he_was_shown_and_not_the_comment():
    _queue_like_19_nov()
    picks = offers.take_approved_reviews(approve_only=True)
    assert sorted(o.op["args"]["pr"] for o in picks) == [A, B, C]
    left = offers.waiting_reviews()
    assert [o.op["args"]["pr"] for o in left] == [OLD]


def test_a_bare_yes_takes_the_batch_he_was_asked_about_last():
    _queue_like_19_nov()
    picks = offers.take_approved_reviews(approve_only=False)
    assert sorted(o.op["args"]["pr"] for o in picks) == [A, B, C]


def test_a_bare_yes_to_a_single_offer_in_front_is_left_to_the_ordinary_path():
    _stage(A, "approve", said="approve A")
    assert offers.take_approved_reviews(approve_only=False) == []


# --- through the chat path ----------------------------------------------------------

def _no_brain(*a, **k):
    raise AssertionError("no brain should be asked")


@pytest.mark.parametrize("said", ["Yes go ahead and approve", "Yes approve", "Just approve all the PR"])
def test_his_approve_posts_the_three_approvals_not_the_comment_and_not_a_task(
        monkeypatch, github, said):
    from tests.test_say_it_once import _Sink, _conv
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    conv = _conv()
    done = store.create_task("An older finished code task", "code", "p", None)
    store.update_task(done["id"], status="done")
    _queue_like_19_nov()
    sink = _Sink()
    asyncio.run(main._dispatch(conv, said, sink, "whatsapp"))
    assert sorted(github) == [(A, "approve"), (B, "approve"), (C, "approve")]
    text = " ".join(p.get("text", "") for p in sink.sent)
    assert f"#{done['id']}" not in text, "the go-ahead must not answer about a code task"


def test_a_bare_yes_after_send_the_three_posts_the_three(monkeypatch, github):
    from tests.test_say_it_once import _Sink, _conv
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    conv = _conv()
    _queue_like_19_nov()
    asyncio.run(main._dispatch(conv, "Yes", _Sink(), "whatsapp"))
    assert sorted(github) == [(A, "approve"), (B, "approve"), (C, "approve")]


def test_a_held_word_posts_nothing(monkeypatch, github):
    from tests.test_say_it_once import _Sink, _conv
    monkeypatch.setattr(main, "_start_turn", lambda *a, **k: None)
    conv = _conv()
    _queue_like_19_nov()
    asyncio.run(main._dispatch(conv, "don't approve yet", _Sink(), "whatsapp"))
    assert github == []


def test_a_colleague_draft_about_those_prs_is_answered_by_the_reviews(monkeypatch, github):
    from tests.test_say_it_once import _Sink, _conv
    monkeypatch.setattr(main, "_start_turn", _no_brain)
    conv = _conv()
    loop.stage(conv["id"], {"type": "answer", "kind": "send", "to": "Colleague One, Two, +2",
                            "to_group": True, "who": "Colleague One",
                            "what": "4101 and 4102 look fine", "need": "approve PRs"})
    _queue_like_19_nov()
    asyncio.run(main._dispatch(conv, "approve all", _Sink(), "whatsapp"))
    assert len(github) == 3
    assert not loop.awaiting(conv["id"])


# --- the redirected draft does not come back ---------------------------------------

def test_a_draft_answered_with_an_approval_is_not_asked_again(monkeypatch):
    queued = []
    monkeypatch.setattr(answers, "_enqueue", lambda item: queued.append(item))
    cid = "c-redirect"
    staged = {"type": "answer", "to": "Colleague One, Two, +2", "to_group": True,
              "who": "Colleague One", "what": "PR 4101 is fine", "need": "approve four PRs"}
    main._under_feedback[cid] = (staged, "Just approve all the PR", time.time())
    main._settle_feedback(cid, None)
    assert queued == []


def test_a_draft_his_message_was_not_about_still_comes_back(monkeypatch):
    queued = []
    monkeypatch.setattr(answers, "_enqueue", lambda item: queued.append(item))
    cid = "c-other"
    staged = {"type": "answer", "to": "Colleague One", "who": "Colleague One",
              "what": "will check the logs", "need": "a log check"}
    main._under_feedback[cid] = (staged, "how many chats are unread", time.time())
    main._settle_feedback(cid, None)
    assert len(queued) == 1


# --- the card says where the reply goes --------------------------------------------

def test_the_card_names_the_group_for_a_group_ask():
    text = answers.render("Colleague One", "approve four PRs", "", "Looks fine",
                          chat="Colleague One, Two, +2", group=True)
    assert "in *Colleague One, Two, +2* (the group Colleague asked in)" in text


def test_the_card_says_1to1_for_a_direct_ask():
    text = answers.render("Colleague One", "a check", "", "Done", chat="Colleague One")
    assert "to Colleague (1:1)" in text


def test_a_plan_shown_after_the_reviews_takes_the_go_ahead(monkeypatch, github):
    """The newer question wins: a plan reached him after the reviews were staged."""
    _queue_like_19_nov()
    t = store.create_task("A code change", "code", "p", None)
    store.update_task(t["id"], status="awaiting_approval")
    store.add_task_event(t["id"], "plan", "full plan sent")
    assert main._reviews_his_word_approves("yes go ahead and approve", "c-plan") == []
    assert len(offers.waiting_reviews()) == 4, "nothing consumed"
