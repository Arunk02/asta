"""Round 5, PR C: a follow-up on approved work is an amendment to that plan, not
a new plan; and a follow-up about the task that just finished is placed on it.

25 Nov, his words: "it's not understanding which is a reply and which is the
actual task" and "every time it re-plans even for minor improvements — the first
plan is better". Task 744 that day: one plan, then four Opus re-plans, each
replacing the plan he had approved."""
from __future__ import annotations

import asyncio
import json

import pytest

from app import store, tasks

PLAN = ("STRUCTURE\n  RailEtaValidator.validate()  new check, mirrors VesselEtaValidator\n"
        "  CamsRetryChildWorkflowImpl  calls it before the retry\nRISK: none")


def _task(status="shipped", plan=PLAN):
    t = store.create_task("ABC-77 CAMS rail ETA validation mirrors VTS vessel ETA",
                          "code", "ABC-77 add rail ETA validation like the vessel one", None)
    store.update_task(t["id"], status=status)
    if plan:
        store.kv_set(f"task_plan:{t['id']}", plan)
    return t["id"]


# --- same subject -------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "fix the null check in RailEtaValidator",
    "also cover the CamsRetryChildWorkflowImpl path with a test",
    "ABC-77 add the component test as well",
    "the rail validation should skip cancelled legs",
])
def test_a_message_about_the_task_is_on_its_subject(text):
    assert tasks.same_subject(_task(), text)


@pytest.mark.parametrize("text", [
    "add a retry to the email sender",
    "ABC-91 bump the kafka client version",
    "fix the flaky readyForPlanning test in AP",
])
def test_other_work_is_not(text):
    assert not tasks.same_subject(_task(), text)


# --- amendments ---------------------------------------------------------------------

def _propose(monkeypatch, tid, answer):
    seen = {}

    async def leg(task_id, prompt, cwd, **kw):
        seen["prompt"] = prompt
        seen["amending"] = tasks.amending(task_id)
        return answer

    async def nothing(*a, **k):
        return ""

    async def pushed(text, *a, **k):
        seen["told"] = text

    from app import notify
    monkeypatch.setattr(tasks, "_run_code_leg", leg)
    monkeypatch.setattr(tasks, "ensure_checkout", nothing)
    monkeypatch.setattr(tasks, "_ci_failure_for", nothing)
    monkeypatch.setattr(tasks, "task_cwd", lambda *a, **k: "/tmp")
    monkeypatch.setattr(notify, "notify", pushed)

    async def go():
        await tasks.propose_change(tid, "include the component test as well")
        await asyncio.gather(*[j for j in list(tasks._running.values())], return_exceptions=True)
    asyncio.run(go())
    return seen


def test_a_follow_up_on_approved_work_asks_for_an_amendment(monkeypatch):
    tid = _task()
    seen = _propose(monkeypatch, tid, "AMENDMENT:\n+ RailEtaComponentTest → covers late ETA\nRISK: none")
    assert "AMENDMENT ONLY" in seen["prompt"] and "RailEtaValidator" in seen["prompt"]
    assert seen["amending"] is True
    assert not tasks.amending(tid)                       # cleared after the leg
    assert "amendment A1 to the plan you approved" in seen["told"]


def test_approving_an_amendment_keeps_the_plan_and_appends_it(monkeypatch):
    tid = _task()
    _propose(monkeypatch, tid, "AMENDMENT:\n+ RailEtaComponentTest → covers late ETA")
    brief, back = tasks._apply_proposal(tid)
    plan = store.kv_get(f"task_plan:{tid}")
    assert plan.startswith(PLAN)                         # v1 stands
    assert "AMENDMENT A1" in plan and "RailEtaComponentTest" in plan
    assert "amendment A1" in brief and back == "shipped"
    store.update_task(tid, status=back)                  # what reply() does after approval
    # A second one is A2, on top of both.
    _propose(monkeypatch, tid, "AMENDMENT:\n~ RailEtaValidator → skip cancelled legs")
    tasks._apply_proposal(tid)
    plan = store.kv_get(f"task_plan:{tid}")
    assert plan.startswith(PLAN) and "AMENDMENT A1" in plan and "AMENDMENT A2" in plan


def test_a_changed_design_replaces_the_plan_and_says_so(monkeypatch):
    tid = _task()
    new = "DESIGN CHANGED: the check belongs in the activity, not the workflow\nSTRUCTURE\n  RailEtaActivityImpl"
    seen = _propose(monkeypatch, tid, new)
    assert "changes the approved design" in seen["told"]
    tasks._apply_proposal(tid)
    assert store.kv_get(f"task_plan:{tid}").startswith("DESIGN CHANGED")


def test_without_an_approved_plan_it_is_a_plan_as_before(monkeypatch):
    tid = _task(plan="")
    seen = _propose(monkeypatch, tid, "PLAN: ...")
    assert "AMENDMENT ONLY" not in seen["prompt"] and seen["amending"] is False
    assert json.loads(store.kv_get(f"task_proposal:{tid}"))["amend"] is False


def test_the_switch_turns_amendments_off(monkeypatch):
    monkeypatch.setenv("ASTA_PLAN_AMEND", "0")
    tid = _task()
    seen = _propose(monkeypatch, tid, "PLAN: ...")
    assert "AMENDMENT ONLY" not in seen["prompt"]


def test_an_amendment_leg_runs_on_the_build_model(monkeypatch):
    from app import routing
    monkeypatch.setenv("ASTA_ROUTING", "1")
    tid = _task()
    routing.set_tier(tid, routing.T3, "test", "signals")
    store.kv_set(f"task_amending:{tid}", "1")
    seen = {}

    async def cli(task_id, executor, prompt, **kw):
        seen.update(kw)
        return "ok"
    monkeypatch.setattr(tasks, "_observed_cli", cli)
    monkeypatch.setattr(tasks, "task_tools", lambda *a, **k: "")
    monkeypatch.setattr(tasks, "leg_executor", lambda task_id: "claude")
    asyncio.run(tasks._code_leg(tid, "amend", "/tmp", resume=False, effort="medium"))
    planned = routing.choose(tid, "claude", "plan").model
    built = routing.choose(tid, "claude", "implement").model
    assert seen["model"] == built and built != planned
    assert seen["plan_only"] is True                      # still cannot write
