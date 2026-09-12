"""The front desk and model routing — Astra-class P3.

The WorkWorld set `frontdesk.yaml` proves the behaviour end to end; these pin
the pieces: which words map to which tier, which messages are answered from
state and which are not, how a message binds to a job, and the event log every
status change now lands in.
"""

from __future__ import annotations

import asyncio

import pytest

from app import claude_cli, copilot_cli, frontdesk, routing, scorecard, store, tasks

# The real functions, before conftest's autouse guard swaps them for a refusal.
_REAL_CLAUDE = claude_cli.one_shot
_REAL_COPILOT = copilot_cli.one_shot


# --- tiers from words ------------------------------------------------------------------

@pytest.mark.parametrize("title,prompt,tier", [
    ("fix the typo in the README heading", "fix the typo", routing.T1),
    ("bump the retention constant", "bump RETENTION_DAYS to 90", routing.T1),
    ("BEPTELIKOS-10159 ETA validation", "implement the ticket", routing.T2),
    ("add the priority field to the Avro schema", "and the consumers", routing.T3),
    ("prod is down for bookings", "find out why", routing.T3),
])
def test_cheap_signals_pick_a_starting_tier(title, prompt, tier):
    assert routing.pre_tier(title, prompt)[0] == tier


def test_the_plan_decides_the_implementation_tier():
    small = "STRUCTURE\n  README.md    heading fixed\n\nPLAN READY"
    three = ("STRUCTURE\n  EtaValidator   rejects late ETA\n    └─ BookingService.apply()  calls it\n"
             "  BookingService  wires it\n  EtaValidatorTest  +3 cases\n\nPLAN READY")
    assert routing.plan_tier(small)[0] == routing.T1
    assert routing.plan_tier(three) == (routing.T2, "3 classes/files")   # └─ belongs to its parent
    assert routing.plan_tier(small, {"repos": ["a", "b"]})[0] == routing.T3
    assert routing.plan_tier("STRUCTURE\n  X   y\n\nRISK: high\nPLAN READY")[0] == routing.T3


@pytest.mark.parametrize("text,want", [
    ("fix it, use claude", {"brain": "claude"}),
    ("do it cheap", {"tier": routing.T1}),
    ("go max on this one", {"tier": routing.T3}),
    ("rework the retry — max", {"tier": routing.T3}),
    ("raise max retries to 5", {}),              # a config value, not a request for T3
    ("set the max pool size", {}),
])
def test_his_words_override(text, want):
    assert routing.overrides(text) == want


def test_plans_run_at_t2_and_builds_at_the_tier(monkeypatch):
    t = store.create_task("x", "code", "p", None)
    routing.set_tier(t["id"], routing.T1, "small", "signals")
    assert routing.choose(t["id"], "claude", "plan") == routing.Choice(
        routing.T2, "sonnet", "medium", "every plan runs at T2")
    assert routing.choose(t["id"], "claude", "implement").effort == "low"
    assert routing.choose(t["id"], "copilot", "implement").model == ""   # account-specific list
    monkeypatch.setenv("ASTA_ROUTE_CLAUDE_T1", "haiku:low")
    assert routing.choose(t["id"], "claude", "implement").model == "haiku"


def test_escalation_is_one_tier_and_stops_at_the_top():
    t = store.create_task("x", "code", "p", None)
    routing.set_tier(t["id"], routing.T2, "ticket", "signals")
    assert routing.escalate(t["id"], "same failure") == routing.T3
    assert routing.escalate(t["id"], "same failure") == routing.T3


def test_a_pinned_tier_survives_the_plan():
    t = store.create_task("x", "code", "p", None)
    routing.on_spawn(t["id"], "fix it", "fix the mapper, max")
    routing.on_plan(t["id"], "STRUCTURE\n  README.md  one line\n\nPLAN READY")
    assert routing.tier_of(t["id"]) == routing.T3


def test_every_decision_is_on_the_timeline():
    t = store.create_task("x", "code", "p", None)
    routing.on_spawn(t["id"], "fix the typo", "fix the typo")
    routing.record(t["id"], "implement", "claude",
                   routing.choose(t["id"], "claude", "implement"))
    kinds = [e["kind"] for e in store.task_events(t["id"])]
    assert kinds == ["created", "tier", "route"]


# --- the model reaches the CLI ---------------------------------------------------------

class _Captured(Exception):
    pass


