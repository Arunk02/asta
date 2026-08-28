"""The scoreboard itself. If this is wrong, every number it reports is wrong.

The one that matters most is the partial-credit test: a binary score has no
gradient, and without a gradient nothing downstream can hill-climb. That is the
difference between a regression gate (which `evals` already is) and a reward.
"""

from __future__ import annotations

import json

import pytest

from app import bench, store


def _case(**kw):
    base = {"id": "c1", "capability": "triage", "must": [], "budget": {}}
    return {**base, **kw}


# --- correctness --------------------------------------------------------------

def test_partial_credit_gives_the_optimiser_a_slope():
    case = _case(must=["alpha", "beta", "gamma", "delta"])
    none, _ = bench.correctness("nothing here", case)
    half, _ = bench.correctness("alpha and beta", case)
    most, _ = bench.correctness("alpha beta gamma", case)
    assert none == 0.0
    assert half == pytest.approx(0.5)
    assert most == pytest.approx(0.75)
    # The whole point: three distinct values where `evals.grade` reports False
    # three times. A change that fixes two of four must be visible as progress.
    assert none < half < most


def test_saying_something_forbidden_is_not_partial_success():
    """A must_not exists because that answer is actively harmful, not merely
    incomplete — it cannot be offset by getting other facts right."""
    case = _case(must=["alpha"], must_not=["deleted the branch"])
    score, _ = bench.correctness("alpha, and I deleted the branch", case)
    assert score == 0.0


def test_an_empty_answer_scores_zero_not_full_marks():
    """A case with no `must` and no answer must not read as a pass — that is how
    a dead runner scores 1.0 and hides itself."""
    score, _ = bench.correctness("", _case())
    assert score == 0.0


def test_any_of_counts_once_and_accepts_either_wording():
    case = _case(must=[], any_of=[["verify", "confirm"]])
    a, _ = bench.correctness("verify the label first", case)
    b, _ = bench.correctness("confirm the label first", case)
    assert a == b == 1.0


# --- the reward ---------------------------------------------------------------

def test_a_fast_cheap_wrong_answer_still_scores_badly():
    """Any weighting that lets speed rescue a wrong answer is lying."""
    out = bench.score(_case(must=["alpha"], budget={"seconds": 10, "tokens": 100}),
                      {"text": "beta", "seconds": 0.0, "tokens": 0})
    assert out["speed"] == 1.0 and out["thrift"] == 1.0
    assert out["reward"] <= 1 - bench.W_CORRECT + 1e-9


def test_a_safety_violation_caps_the_reward_it_does_not_merely_dent_it():
    perfect = {"text": "alpha", "seconds": 0.0, "tokens": 0}
    clean = bench.score(_case(must=["alpha"]), perfect)
    dirty = bench.score(_case(must=["alpha"]), {**perfect, "violations": ["called someone"]})
    assert clean["reward"] > 0.9
    assert dirty["reward"] <= bench.SAFETY_CAP
    assert dirty["ok"] is False


def test_being_inside_the_budget_is_not_a_race():
    """Flat up to the budget, so nothing learns to chase 200ms it already had."""
    case = _case(must=["a"], budget={"seconds": 2.0})
    quick = bench.score(case, {"text": "a", "seconds": 0.1, "tokens": 0})
    nearly = bench.score(case, {"text": "a", "seconds": 1.9, "tokens": 0})
    over = bench.score(case, {"text": "a", "seconds": 4.0, "tokens": 0})
    assert quick["speed"] == nearly["speed"] == 1.0
    assert over["speed"] == pytest.approx(0.5)


def test_no_budget_declared_is_not_scored_as_a_failure():
    out = bench.score(_case(must=["a"]), {"text": "a", "seconds": 3.0, "tokens": 900})
    assert out["speed"] == 1.0 and out["thrift"] == 1.0


# --- loading and running ------------------------------------------------------

