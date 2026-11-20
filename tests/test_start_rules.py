"""Work Asta starts on its own obeys his rules — typed where it can be, read where not.

20 Nov: "only when the ask and ping in teams u do the review or else won't" was
stored as a note. The GitHub review-request watcher read no notes, and a colleague's
PR was reviewed an hour later. Synthetic names and repos throughout.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app import instructions, policy, responder, review, rulebook, store, tasks

HIS_RULE = "Always remember only when the ask and ping in teams u do the review or else won't"


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("ASTA_RESPOND", "1")
    store.kv_set(responder._RATE_KEY, "")
    store.kv_set(responder._DONE_KEY, "")

    async def no_model(*a, **k):
        return None
    monkeypatch.setattr(rulebook, "_complete", no_model)


def _apply(words):
    intent = asyncio.run(rulebook.read(words))
    return rulebook.apply(intent) if intent else None


# --- the typed rule, where every self-started piece of work begins ---------------

@pytest.mark.parametrize("words", [
    HIS_RULE,
    "do the review only when someone pings me in Teams",
    "only review PRs if they ask in teams",
    "don't pick up PRs automatically",
    "dont auto review github PRs",
    "don't start reviews on your own",
])
def test_his_review_rule_is_a_rule_code_holds(words):
    done = _apply(words)
    assert done is not None and done.intent.effect == "block", words
    r = [x for x in policy.rules("never") if x.act == "start"][0]
    assert r.target == "review_request"
    assert "Don't start reviews on your own" in done.line


def test_a_github_request_is_held_and_a_teams_ask_still_runs():
    _apply(HIS_RULE)
    why = responder.should_respond("review_request", None, "k1", source="github")
    assert "your standing rule" in why and "reviews" in why
    assert responder.should_respond("review_request", None, "k2", source="teams-chat") == ""
    assert responder.should_respond("review_request", None, "k3", source="outlook") != ""
    assert responder.should_respond("debug", None, "k4", source="github") == "", "only reviews"


def test_the_watcher_does_not_even_look(monkeypatch):
    _apply(HIS_RULE)

    async def exec_(*a, **k):
        pytest.fail("held: no GitHub search at all")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_)
    assert asyncio.run(responder.github_review_requests()) == []


def test_an_unscoped_rule_and_a_github_only_rule():
    policy.add("never", "start", "review_request", value="only=github", words="no github reviews")
    assert not policy.check("start", "review_request", source="github").ok
    assert policy.check("start", "review_request", source="outlook").ok


@pytest.mark.parametrize("words", [
    "review booking PR 1481 again",                     # a one-off ask, not a withdrawal
    "send the review only after she asks",              # a one-off send
    "Don't review Priya's PR",                          # one PR, not a standing rule
    "only when the CI is green merge it",               # no work kind named
])
def test_one_offs_are_never_a_block_and_never_end_one(words, monkeypatch):
    async def none(*a, **k):
        return '{"op": "none"}'
    monkeypatch.setattr(rulebook, "_complete", none)
    _apply(HIS_RULE)
    assert _apply(words) is None, words
    assert len([r for r in policy.rules("never") if r.act == "start"]) == 1


@pytest.mark.parametrize("words", [
    "pick up reviews on your own again",
    "start reviews automatically again",
    "lift the review rule",
])
def test_the_block_is_withdrawn_in_his_words(words):
    _apply(HIS_RULE)
    done = _apply(words)
    assert done is not None and done.intent.op == "withdraw", words
    assert policy.check("start", "review_request", source="github").ok


def test_my_rules_shows_it():
    from app import frontdesk
    _apply(HIS_RULE)
    assert "Don't start reviews on your own — only when asked in Teams" in \
        frontdesk.answer_from_state("my rules")


# --- notes saved before a kind existed become rules ------------------------------

def test_a_note_that_reads_as_a_rule_becomes_one_at_start():
    note = policy.add("note", words=HIS_RULE)
    other = policy.add("note", words="Dont include the bot name in feature branch names")
    done = rulebook.adopt_notes()
    assert len(done) == 1 and f"R{note.id}" in done[0]
    assert [r.id for r in policy.rules("note")] == [other.id], "the style note stays a note"
    assert not policy.check("start", "review_request", source="github").ok
    assert rulebook.adopt_notes() == [], "idempotent"


def test_a_note_says_plainly_that_no_code_holds_it():
    line = instructions.adopt({"kind": "note", "words": "Write PR titles in the imperative"})
    assert "no code rule holds it" in line


# --- the last net: what only a note says, read before self-started work runs -----

def _spawn_self_started(monkeypatch, verdict):
    started = []

    async def complete(prompt, **k):
        return json.dumps(verdict)
    monkeypatch.setattr(rulebook, "_complete", complete)
    monkeypatch.setattr(tasks, "_start", lambda t: started.append(t["id"]) or t)

    async def go():
        t = tasks.spawn("Review someone's PR #9", "please review https://github.com/org/svc/pull/9",
                        "analysis", self_started="github")
        await asyncio.sleep(0)
        job = tasks._running.get(t["id"])
        if job:
            await job
        return t
    return asyncio.run(go()), started


def test_a_note_that_forbids_it_holds_the_work_and_says_why(monkeypatch):
    policy.add("note", words="never start anything for the release channel on your own")
    t, started = _spawn_self_started(monkeypatch, {"forbidden": True, "note": 1})
    assert started == []
    row = store.get_task(t["id"])
    assert row["status"] == "cancelled" and "release channel" in row["result"]


def test_a_note_that_does_not_forbid_it_lets_it_run(monkeypatch):
    policy.add("note", words="Write PR titles in the imperative")
    t, started = _spawn_self_started(monkeypatch, {"forbidden": False, "note": None})
    assert started == [t["id"]]


def test_no_model_means_it_runs_as_before(monkeypatch):
    policy.add("note", words="never start anything for the release channel on your own")
    t, started = _spawn_self_started(monkeypatch, {"something": "else"})
    assert started == [t["id"]]


def test_work_he_asks_for_is_never_checked_against_notes(monkeypatch):
    policy.add("note", words="never start anything on your own")
    started = []
    monkeypatch.setattr(tasks, "_start", lambda t: started.append(t["id"]) or t)

    async def boom(*a, **k):
        pytest.fail("his own ask is not read against notes")
    monkeypatch.setattr(rulebook, "_complete", boom)

    async def go():
        return tasks.spawn("Look at booking X", "check booking X in sit", "analysis")
    t = asyncio.run(go())
    assert started == [t["id"]]
