"""One ask, one task, one PR: the four ways a single code ask went sideways (5 Nov).

Synthetic throughout: the colleague, PR numbers, branches and ids are made up.
"""
from __future__ import annotations

import asyncio
import json
import time
import types
from pathlib import Path

import pytest

from app import answers, capabilities, offers, responder, store, tasks

# --- 1. the PR to work on is one a person named -------------------------------


@pytest.fixture
def open_prs(monkeypatch, tmp_path):
    repo = tmp_path / "ws" / "svc-booking"
    repo.mkdir(parents=True)
    from app import worktrees
    monkeypatch.setattr(worktrees, "repos_in", lambda root: [repo])
    monkeypatch.setattr(worktrees, "outside", lambda *a, **k: [])
    heads = {"4242": "fix/old-eta-check", "777": "feature/the-one-he-named"}

    async def head(local, number):
        return heads.get(number, "")
    monkeypatch.setattr(tasks, "_open_pr_head", head)
    return tmp_path / "ws"


BRIEF = ("Skip the tracker call for one-way empty orders. This validation is the one "
         "added in Arun's existing PR 4242 — find that path and add the condition.")


def _task(prompt, asked=None):
    t = store.create_task("Skip tracker call for one-way empty orders", "code", prompt, "ws")
    if asked is not None:
        store.kv_set(f"task_asked:{t['id']}", json.dumps(asked))
    return t


def test_a_pr_only_the_brief_mentions_is_not_the_branch_to_work_on(open_prs):
    t = _task(BRIEF, {"his": "we have to skip this call for one way empty, fix and raise PR",
                      "offer": ""})
    assert asyncio.run(tasks.pr_head_branch(t, open_prs)) == {}


def test_a_pr_he_names_is_still_worked_on(open_prs):
    t = _task(BRIEF, {"his": "fix the CT failure on PR 777", "offer": ""})
    assert asyncio.run(tasks.pr_head_branch(t, open_prs)) == {"svc-booking": "feature/the-one-he-named"}


def test_a_pr_the_colleague_named_counts_when_his_word_was_yes(open_prs):
    t = _task(BRIEF, {"his": "yes", "offer": "Priya Nair: please update PR 777 with the new field"})
    assert asyncio.run(tasks.pr_head_branch(t, open_prs)) == {"svc-booking": "feature/the-one-he-named"}


def test_a_task_from_before_the_record_keeps_the_old_reading(open_prs):
    assert asyncio.run(tasks.pr_head_branch(_task(BRIEF), open_prs)) == {"svc-booking": "fix/old-eta-check"}


def test_spawn_records_his_words_and_the_offer_he_accepted(monkeypatch):
    async def idle(task_id):
        return None
    monkeypatch.setattr(tasks, "_worker", idle)
    monkeypatch.setattr(tasks, "_graph", lambda: types.SimpleNamespace(enabled=lambda: False))
    offers.propose(subject="🛠 Priya Nair asks for a code change", context="Priya Nair: add the flag",
                   question="Plan it?", action="Delegate a CODE task", kind="plan_code",
                   payload={"who": "Priya Nair", "words": "please do it on PR 777"})
    offers.accept()

    async def turn():
        conv = tasks.bind_conversation("c-1")
        said = capabilities.TURN_TEXT.set("yes")
        try:
            return tasks.spawn("Add the flag", "brief naming PR 4242", "code", "ws")
        finally:
            capabilities.TURN_TEXT.reset(said)
            tasks.unbind_conversation(conv)
    t = asyncio.run(turn())
    asked = json.loads(store.kv_get(f"task_asked:{t['id']}"))
    assert asked["his"] == "yes" and "PR 777" in asked["offer"] and "4242" not in json.dumps(asked)


def test_an_old_acceptance_is_not_carried_into_a_later_task():
    store.kv_set(offers.ACCEPTED_KEY, json.dumps({"at": time.time() - 3600, "said": "PR 777"}))
    assert offers.recently_accepted() == ""


# --- 2. a plain-language ask is covered by the task already on it --------------

ASK = "Disable the tracker calls and vessel ETA validation for one-way empty or grey pool orders"


def _code_task(title, prompt, status="shipped"):
    t = store.create_task(title, "code", prompt, "ws")
    store.update_task(t["id"], status=status)
    return t


def test_the_same_ask_in_other_words_is_the_running_task():
    t = _code_task("Skip tracker call + vessel ETA validation for one-way empty/grey pool orders",
                   "Priya Nair (testing) asked: skip the tracker call for one-way empty or grey pool orders")
    got = tasks.covering_task(ASK, who="Priya Nair", ask=ASK)
    assert got and got["id"] == t["id"]


@pytest.mark.parametrize("who, title", [
    ("Dev Rao", "Skip tracker call + vessel ETA validation for one-way empty/grey pool orders"),
    ("Priya Nair", "Add retention to the topic config for the archive service"),
])
def test_another_persons_or_another_subject_is_not_covered(who, title):
    _code_task(title, f"Priya Nair asked: {title}")
    assert tasks.covering_task(ASK, who=who, ask=ASK) is None


