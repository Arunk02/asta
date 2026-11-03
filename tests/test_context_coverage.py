"""Entry points the context map does not know are found, flagged and fed to the refresh."""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from app import refresh, store

MAIN = "service/src/main/java/com/acme/booking"


def _git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "booking-workspace"
    repo = root / "booking-service"
    files = {
        f"{MAIN}/temporal/activity/MappedActivityImpl.java": "class MappedActivityImpl {}",
        f"{MAIN}/temporal/activity/AmendActivityImpl.java": "class AmendActivityImpl {}",
        f"{MAIN}/temporal/activity/CancelActivityImpl.java": "class CancelActivityImpl {}",
        f"{MAIN}/temporal/workflow/BookingWorkflowImpl.java": "class BookingWorkflowImpl {}",
        f"{MAIN}/kafka/FeedbackConsumer.java": "@KafkaListener(topics=\"x\") class FeedbackConsumer {}",
        f"{MAIN}/api/BookingController.java": "@RestController class BookingController {}",
        f"{MAIN}/service/PlainService.java": "class PlainService {}",
        "service/src/test/java/com/acme/booking/temporal/activity/FakeActivityImpl.java": "class X {}",
    }
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git("init", "-q", cwd=repo); _git("config", "user.email", "t@t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo); _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "x", cwd=repo)
    (repo / MAIN / "temporal/activity/UntrackedActivityImpl.java").write_text("class U {}")
    ctx = root / ".asta-context"
    ctx.mkdir(parents=True)
    (ctx / "_global_index.json").write_text(json.dumps(
        {"files": [{"path": "repos/booking-service/flow.md", "entities": ["MappedActivityImpl"]}]}))

    class WS:
        path = root
        def exists(self):
            return True

    monkeypatch.setattr(refresh.ws_mod, "get", lambda name: WS())
    from app import worktrees
    monkeypatch.setattr(worktrees, "repos_in", lambda r: [repo])
    refresh._coverage_cache.clear()
    return root


def test_unmapped_entry_points_are_found_by_path_and_annotation(ws):
    cov, = refresh.coverage_gap("booking")
    assert cov["total"] == 6 and cov["mapped"] == 1
    assert cov["missing"] == ["AmendActivityImpl", "BookingController", "BookingWorkflowImpl",
                              "CancelActivityImpl", "FeedbackConsumer"]
    assert cov["kinds"]["FeedbackConsumer"] == "kafka listener"
    assert "PlainService" not in cov["missing"], "only entry points, not every class"
    assert "FakeActivityImpl" not in cov["missing"] and "UntrackedActivityImpl" not in cov["missing"]


def test_a_code_task_is_told_the_map_is_thin(ws, monkeypatch):
    monkeypatch.setattr(refresh, "context_gap", lambda w: [])
    note = refresh.trust_note("booking")
    assert "booking-service: 5 of 6 entry points are in no mini-skill" in note
    assert "AmendActivityImpl" in note and "grep the source" in note


def test_a_well_covered_workspace_adds_nothing(ws, monkeypatch):
    monkeypatch.setattr(refresh, "context_gap", lambda w: [])
    monkeypatch.setattr(refresh, "COVERAGE_MIN_MISSING", 6)
    assert refresh.trust_note("booking") == ""


def test_the_refresh_is_asked_for_the_missing_classes_and_reports_what_it_added(ws, monkeypatch):
    monkeypatch.setattr(refresh, "context_gap", lambda w: [])
    seen = {}

    async def brain(prompt, cwd, timeout, effort):
        seen["prompt"] = prompt
        idx = Path(cwd) / ".asta-context" / "_global_index.json"
        idx.write_text(json.dumps({"files": [{"path": "repos/booking-service/flow.md",
                                              "entities": ["MappedActivityImpl", "AmendActivityImpl",
                                                           "CancelActivityImpl"]}]}))
        return "added two activities"

    async def _notify(*a, **k):
        pass

    monkeypatch.setattr("app.claude_cli.one_shot", brain)
    monkeypatch.setattr(refresh.notify, "notify", _notify)
    out = asyncio.run(refresh.enrich("booking", "DRIFT none"))
    assert "COVERAGE" in seen["prompt"] and "AmendActivityImpl (temporal activity)" in seen["prompt"]
    assert "booking-service: entry points mapped 1/6 → 3/6" in out
    prov = json.loads(store.kv_get("context_provenance:booking"))
    assert prov["coverage"]["booking-service"] == {"before": "1/6", "after": "3/6"}


def test_a_broken_context_never_breaks_the_trust_note(ws, monkeypatch):
    monkeypatch.setattr(refresh, "context_gap", lambda w: [])
    (ws / ".asta-context" / "_global_index.json").write_text("{not json")
    refresh._coverage_cache.clear()
    assert "6 of 6 entry points" in refresh.trust_note("booking")
