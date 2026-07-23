"""The compounding loop: runs become skills, and escalations teach the cheap tier."""

from __future__ import annotations

import asyncio
import json

import pytest

from app import learn, skills, store


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(learn, "USAGE_FILE", tmp_path / "skills" / ".usage.json")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init()


GOOD = {
    "title": "Rebuild generated mappers before running tests",
    "when": "Tests fail with missing generated mapper classes after changing a DTO.",
    "procedure": ["Run the clean build target", "Re-run the scoped tests"],
    "pitfalls": ["An incremental build reuses stale generated sources"],
    "verification": ["The previously failing test compiles and passes"],
    "tags": ["build", "codegen"],
    "confidence": 0.9,
}


# --- what is worth learning from ---------------------------------------------

@pytest.mark.parametrize("rounds,escalated,status,expected", [
    (1, False, "done", False),    # one-shot run taught nothing
    (2, False, "done", True),     # needed several rounds
    (1, True, "done", True),      # escalated — the lesson is exactly here
    (5, False, "failed", False),  # a failure has no verified procedure to record
    (3, False, "rejected", False),
])
def test_should_extract(rounds, escalated, status, expected):
    assert learn.should_extract(rounds, escalated, status) is expected


# --- writing -----------------------------------------------------------------

def test_write_skill_produces_a_loadable_skill():
    path = learn.write_skill(GOOD)
    assert path is not None
    found = {s["name"]: s for s in skills.discover()}
    name = learn._slug(GOOD["title"])
    assert name in found
    assert "Procedure" in found[name]["body"]
    assert skills.load(name)


def test_low_confidence_is_discarded():
    """A wrong procedure gets followed confidently — worse than none at all."""
    assert learn.write_skill({**GOOD, "confidence": 0.4}) is None
    assert skills.discover() == []


def test_a_skill_with_no_procedure_is_discarded():
    assert learn.write_skill({**GOOD, "procedure": []}) is None


def test_rewriting_the_same_skill_replaces_it():
    """Two procedures for one situation is how a memory starts contradicting itself."""
    learn.write_skill(GOOD)
    learn.write_skill({**GOOD, "procedure": ["A different step"]})
    matching = [s for s in skills.discover() if s["name"] == learn._slug(GOOD["title"])]
    assert len(matching) == 1
    assert "A different step" in matching[0]["body"]


def test_written_skill_records_its_confidence_and_source():
    learn.write_skill(GOOD, source="teacher")
    entry = learn.stats()[learn._slug(GOOD["title"])]
    assert entry["confidence"] == pytest.approx(0.9)
    assert entry["source"] == "teacher"


# --- extraction --------------------------------------------------------------

def test_extract_parses_json_from_a_chatty_model(monkeypatch):
    async def fake(prompt):
        return "Sure! Here you go:\n```json\n" + json.dumps(GOOD) + "\n```"
    monkeypatch.setattr(learn, "_distil", fake)
    path = asyncio.run(learn.extract("some task", "transcript"))
    assert path is not None


def test_extract_survives_a_broken_model(monkeypatch):
    """Learning is a side effect of finishing work — it must never fail the work."""
    async def boom(prompt):
        raise RuntimeError("model down")
    monkeypatch.setattr(learn, "_distil", boom)
    assert asyncio.run(learn.extract("t", "x")) is None


def test_extract_ignores_unparseable_output(monkeypatch):
    monkeypatch.setattr(learn, "_distil", lambda p: _async("no json here"))
    assert asyncio.run(learn.extract("t", "x")) is None


def _async(value):
    async def inner():
        return value
    return inner()


def test_escalated_run_is_marked_as_taught_by_the_teacher(monkeypatch):
    captured = {}

    async def fake(prompt):
        captured["prompt"] = prompt
        return json.dumps(GOOD)
    monkeypatch.setattr(learn, "_distil", fake)
    path = asyncio.run(learn.extract("t", "x", escalated=True))
    assert path is not None
    assert "ESCALATED" in captured["prompt"], "the teacher must know why it is writing"
    assert learn.stats()[learn._slug(GOOD["title"])]["source"] == "teacher"
    assert "source: teacher" in path.read_text()


def test_extraction_is_recorded_as_an_outcome(monkeypatch):
    monkeypatch.setattr(learn, "_distil", lambda p: _async(json.dumps(GOOD)))
    asyncio.run(learn.extract("t", "x"))
    assert any(r["kind"] == "skill" for r in store.recent_outcomes())


# --- usage and pruning --------------------------------------------------------

def test_loading_a_skill_counts_as_use(monkeypatch):
    learn.write_skill(GOOD)
    name = learn._slug(GOOD["title"])
    from app import agent
    agent.load_skill(name)
    assert learn.stats()[name]["uses"] == 1


def test_prune_drops_only_old_unused_low_confidence_skills():
    learn.write_skill({**GOOD, "confidence": 0.65})
    name = learn._slug(GOOD["title"])
    usage = learn._usage()
    usage[name]["created"] = 0.0          # long ago
    learn._save_usage(usage)
    assert learn.prune() == [name]
    assert skills.discover() == []


def test_prune_keeps_a_used_skill():
    learn.write_skill({**GOOD, "confidence": 0.65})
    name = learn._slug(GOOD["title"])
    learn.record_use(name)
    usage = learn._usage()
    usage[name]["created"] = 0.0
    learn._save_usage(usage)
    assert learn.prune() == []


def test_prune_keeps_a_confident_skill():
    learn.write_skill(GOOD)          # confidence 0.9
    usage = learn._usage()
    usage[learn._slug(GOOD["title"])]["created"] = 0.0
    learn._save_usage(usage)
    assert learn.prune() == []
