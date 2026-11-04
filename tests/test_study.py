"""A learning session persists, and an answer is graded against its source as it reads now."""
from __future__ import annotations

import importlib

import pytest

from app import agent, skills, study

DOC = """# Temporal basics
An activity is declared on an interface marked @ActivityInterface.
A workflow must be deterministic; side effects belong in activities.
Retries are configured with a RetryOptions object.
"""


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "temporal.md").write_text(DOC)
    (tmp_path / "outside.md").write_text(DOC)

    class WS:
        path = root
        def exists(self):
            return True
    from app import workspace
    monkeypatch.setattr(workspace, "get", lambda name: WS() if name == "booking" else None)
    return root


def _session(ws):
    s = study.start("Temporal", ["Know how activities are declared", "Know why workflows replay"])
    study.add_exercise(s["id"], 1, "What marks an activity interface?", ["ActivityInterface"],
                       "booking", "docs/temporal.md")
    study.add_exercise(s["id"], 2, "What must a workflow be?", ["deterministic", "activities"],
                       "booking", "docs/temporal.md")
    return s["id"]


def test_a_session_survives_a_restart(ws):
    sid = _session(ws)
    importlib.reload(study)              # a new process reads the same store
    assert study.sessions() == [{"id": sid, "topic": "Temporal", "status": "active"}]
    assert [o["exercises"] for o in study.progress(sid)["objectives"]] == [1, 1]


@pytest.mark.parametrize("terms, path, why", [
    (["CronSchedule"], "docs/temporal.md", "does not contain CronSchedule"),
    (["ActivityInterface"], "../outside.md", "escapes the workspace"),
    (["ActivityInterface"], "docs/missing.md", "Not a file"),
    ([], "docs/temporal.md", "terms a right answer contains"),
])
def test_an_exercise_the_source_does_not_support_is_refused(ws, terms, path, why):
    s = study.start("Temporal", ["x"])
    with pytest.raises(ValueError, match=why):
        study.add_exercise(s["id"], 1, "q?", terms, "booking", path)


def test_an_answer_is_graded_and_every_attempt_kept(ws):
    sid = _session(ws)
    miss = study.answer(sid, 2, "It has to be repeatable")
    assert miss["result"] == "failed" and miss["missing"] == ["deterministic", "activities"]
    hit = study.answer(sid, 2, "Deterministic — side effects go in Activities.")
    assert hit["result"] == "passed" and not hit["source_changed"]
    assert study.progress(sid)["attempts"] == 2


def test_a_session_completes_only_when_every_objective_is_met(ws):
    sid = _session(ws)
    study.answer(sid, 1, "@ActivityInterface on the interface")
    assert study.progress(sid)["status"] == "active"
    study.answer(sid, 2, "deterministic; effects in activities")
    p = study.progress(sid)
    assert p["status"] == "complete" and all(o["met"] for o in p["objectives"])


def test_a_source_that_no_longer_says_it_makes_the_answer_unverifiable(ws):
    sid = _session(ws)
    (ws / "docs" / "temporal.md").write_text(DOC.replace("deterministic", "replayable"))
    got = study.answer(sid, 2, "deterministic; effects in activities")
    assert got["result"] == "unverifiable" and "deterministic" in got["why"]
    (ws / "docs" / "temporal.md").unlink()
    assert study.answer(sid, 1, "ActivityInterface")["result"] == "unverifiable"
    assert study.progress(sid)["status"] == "active", "an unverifiable answer meets nothing"


def test_a_changed_source_that_still_supports_the_key_is_graded_and_says_so(ws):
    sid = _session(ws)
    (ws / "docs" / "temporal.md").write_text(DOC + "\nVersioning uses getVersion.\n")
    got = study.answer(sid, 1, "ActivityInterface")
    assert got["result"] == "passed" and got["source_changed"]


def test_a_session_is_never_an_instruction_to_a_brain(ws):
    before = skills.discover()
    _session(ws)
    assert skills.discover() == before
    assert not (skills.SKILLS_DIR / ".candidates").exists() or \
        not any("Temporal" in p.read_text() for p in (skills.SKILLS_DIR / ".candidates").glob("*.md"))


def test_the_tools_say_what_happened(ws):
    started = agent.study_start("Temporal", ["Know how activities are declared"])
    assert started.startswith("Learning session #1: Temporal")
    assert "Not added" in agent.study_exercise(1, 1, "q?", ["CronSchedule"], "booking",
                                               "docs/temporal.md")
    assert "Exercise #1" in agent.study_exercise(1, 1, "What marks it?", ["ActivityInterface"],
                                                 "booking", "docs/temporal.md")
    assert agent.study_answer(1, 1, "no idea").startswith("Not yet. Missing: ActivityInterface")
    assert "session complete" in agent.study_answer(1, 1, "ActivityInterface")
    assert "✓ Know how activities are declared — 1/1" in agent.study_status(1)
    assert "#1 Temporal (complete)" == agent.study_status()
