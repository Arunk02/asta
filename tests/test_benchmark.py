"""The acceptance benchmark: a fixed case set whose score cannot flatter itself."""
import asyncio

import pytest
import yaml

from app.workworld import benchmark, scenario as S


def test_the_current_manifest_is_complete_and_every_case_resolves():
    m = benchmark.load()
    cats = {}
    for case in m["cases"]:
        cats[case["category"]] = cats.get(case["category"], 0) + 1
    assert len(cats) == 9 and all(n == 10 for c, n in cats.items() if c != "authority")
    assert "case" in cats, "v3: one case across turns and lanes"
    assert cats["authority"] >= 20
    assert all(c["outcome"].strip() for c in m["cases"]), "every outcome is stated before it runs"
    assert all(c.get("note") for c in m["cases"] if c["executor"] is None), \
        "a missing case says what exists and what does not"


def test_older_versions_still_load_so_their_results_stay_readable():
    assert benchmark.load(benchmark.MANIFEST.parent / "v1.yaml")["version"] == 1
    assert benchmark.load(benchmark.MANIFEST.parent / "v2.yaml")["version"] == 2


def _manifest(tmp_path, change):
    data = yaml.safe_load(benchmark.MANIFEST.read_text())
    change(data)
    path = tmp_path / "v.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.mark.parametrize("change, problem", [
    (lambda d: d["categories"]["coding"].pop(), "coding: 9 cases"),
    (lambda d: d["authority"][0].update(scenario=None), "authority case must have a scenario"),
    (lambda d: d["categories"]["apps"][0].update(scenario="no-such-scenario"), "no scenario named"),
    (lambda d: d["categories"]["apps"][1].update(id=d["categories"]["apps"][0]["id"]), "duplicate"),
    (lambda d: d["categories"]["memory"][0].update(outcome=" "), "no outcome stated"),
    (lambda d: d["categories"]["calls"][1].update(tests=["tests/test_call_voice.py::test_gone"]),
     "no test named"),
    (lambda d: d["categories"]["calls"][1].update(tests=["tests/nope.py::test_x"]), "no test file"),
    (lambda d: d["categories"]["coding"][0].update(tests=["tests/test_call_voice.py::test_the_greeting_is_a_short_line_of_its_own"]),
     "a scenario or tests, not both"),
])
def test_a_malformed_manifest_is_an_error_not_a_smaller_score(tmp_path, change, problem):
    with pytest.raises(ValueError, match=problem):
        benchmark.load(_manifest(tmp_path, change))


def _all_pass(nodes, k):
    return {n: [] for n in nodes}


def _row(cid, category, status, first="passed"):
    return {"id": cid, "category": category, "status": status, "first_attempt": first,
            "failures": ["[run 1] wrong"] if status == "failed" else []}


def test_missing_and_not_run_count_against_the_rate_and_never_as_passed():
    s = benchmark.score([_row("a", "coding", "passed"), _row("b", "coding", "missing"),
                         _row("c", "coding", "not_run"), _row("d", "coding", "failed"),
                         _row("z", "authority", "passed")], version=1, repeats=3, live=False)
    assert (s["verified"], s["defined"], s["verified_rate"]) == (1, 4, 0.25)
    assert s["missing"] == ["b"] and s["not_run"] == ["c"]
    assert s["categories"]["coding"]["failed"] == 1 and s["hard_gate"]["passed"]
    assert not s["target_met"]


def test_one_authority_failure_fails_the_hard_gate_whatever_the_rate():
    s = benchmark.score([_row("a", "coding", "passed"), _row("z", "authority", "failed")],
                        version=1, repeats=3, live=False)
    assert s["verified_rate"] == 1.0 and not s["hard_gate"]["passed"]
    assert s["hard_gate"]["failures"] == ["z"]
    assert "Hard gate FAILED: z" in benchmark.report(s)


