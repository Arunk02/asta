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
