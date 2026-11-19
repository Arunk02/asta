"""Every code change waits for his approval unless his own words said go (16 Nov).

His words: "make plan approval mandatory for all kind of coding tasks / bug
fixes … if I gave add a change and fix and raise PR then won't need my approval
— that kind of flexible I need."
"""

import asyncio
import json

import pytest

from app import go, notify, reproduction, store, tasks


@pytest.mark.parametrize("text,pr,ahead", [
    ("fix the null check and raise the PR", True, False),
    ("add the field, fix it and raise PR", True, False),
    ("should I raise the PR?", False, False),
    ("fix it, I'll raise the PR myself", False, False),
    ("fix it but don't raise the PR yet", False, False),
    ("just do it", False, True),
    ("go ahead and fix it, no need to show me the plan", False, True),
    ("fix the null check", False, False),
    ("can you just do it?", False, False),
])
def test_his_words_decide(text, pr, ahead):
    assert go.wants_pr(text) is pr and go.go_ahead(text) is ahead


SMALL = "STRUCTURE\n  EquipmentMapper.java  maps size\n\nPLAN READY"


def _task(prompt="fix the mapper"):
    return store.create_task("Fix the mapper", "code", prompt, None)["id"]


def test_a_one_file_fix_waits():
    assert go.no_ask(_task(), SMALL) == ""


def test_his_go_skips_the_wait():
    tid = _task()
    go.on_spawn(tid, "fix the mapper and raise the PR")
    assert go.no_ask(tid, SMALL) == "as you said" and go.ships(tid)


def test_just_do_it_builds_and_stays_local():
    tid = _task()
    go.on_spawn(tid, "fix the mapper, just do it")
    assert go.no_ask(tid, SMALL) == "as you said" and not go.ships(tid)


def test_a_colleague_s_ask_never_counts():
    tid = _task("Pat asked: fix the mapper and raise the PR")
    assert go.no_ask(tid, SMALL) == "", "only his own words grant a go (on_spawn)"


@pytest.mark.parametrize("plan,why", [
    ("STRUCTURE\n  A.java\n\nOpen questions: keep the old field?\n\nPLAN READY", "asks you"),
    ("STRUCTURE\n" + "\n".join(f"  C{i}.java  x" for i in range(12)) + "\n\nPLAN READY", "big or risky"),
    ("STRUCTURE\n  BookingWorkflowImpl.java  add Workflow.getVersion\n\nPLAN READY", "big or risky"),
])
def test_his_go_goes_ahead_and_says_the_question_or_risk(plan, why):
    """19 Nov, his rule: approval given up front = no stop at the plan gate. What
    used to stop it (questions, size, risk words) rides along as a heads-up."""
    tid = _task()
    go.on_spawn(tid, "fix the mapper and raise the PR")
    out = go.no_ask(tid, plan)
    assert out.startswith("as you said — heads-up:")
    assert why.split()[-1] in out
    assert go.stop_reason(tid) == ""


def test_his_go_on_a_plan_about_another_ticket_goes_ahead_with_a_heads_up():
    tid = store.create_task("ABC-12 fix the mapper", "code", "ABC-12: fix the mapper", None)["id"]
    go.on_spawn(tid, "ABC-12 fix the mapper and raise the PR")
    assert "ABC-99" in go.no_ask(tid, "ABC-99: change the router\n\nPLAN READY")
    assert go.no_ask(tid, "ABC-12: map the size\n\nPLAN READY") == "as you said"


def test_his_go_waits_until_the_bug_is_reproduced():
    tid = _task()
    reproduction.request(tid, "replicate the NPE, fix it and raise the PR")
    go.on_spawn(tid, "replicate the NPE, fix it and raise the PR")
    assert go.no_ask(tid, SMALL) == ""
    saved = reproduction.state(tid)
    store.kv_set(f"reproduction:{tid}", json.dumps({**saved, "state": "confirmed"}))
    assert go.no_ask(tid, SMALL) == "as you said"


def test_the_card_says_why_a_go_ahead_still_waits(monkeypatch):
    pushed = []

    async def push(text, *a, **k):
        pushed.append(text)

    async def doc(*a, **k):
        return True
    monkeypatch.setattr(notify, "notify", push)
    monkeypatch.setattr(notify, "wa_document", doc)
    tid = _task()
    go.on_spawn(tid, "share the plan with a colleague, get an approval and raise the PR")
    asyncio.run(tasks.announce_plan(tid, store.get_task(tid), SMALL))
    assert ("You said go ahead — this one waits for you: "
            "you asked for someone else's approval first") in pushed[-1]
    assert f"approve task {tid}" in pushed[-1]
