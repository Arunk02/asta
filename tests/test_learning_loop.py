"""Does the memory actually get better, or does it just get bigger?

Two things were wrong, and they compounded. Every learning path hung off the end
of a BACKGROUND TASK, so a week of chat, corrections and CI investigations taught
nothing at all. And `uses` — the only score a skill had — counts being LOADED,
which a skill earns by having a matching title, not by being right. A confidently
wrong procedure is loaded constantly, so it scored well and was the one thing
pruning could never touch.

These pin the fix: results attach to the skills that were in play, the pass runs
on the clock rather than on delegation, and housekeeping cannot break the morning.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app import learn, store


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Never touch the real skills directory — pruning DELETES files."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(learn.skills, "SKILLS_DIR", skills_dir, raising=False)
    monkeypatch.setattr(learn, "USAGE_FILE", skills_dir / ".usage.json", raising=False)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db", raising=False)
    store.init()
    yield skills_dir


def _skill(dirpath, name, **entry):
    (dirpath / f"{name}.md").write_text(f"# {name}")
    data = json.loads(learn.USAGE_FILE.read_text()) if learn.USAGE_FILE.exists() else {}
    data[name] = {"uses": 0, "confidence": 1.0, "created": time.time(), **entry}
    learn.USAGE_FILE.write_text(json.dumps(data))


# --- attaching results to what was in play ----------------------------------

def test_a_run_that_worked_credits_what_it_read(_isolated):
    learn.record_use("rebuild-mappers")
    judged = learn.credit("done", since=time.time() - 60)
    assert judged == ["rebuild-mappers"]
    assert learn.stats()["rebuild-mappers"]["helped"] == 1


def test_a_run_that_failed_debits_it(_isolated):
    learn.record_use("rebuild-mappers")
    learn.credit("failed", since=time.time() - 60)
    assert learn.stats()["rebuild-mappers"]["missed"] == 1
    assert "helped" not in learn.stats()["rebuild-mappers"]


def test_a_skill_read_long_before_the_run_gets_no_credit(_isolated):
    """Yesterday's reading must not take the credit for today's result."""
    _skill(_isolated, "old", uses=1, last_used=time.time() - 10 * 86400)
    assert learn.credit("done", since=time.time() - 60) == []


def test_a_skill_read_just_before_the_clock_started_still_counts(_isolated):
    """A worker loads its skills in the first moments of a run; a strict cutoff
    would credit none of them."""
    now = time.time()
    _skill(_isolated, "just-in-time", uses=1, last_used=now - 30)
    assert learn.credit("done", since=now) == ["just-in-time"]


def test_shipped_and_sent_count_as_working(_isolated):
    for status in ("done", "sent", "shipped"):
        learn.record_use(f"s-{status}")
        learn.credit(status, since=time.time() - 60)
        assert learn.stats()[f"s-{status}"].get("helped") == 1


# --- pruning on evidence, not just on age -----------------------------------

def test_a_skill_present_for_repeated_failures_is_dropped(_isolated):
    """The case old pruning could not reach: heavily used, so protected, and
    wrong every time."""
    _skill(_isolated, "bad-advice", uses=20, confidence=0.9, helped=1, missed=4)
    assert "bad-advice" in learn.prune()
    assert not (_isolated / "bad-advice.md").exists()


def test_a_skill_that_earns_its_place_survives(_isolated):
    _skill(_isolated, "good-advice", uses=20, confidence=0.9, helped=6, missed=1)
    assert learn.prune() == []
    assert (_isolated / "good-advice.md").exists()


def test_one_bad_result_is_not_enough_to_condemn_a_skill(_isolated):
    """A task can be doomed for reasons that have nothing to do with what it read."""
    _skill(_isolated, "unlucky", uses=5, confidence=0.9, helped=0, missed=1)
    assert learn.prune() == []


def test_the_old_unused_and_unconfident_rule_still_applies(_isolated):
    _skill(_isolated, "stale", uses=0, confidence=0.5,
           created=time.time() - 60 * 86400)
    assert "stale" in learn.prune()


def test_a_confident_unused_skill_is_left_alone(_isolated):
    _skill(_isolated, "waiting-its-turn", uses=0, confidence=0.9,
           created=time.time() - 60 * 86400)
    assert learn.prune() == []


def test_the_scoreboard_puts_the_worst_first(_isolated):
    _skill(_isolated, "bad", helped=0, missed=3)
    _skill(_isolated, "good", helped=5, missed=0)
    assert [r["name"] for r in learn.scoreboard()][0] == "bad"


# --- the daily pass ---------------------------------------------------------

def test_the_daily_pass_reports_only_when_something_changed(_isolated, monkeypatch):
    monkeypatch.setattr(learn, "prune", lambda: [])
    from app import skill_evolution
    monkeypatch.setattr(skill_evolution, "evolve", lambda: [])
    assert asyncio.run(learn.daily_pass()) == ""


def test_the_daily_pass_says_what_it_learned_and_dropped(_isolated, monkeypatch):
    from app import skill_evolution
    monkeypatch.setattr(skill_evolution, "evolve",
                        lambda: [{"category": "full_reads", "skill": "resolve-first"}])
    monkeypatch.setattr(learn, "prune", lambda: ["bad-advice"])
    line = asyncio.run(learn.daily_pass())
    assert "resolve-first" in line and "bad-advice" in line


def test_the_daily_pass_is_recorded_as_evidence(_isolated, monkeypatch):
    """"Is Asta getting better" should be answerable from the record rather than
    asserted — quality_report reads these rows."""
    from app import skill_evolution
    monkeypatch.setattr(skill_evolution, "evolve", lambda: [{"skill": "resolve-first"}])
    monkeypatch.setattr(learn, "prune", lambda: [])
    asyncio.run(learn.daily_pass())
    rows = store.recent_outcomes()
    assert any(o["kind"] == "learning" and "resolve-first" in o["detail"] for o in rows)


def test_a_broken_half_does_not_take_the_other_half_down(_isolated, monkeypatch):
    """Housekeeping that can break the morning brief is worse than housekeeping
    that quietly skips a day."""
    from app import skill_evolution

    def boom():
        raise RuntimeError("token audit unavailable")

    monkeypatch.setattr(skill_evolution, "evolve", boom)
    monkeypatch.setattr(learn, "prune", lambda: ["bad-advice"])
    assert "bad-advice" in asyncio.run(learn.daily_pass())


def test_the_daily_pass_never_raises(_isolated, monkeypatch):
    from app import skill_evolution

    def boom(*a, **k):
        raise RuntimeError("everything is on fire")

    monkeypatch.setattr(skill_evolution, "evolve", boom)
    monkeypatch.setattr(learn, "prune", boom)
    assert asyncio.run(learn.daily_pass()) == ""