def test_a_covered_ask_is_not_offered_again(monkeypatch):
    _code_task("Skip tracker call + vessel ETA validation for one-way empty/grey pool orders",
               "Priya Nair asked: skip the tracker call for one-way empty or grey pool orders")
    pings = []

    async def fake(text, *a, **k):
        pings.append(text)
    from app import notify
    monkeypatch.setattr(notify, "notify", fake)
    monkeypatch.setattr(answers, "origin_allowed", lambda *a, **k: True)
    shown = asyncio.run(answers.offer_plan(who="Priya Nair", chat="Priya Nair", need=ASK,
                                           summary="", thread="", words=ASK))
    assert shown is False and not offers.pending()
    assert all("asks for a code change" not in p for p in pings)


def test_identifier_matching_is_unchanged():
    t = _code_task("Carry isCustomsCleared into the event", "map IS_CUSTOMS_CLEARED and customsStatus")
    assert tasks.covering_task("set isCustomsCleared from customsStatus")["id"] == t["id"]


# --- 3. "521 approve" is a task command whichever way round -----------------------

@pytest.mark.parametrize("text, verb, tid", [
    ("approve 12", "approve", "12"), ("12 approve", "approve", "12"), ("#12 approve", "approve", "12"),
    ("task 12 reject", "reject", "12"), ("12, stop", "stop", "12"), ("ok approve", "approve", None),
])
def test_a_task_command_reads_with_the_id_on_either_side(text, verb, tid):
    from app import main
    m = main._TASK_CMD.match(text)
    assert m and m.group(1).lower() == verb and m.group(2) == tid


@pytest.mark.parametrize("text", ["12 approvers are needed", "approve the draft to Priya",
                                  "I approve of this"])
def test_ordinary_sentences_are_not_task_commands(text):
    from app import main
    assert not main._TASK_CMD.match(text)


# --- 4. a red quality gate is said, not planned around ----------------------------

GATE = "From cicd / Build (end of its log):\n  \x1b[31m✖ Quality Gate has FAILED.\x1b[0m"


def test_a_quality_gate_alone_is_recognised():
    assert tasks.quality_gate_only(GATE)
    assert not tasks.quality_gate_only(GATE + "\n  Tests run: 12, Failures: 1, Errors: 0")
    assert not tasks.quality_gate_only("  [ERROR] BUILD FAILURE")


def _red_task(monkeypatch, ci_read):
    t = store.create_task("Skip tracker call", "code", "p", "ws")
    store.update_task(t["id"], status="pr_ci_failed")

    async def checkout(task_id):
        return None

    async def read(task, feedback):
        return ci_read
    monkeypatch.setattr(tasks, "ensure_checkout", checkout)
    monkeypatch.setattr(tasks, "_ci_failure_for", read)
    return t


def test_the_automatic_round_does_not_propose_a_change_for_a_gate(monkeypatch):
    t = _red_task(monkeypatch, GATE)
    with pytest.raises(tasks.QualityGateOnly):
        asyncio.run(tasks.propose_change(t["id"], tasks.CI_FIX_BRIEF))
    assert store.get_task(t["id"])["status"] == "pr_ci_failed", "nothing started, nothing parked"


def test_when_he_asks_to_fix_it_the_worker_is_told_it_is_the_gate(monkeypatch):
    t = _red_task(monkeypatch, GATE)
    seen = {}

    async def worker(task_id, prompt):
        seen["prompt"] = prompt
    monkeypatch.setattr(tasks, "_propose_worker", worker)

    async def go():
        await tasks.propose_change(t["id"], "fix the ci on #%d" % t["id"])
        await asyncio.sleep(0)
    asyncio.run(go())
    assert "QUALITY GATE (SonarQube)" in seen["prompt"] and "tests for the new code" in seen["prompt"]


# --- 5. a chat window that moved on is not a new ask ------------------------------

WINDOW_1 = """Recent exchange (untrusted chat data):
Arun: call ?
Arun: i have one doubt
Priya Nair: No bro
Priya Nair: Not in call
Priya Nair: https://example.test/booking/XX1TEST Booking :: Solar [image: /tmp/a.png]"""
WINDOW_2 = WINDOW_1.split("\n", 2)[2] + "\nPriya Nair: https://example.test/stream/rec.mp4 Recording"


def test_the_askers_own_words_survive_the_window_moving():
    one, two = responder.their_words("Priya Nair", WINDOW_1), responder.their_words("Priya Nair", WINDOW_2)
    assert one == two == "No bro\nNot in call"
    assert "call ?" not in one, "his own lines are not their ask"


def test_a_new_question_is_a_new_ask():
    later = WINDOW_2 + "\nPriya Nair: can you check why XX1TEST failed in sit?"
    assert responder.their_words("Priya Nair", later) != responder.their_words("Priya Nair", WINDOW_2)


def test_one_tasks_answer_is_shown_once_per_chat():
    assert not answers.already_shown(9, "Priya Nair")
    store.kv_set(answers._shown_key(9, "Priya Nair"), str(time.time()))
    assert answers.already_shown(9, "Priya Nair") and not answers.already_shown(9, "Dev Rao")
    store.kv_set(answers._shown_key(9, "Priya Nair"), str(time.time() - answers.REPEAT_SECONDS - 1))
    assert not answers.already_shown(9, "Priya Nair")
