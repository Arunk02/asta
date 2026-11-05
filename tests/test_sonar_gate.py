"""A red quality gate is read from Sonar, not guessed (5 Nov, #521).

The CI log said only "Quality Gate has FAILED" and a dashboard link; the fix
round wrote tests twice for a gate that was not failing on coverage. Synthetic:
the host, project, files and rules are made up.
"""
from __future__ import annotations

import asyncio

import pytest

from app import sonar, store, tasks

LOG = ("✖ Quality Gate has FAILED.\nDetailed information can be found at: "
       "https://sonar.example.test/dashboard?id=svc-alpha&pullRequest=42")


def test_the_dashboard_link_names_the_gate(monkeypatch):
    monkeypatch.delenv("ASTA_SONAR_URL", raising=False)
    assert sonar.target(LOG) == {"host": "https://sonar.example.test", "project": "svc-alpha",
                                 "pullRequest": "42"}
    assert sonar.target("no link here") == {}


class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


class _Client:
    def __init__(self):
        self.calls = []

    async def get(self, url, params=None):
        self.calls.append((url, params))
        if url.endswith("/api/qualitygates/project_status"):
            return _Resp({"projectStatus": {"status": "ERROR", "conditions": [
                {"status": "ERROR", "metricKey": "new_coverage", "actualValue": "61.0",
                 "comparator": "LT", "errorThreshold": "80"},
                {"status": "OK", "metricKey": "new_bugs", "actualValue": "0"}]}})
        if url.endswith("/api/issues/search"):
            return _Resp({"issues": [{"severity": "MAJOR", "rule": "java:S2629",
                                      "component": "svc-alpha:src/Mapper.java", "line": 77,
                                      "message": "Invoke method(s) only conditionally."}]})
        return _Resp({"components": [
            {"path": "src/Mapper.java", "measures": [
                {"metric": "new_uncovered_lines", "period": {"value": "4"}},
                {"metric": "new_coverage", "period": {"value": "61.0"}}]},
            {"path": "src/Other.java", "measures": [
                {"metric": "new_uncovered_lines", "period": {"value": "0"}}]}]})


def test_the_report_says_what_failed_and_where(monkeypatch):
    monkeypatch.setenv("ASTA_SONAR_TOKEN", "x")
    client = _Client()
    rep = asyncio.run(sonar.report(sonar.target(LOG), client=client))
    text = sonar.render(rep)
    assert "new_coverage: 61.0 (must be LT 80)" in text
    assert "java:S2629 src/Mapper.java:77" in text
    assert "src/Mapper.java: 4 uncovered" in text and "Other.java" not in text
    assert all(p.get("pullRequest") == "42" for _, p in client.calls), "scoped to the PR"


def test_no_token_reads_nothing(monkeypatch):
    monkeypatch.delenv("ASTA_SONAR_TOKEN", raising=False)
    assert not sonar.configured()
    assert asyncio.run(sonar.report(sonar.target(LOG), client=_Client())) == {}


def test_with_a_token_the_fix_round_plans_from_the_gate(monkeypatch):
    monkeypatch.setenv("ASTA_SONAR_TOKEN", "x")
    t = store.create_task("Skip the call", "code", "p", "ws")
    store.update_task(t["id"], status="pr_ci_failed")

    async def ci(task, feedback):
        return LOG
    monkeypatch.setattr(tasks, "_ci_failure_for", ci)
    monkeypatch.setattr(tasks, "ensure_checkout", lambda tid: asyncio.sleep(0))

    async def report(target, client=None):
        return {"status": "ERROR", "failed": [{"metricKey": "new_coverage", "actualValue": "61",
                                               "comparator": "LT", "errorThreshold": "80"}],
                "issues": [], "uncovered": []}
    monkeypatch.setattr(sonar, "report", report)
    briefs = []

    async def worker(task_id, prompt):
        briefs.append(prompt)
    monkeypatch.setattr(tasks, "_propose_worker", worker)

    async def go():
        await tasks.propose_change(t["id"], tasks.CI_FIX_BRIEF)
        await asyncio.sleep(0)
    asyncio.run(go())
    assert briefs and "new_coverage: 61 (must be LT 80)" in briefs[0], \
        "the plan is briefed with the gate's own conditions"


def test_without_a_token_the_round_still_stops_and_says_what_it_needs(monkeypatch):
    monkeypatch.delenv("ASTA_SONAR_TOKEN", raising=False)
    t = store.create_task("Skip the call", "code", "p", "ws")
    store.update_task(t["id"], status="pr_ci_failed")

    async def ci(task, feedback):
        return LOG
    monkeypatch.setattr(tasks, "_ci_failure_for", ci)
    monkeypatch.setattr(tasks, "ensure_checkout", lambda tid: asyncio.sleep(0))
    with pytest.raises(tasks.QualityGateOnly):
        asyncio.run(tasks.propose_change(t["id"], tasks.CI_FIX_BRIEF))
    assert "ASTA_SONAR_TOKEN" in sonar.NEEDS_TOKEN
