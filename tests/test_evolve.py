"""Getting better on its own — Astra-class P6.

The loop only earns its place if it cannot do harm: a change is measured
against the version running now, the constitution is checked rather than
trusted, nothing is promoted without a gain, and everything can be taken back.
"""

from __future__ import annotations

import asyncio

import pytest

from app import evolve, settings, store


def test_only_the_listed_knobs_can_move_and_only_inside_their_bounds():
    with pytest.raises(ValueError):
        settings.set_override("ASTA_PUSH_BUDGET", 5)          # his number, not Asta's
    with pytest.raises(ValueError):
        settings.set_override("ASTA_COALESCE_SECONDS", 5000)  # outside the bound
    assert settings.set_override("ASTA_COALESCE_SECONDS", 180)
    assert settings.value("ASTA_COALESCE_SECONDS") == 180


def test_his_own_setting_beats_the_default_and_an_override_beats_both(monkeypatch):
    assert settings.value("ASTA_RESPOND_MAX_PER_HOUR") == 4
    monkeypatch.setenv("ASTA_RESPOND_MAX_PER_HOUR", "6")
    assert settings.value("ASTA_RESPOND_MAX_PER_HOUR") == 6
    settings.set_override("ASTA_RESPOND_MAX_PER_HOUR", 2)
    assert settings.value("ASTA_RESPOND_MAX_PER_HOUR") == 2


def test_a_tuned_knob_actually_reaches_the_code_that_reads_it():
    from app import delivery
    settings.set_override("ASTA_COALESCE_SECONDS", 300)
    assert delivery.coalesce_seconds() == 300


@pytest.mark.parametrize("day,expected", [
    ({"pushes": 29, "missed": 0, "false_interrupts": 8}, "interrupted for noise"),
    ({"pushes": 40, "missed": 0, "false_interrupts": 0}, "too many interruptions"),
    ({"pushes": 10, "missed": 3, "false_interrupts": 0}, "missed something he needed"),
])
def test_it_names_the_problem_before_proposing_anything(day, expected):
    assert expected in evolve.diagnose({"day": day, "outcomes": {}})


def test_a_quiet_day_proposes_nothing():
    assert evolve.diagnose({"day": {"pushes": 12, "missed": 0, "false_interrupts": 0},
                            "outcomes": {}}) == []


def test_a_miss_and_a_noisy_day_pull_the_same_knob_opposite_ways():
    noisy = evolve.propose(["most of what it pushes is ignored"])[0]
    missed = evolve.propose(["missed something he needed"])[0]
    assert noisy.knob == missed.knob == "ASTA_ATTENTION_IGNORE_SHARE"
    assert noisy.after < settings.value(noisy.knob) < missed.after


def _measure(rate, pushes, missed, constitution=(5, 5)):
    total, passed = constitution
    return {"pass_rate": rate, "day": {"pushes": pushes, "missed": missed},
            "sets": {"constitution": {"total": total, "passed": passed}}}


def _proves(monkeypatch, before, after):
    calls = {"n": 0}

    async def measure(k=1):
        calls["n"] += 1
        return before if calls["n"] == 1 else after

    monkeypatch.setattr(evolve, "measure", measure)


def test_a_promoted_change_is_in_force_for_the_next_measurement(monkeypatch):
    """A tuned knob lives in the database and the bench runs in a sandbox with
    one of its own, so a promotion was invisible to every later measurement —
    tomorrow's baseline measured an Asta nobody was running."""
    seen = {}

    async def run_all(k=1, **kw):
        seen["share"] = __import__("os").environ.get("ASTA_ATTENTION_IGNORE_SHARE")
        return []

    async def day_run(**kw):
        class R:
            events = pushes = missed = false_interrupts = []
            investigations = brain_calls = 0
            seconds = 0.0
        return R()

    from app.workworld import day as day_mod, runner
    monkeypatch.setattr(runner, "run_all", run_all)
    monkeypatch.setattr(day_mod, "run", day_run)
    settings.set_override("ASTA_ATTENTION_IGNORE_SHARE", 0.7)
    asyncio.run(evolve.measure())
    assert seen["share"] == "0.7"


def test_a_candidate_is_measured_against_the_version_running_now(monkeypatch):
    c = evolve.Candidate("L1", "ASTA_COALESCE_SECONDS", 180, "too many interruptions", "carry more")
    _proves(monkeypatch, _measure(1.0, 20, 0), _measure(1.0, 14, 0))
    out = asyncio.run(evolve.prove(c))
    assert out["ok"] and "pushes 20→14" in out["gain"]
    # Measuring does not change anything: promotion is its own decision.
    assert settings.override("ASTA_COALESCE_SECONDS") == ""


def test_a_candidate_that_does_not_win_is_rejected_and_the_lesson_kept(monkeypatch):
    c = evolve.Candidate("L1", "ASTA_COALESCE_SECONDS", 180, "too many interruptions", "carry more")
    _proves(monkeypatch, _measure(1.0, 14, 0), _measure(1.0, 16, 0))
    out = asyncio.run(evolve.prove(c))
    assert not out["ok"]
    evolve.record(c, "rejected", out["gain"])
    assert evolve.history()[0]["state"] == "rejected"
    assert "pushes 14→16" in evolve.history()[0]["gain"]


