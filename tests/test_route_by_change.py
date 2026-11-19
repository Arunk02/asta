"""19 Nov: a two-line helm change ran on opus twice — "high impact (billing)"
because the list it edited was named …_TO_BILLING, and "2 repos" made it big.
His table since: size from what the change touches, haiku for small work,
sonnet high for builds, opus only to plan the big or risky, and booking
debugging on sonnet at medium. Synthetic repos, tasks and words throughout.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import routing, store, tasks


@pytest.fixture(autouse=True)
def _routing_on(monkeypatch):
    monkeypatch.setenv("ASTA_ROUTING", "1")

HELM_PLAN = """I need one answer first: which environments?

STRUCTURE
```
svc-a
  helm/dev-values.yml        LIST_OF_ENABLED_COUNTRIES +XX
svc-b
  helm/dev-values.yml        LIST_OF_COUNTRIES_TO_BILLING +XX
```

FLOW
```
svc-a helm (dev)            XX added            <- change
CountryValidator (svc-b)    routes XX, unchanged
billing                     unchanged
```

- **Question:** dev only, or also sit and prod?
RISK: Low. Config only.

PLAN READY
"""


# --- the plan's size, from what it changes ------------------------------------------

def test_a_helm_value_in_two_repos_is_t1_config_whatever_the_list_is_called():
    size = routing.plan_size(HELM_PLAN, {"repos": ["svc-a", "svc-b"]})
    assert (size.tier, size.flavor) == (routing.T1, "config")
    assert "billing" not in size.why


def test_code_on_a_risky_path_is_a_t3_modification():
    plan = "STRUCTURE\n  src/main/java/x/billing/RateMapper.java   adds a field\n\nPLAN READY"
    assert routing.plan_size(plan) == routing.Size(routing.T3, routing.plan_size(plan).why,
                                                   "modification")


def test_code_in_two_repos_or_over_ten_files_or_a_contract_is_big():
    two = "STRUCTURE\n  src/A.java  x\n\nPLAN READY"
    assert routing.plan_size(two, {"repos": ["a", "b"]}).flavor == "big"
    many = "STRUCTURE\n" + "\n".join(f"  src/C{i}.java  x" for i in range(12)) + "\n\nPLAN READY"
    assert routing.plan_size(many).flavor == "big"
    avro = "STRUCTURE\n  src/main/avro/Event.avsc   new field\n\nPLAN READY"
    assert routing.plan_size(avro).tier == routing.T3


def test_config_beside_code_counts_as_part_of_the_change():
    plan = ("STRUCTURE\n  src/A.java  x\n  src/B.java  y\n  helm/values.yml  z\n\nPLAN READY")
    assert routing.plan_size(plan).tier == routing.T2


def test_untouched_lines_and_prose_do_not_count():
    plan = ("STRUCTURE\n  helm/values.yml   +XX\n  OrderWorkflow   unchanged\n\n"
            "No schema change.\n\nPLAN READY")
    assert routing.plan_size(plan).flavor == "config"


# --- before a plan: his words, not the brief -----------------------------------------

@pytest.mark.parametrize("his,tier,flavor", [
    ("enable XX as S4 country in svc-a and svc-b on develop and raise PR", routing.T1, "config"),
    ("update helm prod values for the topic and raise PR", routing.T1, "config"),
    ("add subtract(a, b) to calc.py with a test", routing.T1, ""),
    ("fix the NPE in OrderMapper.java:120", routing.T1, ""),
    ("go ahead and see and fix it, prove it before fix", routing.T2, ""),
    ("implement ABC-123 in svc-a", routing.T2, ""),
    ("add the priority field to the Avro schema", routing.T3, "big"),
])
def test_his_words_size_the_plan_leg(his, tier, flavor):
    size = routing.pre_size("a long brief a brain wrote", "x" * 400, his)
    assert (size.tier, size.flavor) == (tier, flavor), size


def test_without_his_words_the_brief_still_decides():
    assert routing.pre_tier("fix the typo in the README heading", "fix the typo")[0] == routing.T1


def test_the_spawn_tier_reads_his_turn(monkeypatch):
    from app import capabilities
    t = store.create_task("Enable XX — a long brief", "code", "y" * 500, None)
    token = capabilities.TURN_TEXT.set("enable XX as S4 country and raise PR")
    try:
        routing.on_spawn(t["id"], t["title"], t["prompt"])
    finally:
        capabilities.TURN_TEXT.reset(token)
    assert (routing.tier_of(t["id"]), routing.flavor_of(t["id"])) == (routing.T1, "config")
    plan = routing.choose(t["id"], "claude", "plan")
    assert (plan.model, plan.effort) == ("haiku", "low")


def test_pi_never_builds_on_its_old_haiku():
    t = store.create_task("x", "code", "p", None)
    routing.set_tier(t["id"], routing.T1, "1 file", "signals")
    assert routing.choose(t["id"], "pi", "implement").model == "sonnet"


# --- after the build: the real change against the tier --------------------------------

def test_a_build_bigger_than_its_tier_moves_the_fix_legs_up_and_is_counted_once():
    t = store.create_task("x", "code", "p", None)
    routing.set_tier(t["id"], routing.T1, "1 file", "plan")
    assert routing.fit(t["id"], {"code": 8, "test": 9, "config": 0}, 741) == "under"
    assert routing.tier_of(t["id"]) == routing.T2
    assert routing.choose(t["id"], "claude", "implement").model == "sonnet"
    assert routing.fit(t["id"], {"code": 8}, 741) == "under"     # same answer, not recounted
    rows = [r for r in store.list_outcomes(kind="route_fit")] if hasattr(store, "list_outcomes") else None
    if rows is not None:
        assert len([r for r in rows if r.get("subject") == str(t["id"])]) == 1


def test_a_tier_he_pinned_is_not_moved():
    t = store.create_task("x", "code", "p", None)
    routing.set_tier(t["id"], routing.T1, "you asked for it", "override")
    store.kv_set(f"task_tier_pinned:{t['id']}", "1")
    assert routing.fit(t["id"], {"code": 12}, 900) == "under"
    assert routing.tier_of(t["id"]) == routing.T1


def test_a_config_build_on_t1_is_a_hit():
    t = store.create_task("x", "code", "p", None)
    routing.set_tier(t["id"], routing.T1, "a config value", "signals", "config")
    assert routing.fit(t["id"], {"config": 2}, 4) == "hit"


def test_route_fit_measures_the_task_diff_by_kind(monkeypatch, tmp_path):
    t = store.create_task("x", "code", "p", "ws")
    routing.set_tier(t["id"], routing.T1, "1 file", "plan")
    repo = tmp_path / "svc-a"
    repo.mkdir()
    monkeypatch.setattr(tasks, "task_cwd", lambda tid, ws: str(tmp_path))
    monkeypatch.setattr(tasks, "_repos_under", lambda root: [repo])

    async def base(repo, t, task_id=0):
        return "abc"

    async def git(cwd, *args, **k):
        return 0, ("5\t1\tsrc/main/java/A.java\n3\t0\tsrc/main/java/B.java\n"
                   "40\t0\tsrc/test/java/ATest.java\n1\t1\thelm/values.yml\n")
    monkeypatch.setattr(tasks, "_task_base", base)
    monkeypatch.setattr(tasks.repo_ops, "git", git)
    assert asyncio.run(tasks.route_fit(t["id"], store.get_task(t["id"]))) == "under"


# --- booking debugging ----------------------------------------------------------------

DEBUG = ("For this piece of work you are an expert debugging engineer. Reproduce it first.\n"
         "Booking ABCD1234567 is stuck in svc-a; trace it across services.")


def test_debugging_a_booking_runs_on_sonnet_at_medium():
    c = routing.debug_choice("claude", DEBUG)
    assert (c.model, c.effort) == ("sonnet", "medium")


def test_a_second_pass_on_the_case_thinks_harder():
    c = routing.debug_choice("claude", DEBUG + "\n\n[Prior investigation (task #12) — …]")
    assert (c.model, c.effort) == ("sonnet", "high")


def test_other_analyses_keep_their_settings_and_copilot_keeps_its_model():
    assert routing.debug_choice("claude", "Summarise this mail thread for Arun") is None
    assert routing.debug_choice("copilot", DEBUG).model == ""


def test_the_analysis_leg_gets_the_debug_model(monkeypatch):
    t = store.create_task("Booking stuck", "analysis", DEBUG, None)
    seen = {}

    async def observed(task_id, executor, prompt, **kw):
        seen.update(kw, executor=executor)
        return "done"
    monkeypatch.setattr(tasks, "_observed_cli", observed)
    monkeypatch.setattr(tasks, "_resolve_executor", lambda tid: "claude")
    asyncio.run(tasks._simple_leg(t["id"], store.get_task(t["id"]), DEBUG))
    assert seen["executor"] == "claude" and seen["model"] == "sonnet" and seen["effort"] == "medium"


# --- small things that keep small work small ------------------------------------------

def test_t1_code_tasks_get_no_code_search_servers(monkeypatch):
    from app import dev_mcp
    t = store.create_task("x", "code", "p", None)
    routing.set_tier(t["id"], routing.T1, "1 file", "signals")
    seen = {}
    monkeypatch.setattr(dev_mcp, "config_json", lambda project, base=None: seen.setdefault("p", project) or "")
    tasks.task_tools(t["id"], "/some/checkout", "code")
    assert seen["p"] == ""


def test_micro_does_not_escalate_a_config_value_in_several_repos():
    text = (Path(__file__).resolve().parents[1] / "agents" / "micro.md").read_text()
    assert "is\n  NOT a reason to escalate" in text or "NOT a reason to escalate" in text


def test_the_open_question_of_a_go_ahead_plan_is_found():
    assert tasks.open_question(HELM_PLAN).startswith("Question: dev only")
    assert tasks.open_question("STRUCTURE\n  A.java  x\n\nPLAN READY") == ""


# --- cost tracking is untouched, and now read per tier --------------------------------

def test_a_haiku_leg_still_records_its_cost(monkeypatch):
    from app import claude_cli, usage_week, worker_usage
    t = store.create_task("Enable XX", "code", "p", None)
    with worker_usage.attempt(t["id"], "claude", "/tmp", "sid", stage="implement", model="haiku"):
        claude_cli.LAST_COST.set(0.0123)
    rows = [json.loads(v) for v in store.kv_like(f"worker_usage:{t['id']}:%").values()]
    assert rows[0]["configured_model"] == "haiku" and rows[0]["cost_usd"] == 0.0123
    assert usage_week.task_spend(0)[t["id"]]["usd"] == pytest.approx(0.0123)


def test_the_scorecard_shows_cost_per_finished_task_by_tier(monkeypatch):
    from app import scorecard, worker_usage, claude_cli
    for tier, cost, status in ((routing.T1, 0.04, "shipped"), (routing.T2, 0.5, "merged"),
                               (routing.T1, 9.0, "failed")):
        t = store.create_task("x", "code", "p", None)
        store.update_task(t["id"], status=status)
        routing.set_tier(t["id"], tier, "w", "plan")
        with worker_usage.attempt(t["id"], "claude", "/tmp", "sid", stage="implement"):
            claude_cli.LAST_COST.set(cost)
    out = scorecard.cost_by_tier(0, 10 ** 12)
    assert out["avg"] == {"T1": pytest.approx(0.04), "T2": pytest.approx(0.5)}, "failed tasks excluded"
