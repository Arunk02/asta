"""19 Nov, his rule for code tasks — one approval, never one per stage:

  * approval given up front ("…and raise the PR", "don't need my approval")
    means no stop at the plan gate: it goes all the way to the PR;
  * no approval up front: the plan waits for ONE yes, and that yes ships the PR.

What happened instead: a config-only task waited at the gate despite his go
("high impact (billing)"), his "ship" and "Approve Tak 12" (a typo) at the gate
were read as feedback and re-planned, and the re-plan waited a third time.

Synthetic tasks, repos and words throughout.
"""
from __future__ import annotations

import pytest

from app import go, store, tasks

PLAN = ("STRUCTURE\n  helm/prod-values.yml:78  config.billing.countries gets XX\n\n"
        "FLOW\n  1 env var -> config.billing.countries\n\nPLAN READY")


def _at_gate(gate: str = "plan") -> dict:
    t = store.create_task("Enable XX", "code", "enable XX in svc-a", None)
    store.update_task(t["id"], status="awaiting_approval", result=PLAN)
    store.kv_set(f"task_gate:{t['id']}", gate)
    return store.get_task(t["id"])


@pytest.mark.parametrize("said", [
    "ship", "Ship it", "Approve Tak 12", "approve task 12", "PLAN APPROVED", "lgtm",
    "Raise the PR and share", "Yes promote and raise PR", "yes go ahead",
    "Go ahead and create PR don't need my approval update and raise PR",
    "approve, don't push yet",
])
def test_every_way_he_says_yes_at_the_plan_gate_is_a_yes(said):
    assert tasks.plan_yes(said), said


@pytest.mark.parametrize("said", [
    "change the field name to countryList", "why does it touch the mapper?",
    "no", "wait", "use the other branch instead", "not this repo",
])
def test_a_change_or_a_question_is_not_a_yes(said):
    assert not tasks.plan_yes(said), said


def _his_answer(t: dict, said: str):
    """His answer at the gate, as main hands it on: his yes grants the PR first."""
    if tasks.plan_yes(said):
        go.his_plan_yes(t["id"], said)
    return tasks.record_answer(t["id"], t, said)


def test_ship_at_the_gate_approves_the_plan_once_and_ships_it():
    t = _at_gate()
    approved, text = _his_answer(t, "ship")
    assert approved and text.startswith("PLAN APPROVED")
    assert "ship" not in text.lower().replace("plan approved", ""), "no garbage amendment"
    assert tasks.plan_approved(t["id"]) and go.ships(t["id"])
    events = [e["detail"] for e in store.task_events(t["id"]) if e["kind"] == "gate"]
    assert events and events[-1].startswith("approved:"), "never logged as a review answer"


def test_a_yes_with_real_words_keeps_them_as_the_amendment():
    t = _at_gate()
    _, text = tasks.record_answer(t["id"], t, "approve, also add a unit test for the alias case")
    assert text.startswith("PLAN APPROVED — ") and "alias" in text


def test_a_yes_at_the_context_gate_approves_no_plan():
    t = _at_gate("context")
    approved, _ = tasks.record_answer(t["id"], t, "yes")
    assert not approved and not tasks.plan_approved(t["id"])


def test_his_plan_approval_without_pr_words_still_ships_the_pr():
    t = _at_gate()
    _his_answer(t, "approve task")
    assert go.ships(t["id"])


def test_his_approval_that_keeps_it_local_does_not_ship():
    t = _at_gate()
    _his_answer(t, "approve, don't push yet")
    assert not go.ships(t["id"])


def test_his_go_up_front_skips_the_gate_even_for_a_risky_word():
    t = store.create_task("Enable XX", "code", "enable XX", None)
    go.on_spawn(t["id"], "enable XX in svc-a and raise PR, don't need my approval")
    why = go.no_ask(t["id"], PLAN)
    assert why.startswith("as you said"), why
    assert "billing" in why, "the risk is still said — as a heads-up, not a stop"
    assert go.stop_reason(t["id"]) == ""


def test_his_go_still_waits_when_he_asked_for_someone_elses_approval():
    t = store.create_task("Enable XX", "code", "enable XX", None)
    go.on_spawn(t["id"], "share the plan with a colleague, get an approval and raise PR")
    assert go.no_ask(t["id"], PLAN) == ""
    assert "someone else" in go.stop_reason(t["id"])


def test_without_his_go_the_plan_waits_for_one_yes():
    t = store.create_task("Enable XX", "code", "enable XX", None)
    assert go.no_ask(t["id"], PLAN) == ""


def test_his_yes_through_the_chat_path_ships_the_pr(monkeypatch):
    """main hands his gate answer on: "ship" for the task it names."""
    import asyncio

    from app import main
    from tests.test_say_it_once import _Sink, _conv
    t = _at_gate()
    seen = []
    monkeypatch.setattr(tasks, "reply", lambda tid, text: seen.append((tid, text)) or "ok")
    asyncio.run(main._dispatch(_conv(), f"{t['id']} ship", _Sink(), "whatsapp"))
    assert seen and seen[0][0] == t["id"]
    assert go.ships(t["id"])


def test_asta_approving_on_its_own_does_not_grant_a_pr():
    """Only his words grant the PR; an internal "PLAN APPROVED" does not."""
    t = _at_gate()
    tasks.record_answer(t["id"], t, "PLAN APPROVED")
    assert not go.ships(t["id"])