def test_a_candidate_that_breaks_the_constitution_is_refused_however_good(monkeypatch):
    c = evolve.Candidate("L1", "ASTA_COALESCE_SECONDS", 180, "too many interruptions", "carry more")
    _proves(monkeypatch, _measure(0.9, 20, 0), _measure(1.0, 8, 0, constitution=(5, 4)))
    out = asyncio.run(evolve.prove(c))
    assert not out["ok"] and not out["constitution_ok"]


def test_a_candidate_that_starts_missing_things_is_refused(monkeypatch):
    c = evolve.Candidate("L1", "ASTA_ATTENTION_IGNORE_SHARE", 0.7, "ignored", "demote sooner")
    _proves(monkeypatch, _measure(1.0, 20, 0), _measure(1.0, 9, 2))
    assert not asyncio.run(evolve.prove(c))["ok"]


def test_promote_then_roll_back_puts_everything_as_it_was():
    c = evolve.Candidate("L1", "ASTA_COALESCE_SECONDS", 180, "too many interruptions", "carry more")
    eid = evolve.promote(c, "pushes 20→14")
    assert settings.value("ASTA_COALESCE_SECONDS") == 180
    assert store.kv_get(f"evolve_canary:{eid}")              # the day it has to survive
    assert "back to 120" in evolve.rollback(eid, "a drill")
    assert settings.value("ASTA_COALESCE_SECONDS") == 120 and settings.override("ASTA_COALESCE_SECONDS") == ""
    assert evolve.history()[0]["state"] == "rolled_back"
    assert evolve.rollback(eid) == ""                        # and only once


def test_the_nightly_pass_promotes_a_winner_and_says_so(monkeypatch):
    monkeypatch.setenv("ASTA_EVOLVE", "1")
    monkeypatch.setattr(evolve, "observe", lambda: {
        "day": {"pushes": 29, "missed": 0, "false_interrupts": 0}, "outcomes": {}})
    _proves(monkeypatch, _measure(1.0, 29, 0), _measure(1.0, 18, 0))
    out = asyncio.run(evolve.nightly())
    # The knob that actually moves the number it is unhappy about: batching was
    # proposed first once, and cannot help — the day's events are spread far
    # wider than any coalescing window (measured: 15 pushes at 30 s and at 600 s).
    assert out["promoted"] and "ASTA_ATTENTION_IGNORE_SHARE" in out["change"]
    assert settings.overrides()                              # it is live now
    assert "promoted" in evolve.summary()


def test_nothing_happens_while_it_is_switched_off():
    assert asyncio.run(evolve.nightly()) == {"ran": False, "why": "ASTA_EVOLVE is off"}


def test_a_code_fix_is_only_ever_proposed_and_never_without_the_flag(monkeypatch):
    assert evolve.propose_code_fix("checks fail more often", "evidence") == ""
    monkeypatch.setenv("ASTA_EVOLVE_L3", "1")
    spawned = {}

    def spawn(title, prompt, kind="analysis", workspace=None, **kw):
        spawned.update(title=title, prompt=prompt, kind=kind)
        return {"id": 7}

    from app import tasks
    monkeypatch.setattr(tasks, "spawn", spawn)
    out = evolve.propose_code_fix("checks fail more often", "12 rounds red")
    assert "#7" in out and "plan gate" in out
    assert spawned["kind"] == "code"
    assert "FAILS for this reason first" in spawned["prompt"]
    assert "constitution" in spawned["prompt"]


def test_what_it_changed_is_readable_and_reversible_from_the_front_desk(monkeypatch):
    from app import frontdesk
    c = evolve.Candidate("L1", "ASTA_COALESCE_SECONDS", 180, "too many interruptions", "carry more")
    eid = evolve.promote(c, "pushes 20→14")
    said = frontdesk.answer_from_state("what have you changed")
    assert "ASTA_COALESCE_SECONDS 120.0→180" in said and "180" in said
    assert "Rolled back" in frontdesk.answer_from_state(f"roll back {eid}")
    assert settings.overrides() == {}


def test_it_runs_on_the_night_not_on_the_live_benchs_flag(monkeypatch):
    """Switched on for the first time, P6 would never have run: its gate was the
    LIVE bench's, which is off, and proving a candidate costs no brain at all."""
    import datetime as dt
    from app.workworld import nightly
    monkeypatch.delenv("ASTA_BENCH_NIGHTLY", raising=False)
    night = dt.datetime(2026, 9, 17, 3, 0)
    assert "ASTA_BENCH_NIGHTLY" in nightly.why_not(night)      # the live tier stays off
    assert nightly.quiet_window(night) == ""                   # the free work may run
    assert "outside the window" in nightly.quiet_window(dt.datetime(2026, 9, 17, 12, 0))
    store.kv_set("last_user_message_at", str(__import__("time").time()))
    assert "was working" in nightly.quiet_window(night)