def test_a_real_scenario_shadows_its_anonymised_twin(tmp_path, monkeypatch):
    """The committed starter set exists so CI has something to run. When he adds
    the real, grounded version it must win — not run twice."""
    real, starter = tmp_path / "real", tmp_path / "starter"
    real.mkdir(), starter.mkdir()
    (real / "triage.json").write_text(json.dumps(
        {"capability": "triage", "cases": [{"id": "shared", "must": ["real"]}]}))
    (starter / "triage.json").write_text(json.dumps(
        {"capability": "triage", "cases": [{"id": "shared", "must": ["anon"]},
                                           {"id": "extra", "must": ["x"]}]}))
    monkeypatch.setattr(bench, "SCENARIOS_DIR", real)
    monkeypatch.setattr(bench, "STARTER_DIR", starter)
    cases = bench.load()
    assert len(cases) == 2
    assert next(c for c in cases if c["id"] == "shared")["must"] == ["real"]


@pytest.mark.asyncio
async def test_an_unknown_capability_is_an_error_not_a_silent_zero(tmp_path, monkeypatch):
    """A typo in a suite must be visible. Scored as 0.0 it looks like a real
    regression and sends him hunting for a bug that is not there."""
    d = tmp_path / "s"
    d.mkdir()
    (d / "nope.json").write_text(json.dumps(
        {"capability": "teleportation", "cases": [{"id": "t", "must": ["x"]}]}))
    monkeypatch.setattr(bench, "SCENARIOS_DIR", d)
    monkeypatch.setattr(bench, "STARTER_DIR", tmp_path / "missing")
    out = await bench.run()
    assert "no runner" in out["results"][0]["error"]


@pytest.mark.asyncio
async def test_a_runner_that_explodes_names_itself_instead_of_killing_the_run():
    from app import runners
    original = runners.RUNNERS.get("triage")

    async def broken(case):
        raise ZeroDivisionError("boom")

    runners.RUNNERS["triage"] = broken
    try:
        out = await bench.run("triage")
    finally:
        if original is not None:
            runners.RUNNERS["triage"] = original
    assert out["total"] > 0
    assert all("ZeroDivisionError" in r["error"] for r in out["results"])


@pytest.mark.asyncio
async def test_live_cases_cost_nothing_unless_asked_for(tmp_path, monkeypatch):
    d = tmp_path / "s"
    d.mkdir()
    (d / "triage.json").write_text(json.dumps({"capability": "triage", "cases": [
        {"id": "free", "given": {"subject": "can you approve?"}, "must": ["action=True"]},
        {"id": "paid", "live": True, "given": {"subject": "can you approve?"},
         "must": ["action=True"]},
    ]}))
    monkeypatch.setattr(bench, "SCENARIOS_DIR", d)
    monkeypatch.setattr(bench, "STARTER_DIR", tmp_path / "missing")
    out = await bench.run()
    assert out["total"] == 1 and out["skipped_live"] == 1


@pytest.mark.asyncio
async def test_a_bench_run_does_not_write_to_his_database():
    """The recovery ladder writes cooldown keys; the ledger writes rows. Run
    against the live store, a bench would change the next run's result."""
    before = store.DB_PATH
    seen: list = []

    from app import runners
    original = runners.RUNNERS.get("triage")

    async def peek(case):
        seen.append(store.DB_PATH)
        return {"text": "action=True"}

    runners.RUNNERS["triage"] = peek
    try:
        await bench.run("triage")
    finally:
        if original is not None:
            runners.RUNNERS["triage"] = original
    assert seen and all(p != before for p in seen), "scenarios ran against the real DB"
    assert store.DB_PATH == before, "the bench did not put the DB path back"


# --- the report ---------------------------------------------------------------

def test_no_scenarios_is_not_reported_as_a_score_of_zero():
    text = bench.report({"total": 0, "passed": 0, "reward": 0.0, "seconds": 0.0,
                         "tokens": 0, "capabilities": {}, "results": []})
    assert "0%" not in text and "data/scenarios" in text


def test_the_report_names_the_ground_truth_of_every_failure():
    out = {"total": 1, "passed": 0, "reward": 0.1, "seconds": 0.1, "tokens": 0,
           "capabilities": {"triage": {"n": 1, "passed": 0, "reward": 0.1,
                                       "correctness": 0.0, "seconds": 0.1, "tokens": 0}},
           "results": [bench.score(_case(must=["alpha"], source="lessons.md vessel-dates"),
                                   {"text": "beta", "seconds": 0.0, "tokens": 0})]}
    text = bench.report(out)
    assert "never mentioned alpha" in text
    assert "lessons.md vessel-dates" in text
