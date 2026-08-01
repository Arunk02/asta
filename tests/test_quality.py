"""Self-evaluation: 'is Asta getting better' has to be a number, not a feeling."""

from __future__ import annotations

import time

import pytest

from app import quality, store


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init()


def test_empty_says_so_rather_than_claiming_success():
    text = quality.report()
    assert "No outcomes" in text


def test_success_rate_is_computed_per_kind():
    for outcome in ("approved", "approved", "approved", "replanned"):
        store.record_outcome("plan", outcome)
    data = quality.summary()
    assert data["kinds"]["plan"]["total"] == 4
    assert data["kinds"]["plan"]["rate"] == 0.75


def test_report_reads_as_evidence():
    store.record_outcome("task", "done")
    store.record_outcome("task", "failed")
    store.record_outcome("ship", "pr_opened")
    text = quality.report()
    assert "50% tasks finished" in text
    assert "Shipping" in text


def test_verify_rate_measures_passing_its_own_check():
    """The un-fakeable signal: 'good' is a passing check, and a stuck run that
    parked unresolved counts against the rate — not per-round fix telemetry."""
    for outcome in ("passed", "passed", "passed", "unresolved"):
        store.record_outcome("verify", outcome)
    data = quality.summary()
    assert data["kinds"]["verify"]["rate"] == 0.75
    assert "75% passed their own check" in quality.report()


def test_verify_convergence_measures_how_hard_green_was():
    """The learning number: avg fix-rounds to green, and how many passed first try."""
    store.record_outcome("verify", "passed", detail="fix_rounds=0 cmd=pytest")
    store.record_outcome("verify", "passed", detail="fix_rounds=0 cmd=pytest")
    store.record_outcome("verify", "passed", detail="fix_rounds=2 cmd=pytest")
    conv = quality.verify_convergence()
    assert conv == {"passed": 3, "avg_fix_rounds": round(2 / 3, 2), "first_try": 2}
    assert "fix-round(s) to green, 2/3 first-try" in quality.report()


def test_verify_convergence_empty_when_nothing_passed():
    store.record_outcome("verify", "unresolved", detail="stuck")
    assert quality.verify_convergence() == {}


def test_old_outcomes_fall_outside_the_window():
    store.record_outcome("task", "done")
    with store._connect() as conn:
        conn.execute("UPDATE outcomes SET created_at=?", (time.time() - 40 * 86400,))
    assert quality.summary(days=7)["kinds"] == {}
    assert quality.summary(days=90)["kinds"]["task"]["total"] == 1


def test_kinds_without_a_defined_good_outcome_still_report():
    store.record_outcome("skill", "written")
    text = quality.report()
    assert "Learning" in text
    assert "rate" not in quality.summary()["kinds"]["skill"]


def test_recording_never_raises_on_a_pre_outcomes_database(tmp_path, monkeypatch):
    """Measurement must never break the thing being measured."""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "bare.db")
    with store._connect() as conn:
        conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    store.record_outcome("task", "done")   # must not raise
