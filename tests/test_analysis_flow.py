"""Bug analysis should work like a senior engineer, not a log reader.

When a CI pipeline goes red, "yes" to "analyse the failure?" used to fire a prompt
that said only "pull the log, find the cause, report the smallest fix". That skips
everything that makes an analysis trustworthy: reading the actual code, judging it
against how this codebase works, and — before claiming a cause — reproducing it.

These pin the workflow the analysis now instructs: use project context, trace the
root cause in the CODE, reproduce with a failing test before believing it, then
fix behind a green test and raise the PR. And they pin that the analysis runs in
the failing repo's workspace, so "project context" is a real thing it can reach.
"""

from __future__ import annotations

import time

from app import main as main_mod, offers


def _analyse_offer(payload=None):
    return offers.Offer(id="x", kind="analyse", subject="🔴 CI failure: telikos-booking-service",
                        context="https://github.com/…/runs/123", prompt="Want me to analyse the failure?",
                        created=time.time(), payload=payload or {})


def test_analysis_traces_root_cause_in_the_code_not_the_log():
    p = main_mod._offer_prompt(_analyse_offer()).lower()
    assert "root cause" in p
    assert "code" in p and "log" in p            # reads code, not just the log
    assert "symptom" in p                        # explicitly: cause, not symptom


def test_analysis_reproduces_before_it_believes_itself():
    """The 'mock up to replicate, confirm the bug' step — a claimed cause with no
    repro is a guess."""
    p = main_mod._offer_prompt(_analyse_offer()).lower()
    assert "reproduc" in p
    assert "failing test" in p or "mock" in p
    assert "say so" in p                         # it tells Arun it's setting up a repro


def test_analysis_judges_against_this_codebase():
    p = main_mod._offer_prompt(_analyse_offer()).lower()
    assert "this codebase" in p or "conventions" in p or "context" in p


def test_analysis_does_not_touch_production_code_yet():
    p = main_mod._offer_prompt(_analyse_offer()).lower()
    assert "do not change" in p or "not change production" in p
    assert "raise the pr" in p                   # ends by offering the next step


def test_the_fix_step_puts_a_failing_test_before_the_fix():
    o = offers.Offer(id="y", kind="raise_pr", subject="fix it", context="ctx",
                     prompt="fix and PR?", created=time.time())
    p = main_mod._offer_prompt(o).lower()
    assert "fails for the right reason" in p or "reproduces the bug" in p
    assert "suite is still green" in p or "wider suite" in p
    assert "personal account" in p               # standing rule: his personal GH


# --- the analysis runs where the code is -------------------------------------

def test_a_ci_offer_resolves_the_repos_workspace(monkeypatch):
    """So "yes" analyses in that project — with its context and code — instead of
    guessing from Asta's own repo."""
    import app.ci_watch as ci
    note = "🔴 CI failure: telikos-booking-service · Build (main) — boom\nhttps://x/runs/1"
    monkeypatch.setattr("app.workspace.infer", lambda text="", **k: "booking")
    assert ci._analyse_payload(note) == {"workspace": "booking"}


def test_an_unresolvable_repo_leaves_the_brain_to_figure_it_out(monkeypatch):
    """Ambiguous → None, not a wrong guess: analysing against the wrong repo is
    worse than the brain resolving it from the note itself."""
    import app.ci_watch as ci
    monkeypatch.setattr("app.workspace.infer", lambda text="", **k: None)
    assert ci._analyse_payload("🔴 CI failure: unknown-service · Build") is None
