"""The measure→improve loop: recurring token waste must turn into a durable skill,
once, safely. End-to-end — real write_skill, real kv dedupe, and the task-finish hook.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app import learn, skill_evolution, skills, store, token_audit


@pytest.fixture
def sk(monkeypatch):
    d = Path(tempfile.mkdtemp()) / "skills"
    d.mkdir()
    monkeypatch.setattr(skills, "SKILLS_DIR", d)
    monkeypatch.setattr(learn, "USAGE_FILE", d / ".usage.json")
    monkeypatch.setattr(store, "DB_PATH", d / "t.db")
    store.init()
    return d


def _hist(*cats: str) -> list[dict]:
    return [{"top_fix": c, "waste_ratio": 0.3} for c in cats]


# --- recurrence gate --------------------------------------------------------

def test_one_run_is_noise_two_is_a_pattern():
    assert skill_evolution.recurring(_hist("full_reads")) == []
    assert skill_evolution.recurring(_hist("full_reads", "full_reads")) == ["full_reads"]


def test_none_and_unknown_categories_never_recur():
    assert skill_evolution.recurring(_hist("none", "none")) == []
    assert skill_evolution.recurring(_hist("not_a_category", "not_a_category")) == []


# --- evolve writes a real, durable skill ------------------------------------

def test_recurring_waste_becomes_a_skill(sk):
    evolved = skill_evolution.evolve(_hist("full_reads", "full_reads"))
    assert len(evolved) == 1 and evolved[0]["category"] == "full_reads"
    written = list(sk.glob("*.md"))
    assert len(written) == 1
    body = written[0].read_text()
    assert "source: evolution" in body and "resolve" in body.lower()


def test_evolve_fires_once_per_category(sk):
    first = skill_evolution.evolve(_hist("fat_outputs", "fat_outputs"))
    second = skill_evolution.evolve(_hist("fat_outputs", "fat_outputs"))
    assert len(first) == 1
    assert second == []                         # deduped in kv — no re-announce, no churn


def test_a_one_off_never_evolves(sk):
    assert skill_evolution.evolve(_hist("excess_greps")) == []
    assert list(sk.glob("*.md")) == []


# --- guards that keep it honest ---------------------------------------------

def _categories_the_auditor_can_emit() -> set[str]:
    """Derive real categories straight from detect_waste, so a rename there fails
    this test instead of silently orphaning a FIX_MAP entry."""
    n = token_audit._norm
    records = [
        {**n(), "reads": [("/a", False)]},                                   # full_reads
        {**n(), "reads": [("/a", True), ("/a", True)]},                      # duplicate_reads
        {**n(), "calls": 5, "results": {"t": 9000}, "result_turn": {"t": 1}},  # fat_outputs
        {**n(), "bash": ["grep x"] * 11},                                    # excess_greps
        {**n(), "text_blocks": [4000, 4000]},                                # narration
        {**n(), "executor": "claude", "new_tokens": 100000, "out_tokens": 100},  # replan_recache
    ]
    cats: set[str] = set()
    for r in records:
        cats |= set(token_audit.detect_waste(r))
    return cats


def test_every_fix_maps_to_a_real_auditor_category():
    assert set(skill_evolution.FIX_MAP) <= _categories_the_auditor_can_emit()


def test_every_fix_clears_write_skill_bar(sk):
    """A malformed lesson would silently no-op (write_skill returns None). Guard it."""
    for cat, data in skill_evolution.FIX_MAP.items():
        assert learn.write_skill(data, source="evolution") is not None, cat


# --- the task-finish hook actually drives it --------------------------------

def test_task_finish_evolves_on_recurring_waste(sk, monkeypatch):
    from app import tasks
    monkeypatch.setattr(token_audit, "audit_task", lambda tid: {
        "waste_ratio": 0.3, "avoidable_tokens": 9000, "top_fix": "full_reads",
        "grade": "D (wasteful)"})
    monkeypatch.setattr(token_audit, "trend_series",
                        lambda: _hist("full_reads", "full_reads"))
    note = tasks._audit_note(1)
    assert "token audit" in note
    assert "🧬 learned a skill" in note          # the loop closed, end to end
