"""His "send" reaches the review he means, wherever it waits (11 Nov).

A task recorded a Request-changes review for a colleague's PR. It waited in the
offer queue behind an older approval for another PR. "task N send it" and
"send" only read the asked offer, so the brain re-drafted the review as a "pr"
text card that nothing could post — and staged it again on each "send".
Synthetic PR numbers and names throughout.
"""
from __future__ import annotations

import asyncio

import pytest

from app import loop, main, offers, ops, store, tasks

OLD = "https://example.test/org/booking-service/pull/4320"
NEW = "https://example.test/org/booking-service/pull/4321"


class Sink:
    def __init__(self):
        self.sent = []

    async def send(self, p):
        self.sent.append(p)

    def said(self):
        return " ".join(p.get("text") or "" for p in self.sent)


@pytest.fixture
def posted(monkeypatch):
    calls = []

    async def run(op):
        calls.append(op)
        return f"✅ {op['args']['action']} posted on {op['args']['pr']}"
    monkeypatch.setattr(ops, "run", run)
    monkeypatch.setattr(main, "_start_turn", lambda c, t, s, ch: calls.append({"brain": t}))
    return calls


def _review(pr, action, comments):
    return offers.staged_write("pr_review_inline", {"pr": pr, "action": action, "body": comments},
                               f"🔎 {action} on PR #{pr[-4:]}", comments, "Post this review as you?",
                               kind="pr_write")


def _his_morning():
    _review(OLD, "approve", "Approve.")                              # asked since 10:06
    _review(NEW, "request_changes", "Must fix: one thing.")           # 1 comment
    _review(NEW, "request_changes", "Must fix: one thing. Plus four more.")   # 5 comments
    return store.create_task("Review a colleague's PR #4321", "analysis", "review PR #4321", None)


def _say(conv, text):
    sink = Sink()
    asyncio.run(main._dispatch(conv, text, sink, "whatsapp"))
    return sink


def test_a_newer_review_of_the_same_pr_replaces_the_queued_one():
    _his_morning()
    queued = offers.waiting()
    assert len(queued) == 1 and "four more" in queued[0].context
    assert offers.pending().op["args"]["pr"] == OLD


def test_task_n_send_it_posts_that_tasks_review_not_the_one_in_front(posted):
    task = _his_morning()
    conv = store.create_conversation("claude_cli", None)
    sink = _say(conv, f"task {task['id']} send it")
    ops_run = [c for c in posted if "name" in c]
    assert [(c["args"]["pr"], c["args"]["action"]) for c in ops_run] == [(NEW, "request_changes")]
    assert "four more" in ops_run[0]["args"]["body"], "the newest review, exactly as recorded"
    assert "posted" in sink.said() and not [c for c in posted if "brain" in c]
    assert offers.pending().op["args"]["pr"] == OLD, "the approval he has not answered still waits"
    assert offers.review_for_pr(NEW) is None, "posted once, never twice"


def test_send_on_a_pr_text_card_posts_the_recorded_review(posted):
    _his_morning()
    conv = store.create_conversation("claude_cli", None)
    loop.stage(conv["id"], {"what": "Request changes. Must fix: one thing.",
                            "to": "org/booking-service#4321", "channel": "pr"})
    _say(conv, "send")
    ops_run = [c for c in posted if "name" in c]
    assert [c["args"]["pr"] for c in ops_run] == [NEW]
    assert not [c for c in posted if "brain" in c], "no brain asked to send what it cannot"


def test_post_pr_n_by_number_works_too(posted):
    _his_morning()
    conv = store.create_conversation("claude_cli", None)
    _say(conv, "post PR 4321")
    assert [c["args"]["pr"] for c in posted if "name" in c] == [NEW]


@pytest.mark.parametrize("text", ["don't send PR 4321 yet", "task {id} hold it",
                                  "what does PR 4321 change?"])
def test_anything_but_a_yes_posts_nothing(posted, text):
    task = _his_morning()
    conv = store.create_conversation("claude_cli", None)
    _say(conv, text.format(id=task["id"]))
    assert not [c for c in posted if "name" in c]
    assert offers.review_for_pr(NEW) is not None


def test_a_pr_text_draft_is_not_staged_when_a_review_is_recorded():
    from app import agent
    _his_morning()

    async def go(**kw):
        cid = tasks.bind_conversation("c-1")
        try:
            return agent.prepare_to_send(**kw)
        finally:
            tasks.unbind_conversation(cid)
    said = asyncio.run(go(what="Request changes. Must fix: one thing.",
                          to="org/booking-service#4321", channel="pr"))
    assert "already recorded" in said and "post PR 4321" in said
    said = asyncio.run(go(what="Looks fine.", to="org/booking-service#4399", channel="pr"))
    assert "already recorded" not in said, "a PR with no recorded review stages as before"


def test_task_hash_n_is_the_task_not_a_pr(posted):
    task = _his_morning()
    conv = store.create_conversation("claude_cli", None)
    _say(conv, f"task #{task['id']} send it")
    assert [c["args"]["pr"] for c in posted if "name" in c] == [NEW]