def test_a_pass_that_does_not_repeat_is_a_failure(monkeypatch):
    """pass^k: one good run of three is not reliability."""
    async def flaky(sc, k=1, live=False):
        fails = ["[run 2] reply missing"] if sc.id == "every-code-task-stops-for-approval" else []
        return S.Result(scenario=sc, passed=not fails, failures=fails, variants=k)
    monkeypatch.setattr(S, "run_scenario", flaky)
    monkeypatch.setattr(benchmark, "run_tests", _all_pass)
    s = asyncio.run(benchmark.run())
    row = next(o for o in s["cases"] if o["id"] == "code-plan-gate")
    assert row["status"] == "failed" and row["first_attempt"] == "passed" and row["repeats"] == 3
    assert s["categories"]["coding"]["first_attempt"] >= 1


def test_live_cases_run_only_with_live_and_otherwise_are_not_run(monkeypatch):
    seen = []

    async def record(sc, k=1, live=False):
        seen.append((sc.id, live))
        return S.Result(scenario=sc, passed=True, failures=[], variants=k)
    monkeypatch.setattr(S, "run_scenario", record)
    monkeypatch.setattr(benchmark, "run_tests", _all_pass)
    s = asyncio.run(benchmark.run())
    assert not any(live for _, live in seen)
    assert "code-live-graph-build" in s["not_run"]
    seen.clear()
    s = asyncio.run(benchmark.run(live=True, repeats=1))
    assert ("live-the-graph-builds-a-small-change-end-to-end", True) in seen
    assert s["not_run"] == []


def _junit(monkeypatch, cases):
    """pytest, as far as run_tests can see it: one junit report per repeat."""
    import subprocess
    runs = []

    def fake(argv, **kw):
        runs.append(argv)
        report = next(a.split("=", 1)[1] for a in argv if a.startswith("--junitxml="))
        body = "".join(f'<testcase classname="tests.test_x" name="{name}">{inner}</testcase>'
                       for name, inner in cases(len(runs) - 1))
        open(report, "w").write(f"<testsuites><testsuite>{body}</testsuite></testsuites>")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(benchmark.subprocess, "run", fake)
    return runs


def test_a_skipped_or_uncollected_test_verifies_nothing(monkeypatch):
    runs = _junit(monkeypatch, lambda seed: [
        ("test_ok", ""), ("test_param[a]", ""), ("test_param[b]", '<failure message="x"/>' if seed == 2 else ""),
        ("test_skips", '<skipped message="no db"/>')])
    got = benchmark.run_tests(["tests/test_x.py::test_ok", "tests/test_x.py::test_param",
                               "tests/test_x.py::test_skips", "tests/test_x.py::test_absent"], 3)
    assert len(runs) == 3, "one pytest process per repeat"
    assert got["tests/test_x.py::test_ok"] == []
    assert got["tests/test_x.py::test_param"] == ["[run 2] tests/test_x.py::test_param: failure"]
    assert all("skipped" in f for f in got["tests/test_x.py::test_skips"])
    assert all("not collected" in f for f in got["tests/test_x.py::test_absent"])


def test_tests_run_without_his_settings():
    env = benchmark._test_env()
    assert env["ASTA_TOKEN"] == "test-only"
    assert not [k for k in env if k.startswith("ASTA_") and k != "ASTA_TOKEN"]


def test_a_test_case_that_fails_a_repeat_fails(monkeypatch):
    async def ok(sc, k=1, live=False):
        return S.Result(scenario=sc, passed=True, failures=[], variants=k)
    monkeypatch.setattr(S, "run_scenario", ok)
    monkeypatch.setattr(benchmark, "run_tests", lambda nodes, k: {
        n: (["[run 1] boom"] if "greeting" in n else []) for n in nodes})
    s = asyncio.run(benchmark.run())
    row = next(o for o in s["cases"] if o["id"] == "call-greeting-first")
    assert row["executor"] == "tests" and row["status"] == "failed" and row["first_attempt"] == "passed"
    assert next(o for o in s["cases"] if o["id"] == "call-one-voice")["status"] == "passed"
    assert "by tests of the real code" in benchmark.report(s)
