"""Does the waste number mean anything?

Arun asked directly: "it told token wastage 7 percent — does it really improve
and make it better?" It did not, and the tests below pin why.

Reading the first 13 real snapshots off his machine:

  - every single run scored between 0.0% and 6.8%, so all 13 fell in grade A or
    B. The C (>12%) and D (>25%) bands have never once fired. The letter was
    effectively a constant;
  - the 7% he was shown was, in fact, among the WORST runs ever recorded — and
    it was labelled "grade B (ok)";
  - the series looked like it was improving (median 4.8% → 3.4%) and was not:
    the first half was Claude and unlabelled runs, the second half was entirely
    Copilot. The two halves share no executor at all. The denominators differ
    too — Claude reports real cache-write tokens, Copilot is a proxy — so the
    comparison was never valid.

So a run is now graded against the same brain's own history, and a trend is
claimed only when there is enough of one to claim.
"""

from __future__ import annotations

import pytest

from app import token_audit as ta


def _hist(*pairs):
    """(executor, ratio) → history rows."""
    return [{"at": i, "executor": ex, "waste_ratio": r, "calls": 40,
             "avoidable": 1000, "top_fix": "excess_greps"}
            for i, (ex, r) in enumerate(pairs)]


#: His actual first 13, copied off the machine — the data the old grade was
#: silently wrong about.
REAL = _hist(("claude", 0.029), ("claude", 0.043), ("claude", 0.053),
             ("copilot", 0.000), ("copilot", 0.017), ("copilot", 0.034),
             ("copilot", 0.034), ("copilot", 0.040), ("copilot", 0.060),
             ("copilot", 0.068))


def test_the_old_bands_never_fire_on_real_data():
    """Which is what made 'grade B (ok)' meaningless."""
    grades = {ta._grade(r["waste_ratio"]) for r in REAL}
    assert grades <= {"A (lean)", "B (ok)"}
    assert not any(g.startswith(("C", "D")) for g in grades)


def test_the_seven_percent_he_saw_is_now_called_worse_not_ok():
    """The exact reported case."""
    out = ta.verdict(0.07, "copilot", REAL)
    assert "WORSE" in out
    assert "grade B" not in out
    assert "ok" not in out.lower()


def test_a_genuinely_good_run_is_called_better():
    assert "better than" in ta.verdict(0.01, "copilot", REAL)


def test_a_typical_run_is_called_typical():
    med, _ = ta.baseline("copilot", REAL)
    assert "typical" in ta.verdict(med, "copilot", REAL)


def test_no_claim_is_made_without_enough_history():
    """Four runs is not a baseline, and saying 'better than usual' off it is noise."""
    out = ta.verdict(0.07, "claude", REAL)          # only 3 claude rows
    assert "no baseline yet" in out
    # No comparative claim — the word "better" appears only in the explanation
    # of what is missing, never as a verdict about this run.
    assert "better than" not in out and "WORSE than" not in out


def test_an_unseen_executor_says_so_rather_than_guessing():
    out = ta.verdict(0.05, "gemini", REAL)
    assert "0 previous gemini" in out


def test_the_baseline_is_per_executor_not_pooled():
    """Pooling is what let a change of brain read as a change in behaviour."""
    cop, n_cop = ta.baseline("copilot", REAL)
    cla, n_cla = ta.baseline("claude", REAL)
    assert n_cop == 7 and n_cla == 3
    assert cop != cla


def test_the_apparent_improvement_in_the_real_series_is_not_claimed():
    """Overall median fell 4.8% → 3.4% purely because the executor changed."""
    out = ta.trend_verdict(REAL)
    assert "too few to say" in out
    assert "improving" not in out


def test_a_real_within_executor_improvement_is_reported():
    """When the evidence IS there, it should say so."""
    history = _hist(*([("copilot", 0.09)] * 5 + [("copilot", 0.02)] * 5))
    assert "improving" in ta.trend_verdict(history)


def test_a_real_regression_is_reported():
    history = _hist(*([("copilot", 0.02)] * 5 + [("copilot", 0.09)] * 5))
    assert "getting worse" in ta.trend_verdict(history)


def test_a_flat_series_is_called_flat():
    history = _hist(*([("copilot", 0.04)] * 10))
    assert "flat" in ta.trend_verdict(history)


def test_two_executors_are_reported_separately():
    history = _hist(*([("copilot", 0.09)] * 5 + [("copilot", 0.02)] * 5
                      + [("claude", 0.05)] * 10))
    out = ta.trend_verdict(history)
    assert "copilot:" in out and "claude:" in out


def test_an_empty_history_claims_nothing():
    assert "not enough runs" in ta.trend_verdict([])
    assert ta.baseline("copilot", []) == (0.0, 0)


def test_rows_without_an_executor_do_not_pollute_a_baseline():
    """Three of his real snapshots predate the field entirely."""
    history = REAL + [{"at": 99, "waste_ratio": 0.9}]       # no executor key
    med, n = ta.baseline("copilot", history)
    assert n == 7, "an unlabelled row was counted as this executor's"


def test_a_run_is_not_measured_against_a_bar_it_helped_set(monkeypatch):
    """audit_task must grade BEFORE appending, or every run flatters itself."""
    monkeypatch.setattr(ta, "_session_file", lambda tid: "fake")
    monkeypatch.setattr(ta, "audit_session", lambda p: {
        "waste_ratio": 0.07, "executor": "copilot", "calls": 40,
        "avoidable_tokens": 5000, "top_fix": "excess_greps", "grade": "B (ok)"})
    monkeypatch.setattr(ta, "_history", lambda: list(REAL))

    saved = {}
    monkeypatch.setattr(ta.store, "kv_set", lambda k, v: saved.update({k: v}))

    rep = ta.audit_task(64)
    assert "WORSE" in rep["verdict"]
    assert rep["baseline_runs"] == 7, "graded against a history including itself"