def _argv(monkeypatch, cli, real, **kw) -> list[str]:
    seen: dict = {}

    async def _exec(*cmd, **_k):
        seen["cmd"] = list(cmd)
        raise _Captured()

    monkeypatch.setattr(cli, "available", lambda: True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    with pytest.raises(_Captured):
        asyncio.run(real("implement it", **kw))
    return seen["cmd"]


def test_a_leg_model_reaches_claude_and_wins_over_the_standing_tier(monkeypatch):
    from app import agent
    agent.set_tier("claude_cli", "sonnet")
    cmd = _argv(monkeypatch, claude_cli, _REAL_CLAUDE, model="opus")
    assert cmd[cmd.index("--model") + 1] == "opus"


def test_a_leg_model_reaches_copilot(monkeypatch):
    cmd = _argv(monkeypatch, copilot_cli, _REAL_COPILOT, model="gpt-5")
    assert cmd[cmd.index("--model") + 1] == "gpt-5"
    assert "--model" not in _argv(monkeypatch, copilot_cli, _REAL_COPILOT)


def test_routing_off_leaves_a_leg_untouched(monkeypatch, tmp_path):
    seen: dict = {}

    async def one_shot(prompt, **kw):
        seen.update(kw)
        return "PLAN READY"

    monkeypatch.setattr(tasks.claude_cli, "one_shot", one_shot)
    monkeypatch.setattr(tasks, "_resolve_executor", lambda tid: "claude")
    monkeypatch.setattr(tasks, "task_tools", lambda *a, **k: "")
    t = store.create_task("x", "code", "p", None)
    asyncio.run(tasks._run_code_leg(t["id"], "p", str(tmp_path), resume=False, effort="medium"))
    assert seen["model"] == "" and seen["effort"] == "medium"


# --- answered from state -----------------------------------------------------------------

def _waiting(title="mapping change", gate="plan"):
    t = store.create_task(title, "code", "p", None)
    store.update_task(t["id"], status="awaiting_approval")
    store.kv_set(f"task_gate:{t['id']}", gate)
    return t["id"]


@pytest.mark.parametrize("said", [
    "status of {id}", "what's the status of task {id}", "how's #{id} going", "{id} status",
    "where is {id}?", "any update on {id}",
])
def test_a_named_task_status_is_its_card(said):
    tid = _waiting()
    got = frontdesk.answer_from_state(said.format(id=tid))
    assert f"#{tid} mapping change" in got and "approve the plan" in got


@pytest.mark.parametrize("said", [
    "what is the timeout in prod", "status of the booking consumer in prod",
    "raise max retries to 5", "implement the status endpoint", "yes",
    "how do we handle 404s",
])
def test_everything_else_is_not_answered_from_state(said):
    _waiting()
    assert frontdesk.answer_from_state(said) == ""


def test_open_prs_and_running_work_are_read_from_the_table():
    t = store.create_task("ship the mapper", "code", "p", None)
    store.update_task(t["id"], status="shipped", pr_urls="https://github.com/x/y/pull/9",
                      pr_state="OPEN")
    assert "pull/9" in frontdesk.answer_from_state("any PR blocked?")
    _waiting("plan for acl")
    assert "plan for acl" in frontdesk.answer_from_state("what's running")


def test_a_card_shows_the_timeline():
    tid = _waiting()
    store.add_task_event(tid, "gate", "answer: leave the PDF side")
    card = frontdesk.task_card(tid)
    assert "status: running → awaiting_approval" in card
    assert "gate: answer: leave the PDF side" in card


# --- binding ------------------------------------------------------------------------------

@pytest.mark.parametrize("text,named,want", [
    ("check the consumer lag on the AP side", False, "ambiguous"),   # never folded on a guess
    ("check the consumer lag on the AP side", True, "augment"),      # he named the job
    ("also cover the amend path", False, "augment"),                 # the rule says addition
    ("stop, wrong repo", False, "redirect"),
    ("what's failing?", True, "independent"),                        # a question, even if named
])
def test_binding_is_by_name_or_rule(text, named, want):
    assert frontdesk.interjection(text, named) == want


# --- the event log --------------------------------------------------------------------------

def test_every_status_change_lands_on_the_timeline_once():
    t = store.create_task("x", "code", "p", None)                    # starts running
    store.update_task(t["id"], status="running", result="partial")    # no change: no event
    store.update_task(t["id"], status="awaiting_approval")
    store.update_task(t["id"], result="edited")                       # no status: no event
    store.update_task(t["id"], status="done")
    assert [e["detail"] for e in store.task_events(t["id"])] == [
        "code", "running → awaiting_approval", "awaiting_approval → done"]


def test_the_timeline_has_an_endpoint():
    from fastapi.testclient import TestClient
    from app import main
    t = store.create_task("x", "code", "p", None)
    main.app.dependency_overrides[main.require_auth] = lambda: None
    try:
        body = TestClient(main.app).get(f"/api/tasks/{t['id']}/events").json()
    finally:
        main.app.dependency_overrides.clear()
    assert body["events"][0]["kind"] == "created"


# --- the scorecard reads it -------------------------------------------------------------------

def test_the_scorecard_counts_messages_that_needed_no_brain():
    for route in ("state", "command", "brain", "brain"):
        frontdesk.record(route)
    rows = {r["key"]: r for r in scorecard.compute()["rows"]}
    assert rows["no_brain"]["value"] == 0.5
    assert "task_tiers" in rows
