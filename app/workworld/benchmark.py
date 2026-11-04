"""The versioned acceptance benchmark: a fixed case set, scored per category.

The scenario bench answers "does each behaviour still hold"; this answers the
roadmap's question, "how much of what he needs is verified to work", over a
case set that does not move between runs (tests/workworld/benchmark/v1.yaml).

A case is verified one of two ways: a WorkWorld scenario (the real dispatcher over
a fake world), or named tests of the real code where the behaviour lives below the
dispatcher (the call engine, memory correction). Both run every repeat.

What it will not do, because each would make the number lie:
  - count a case with nothing to verify it as anything but MISSING;
  - count a test that was skipped or not collected as verified;
  - count a case that passed once and failed on a repeat as passed;
  - run a live case without --live, or call a not-run live case a pass;
  - report a completion rate without the hard gate beside it.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from . import runner, scenario as S

ROOT = Path(__file__).resolve().parent.parent.parent
#: The current set. Older versions stay beside it; their results are never compared.
MANIFEST = ROOT / "tests" / "workworld" / "benchmark" / "v2.yaml"
CATEGORY_SIZE = 10
MIN_AUTHORITY = 20
#: The proposed capability target. A target, not a result.
TARGET = 0.90


def load(path: Path = MANIFEST) -> dict:
    """The manifest, validated: a malformed set is an error, not a smaller score."""
    data = yaml.safe_load(path.read_text())
    cats, auth = data.get("categories") or {}, data.get("authority") or []
    problems = [f"{name}: {len(cases)} cases, not {CATEGORY_SIZE}"
                for name, cases in cats.items() if len(cases) != CATEGORY_SIZE]
    if len(cats) != 7:
        problems.append(f"{len(cats)} categories, not 7")
    if len(auth) < MIN_AUTHORITY:
        problems.append(f"{len(auth)} authority cases, fewer than {MIN_AUTHORITY}")
    cases = [dict(c, category=name) for name, cs in cats.items() for c in cs] + \
            [dict(c, category="authority") for c in auth]
    ids = [c["id"] for c in cases]
    problems += [f"duplicate case id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    problems += [f"{c['id']}: no outcome stated" for c in cases if not str(c.get("outcome", "")).strip()]
    problems += [f"{c['id']}: an authority case must have a scenario"
                 for c in cases if c["category"] == "authority" and not c.get("scenario")]
    problems += [f"{c['id']}: a scenario or tests, not both"
                 for c in cases if c.get("scenario") and c.get("tests")]
    known = {sc.id: sc for sc in S.load(None, directory=S.SCENARIO_DIR)}
    problems += [f"{c['id']}: no scenario named {c['scenario']}"
                 for c in cases if c.get("scenario") and c["scenario"] not in known]
    problems += [f"{c['id']}: {why}" for c in cases for why in map(_node_problem, c.get("tests") or [])
                 if why]
    if problems:
        raise ValueError("benchmark manifest invalid: " + "; ".join(problems))
    for c in cases:
        c["live"] = bool(c.get("scenario")) and known[c["scenario"]].set == "live"
        c["executor"] = "scenario" if c.get("scenario") else "tests" if c.get("tests") else None
    return {"version": data["version"], "repeats": int(data.get("repeats", 3)),
            "cases": cases, "scenarios": known}


def _node_problem(node: str) -> str:
    """A named test must exist where it says, so a rename cannot quietly pass a case."""
    path, _, name = str(node).partition("::")
    file = ROOT / path
    if not (name and path.startswith("tests/") and file.is_file()):
        return f"no test file for {node}"
    text = file.read_text(errors="replace")
    if f"def {name}(" not in text:
        return f"no test named {node}"
    return ""


def _test_env() -> dict:
    """What the frozen suite runs under: nothing from his .env reaches a test."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", ""),
            "TMPDIR": tempfile.gettempdir(), "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1", "ASTA_TOKEN": "test-only"}


def run_tests(nodes: list[str], k: int) -> dict[str, list[str]]:
    """Each node's failures per repeat, in one pytest process per repeat.

    A node that was skipped, or never collected, fails: it verified nothing.
    """
    out: dict[str, list[str]] = {n: [] for n in nodes}
    for seed in range(k):
        with tempfile.TemporaryDirectory(prefix="asta-bench-") as tmp:
            report = Path(tmp) / "junit.xml"
            subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                            f"--junitxml={report}", *nodes], cwd=ROOT, env=_test_env(),
                           capture_output=True, text=True, timeout=1800)
            cases = ET.parse(report).getroot().iter("testcase") if report.is_file() else []
            seen: dict[str, list[str]] = {}
            for tc in cases:
                module = tc.get("classname", "").replace(".", "/")
                name = tc.get("name", "").split("[", 1)[0]
                bad = [el.tag for el in tc if el.tag in ("failure", "error", "skipped")]
                seen.setdefault(f"{module}.py::{name}", []).extend(bad)
        for node in nodes:
            if node not in seen:
                out[node].append(f"[run {seed}] {node}: not collected")
            elif seen[node]:
                out[node].append(f"[run {seed}] {node}: {', '.join(sorted(set(seen[node])))}")
    return out


async def run(live: bool = False, repeats: int | None = None, path: Path = MANIFEST) -> dict:
    m = load(path)
    k = repeats or m["repeats"]
    nodes = sorted({n for c in m["cases"] for n in c.get("tests") or []})
    tested = await asyncio.to_thread(run_tests, nodes, k) if nodes else {}
    outcomes = []
    for case in m["cases"]:
        row = {"id": case["id"], "category": case["category"], "scenario": case.get("scenario"),
               "tests": case.get("tests") or [], "executor": case["executor"],
               "live": case["live"], "outcome": case["outcome"], "note": case.get("note", "")}
        if case["executor"] == "tests":
            failures = [f for n in case["tests"] for f in tested[n]]
            row.update(status="failed" if failures else "passed",
                       first_attempt="failed" if any(f.startswith("[run 0]") for f in failures)
                       else "passed", repeats=k, failures=failures[:3])
        elif not case.get("scenario"):
            row["status"] = "missing"
        elif case["live"] and not live:
            row["status"] = "not_run"
        else:
            result = await S.run_scenario(m["scenarios"][case["scenario"]], k=k, live=case["live"])
            first_failed = any(f.startswith("[run 0]") for f in result.failures)
            row.update(status="passed" if result.passed else "failed",
                       first_attempt="failed" if first_failed else "passed",
                       repeats=k, seconds=round(result.seconds, 2),
                       failures=[f[:200] for f in result.failures[:3]])
        outcomes.append(row)
    return score(outcomes, version=m["version"], repeats=k, live=live)


def score(outcomes: list[dict], *, version: int, repeats: int, live: bool) -> dict:
    """Per-category verified completion over DEFINED cases: missing and not-run count against."""
    cats: dict[str, dict] = {}
    for o in outcomes:
        c = cats.setdefault(o["category"], {"defined": 0, "passed": 0, "failed": 0,
                                            "missing": 0, "not_run": 0, "first_attempt": 0})
        c["defined"] += 1
        c[o["status"]] += 1
        if o.get("first_attempt") == "passed":
            c["first_attempt"] += 1
    for c in cats.values():
        c["verified_rate"] = round(c["passed"] / c["defined"], 3)
    auth = cats.get("authority", {})
    gate_failures = [o["id"] for o in outcomes
                     if o["category"] == "authority" and o["status"] != "passed"]
    work = [o for o in outcomes if o["category"] != "authority"]
    passed = sum(o["status"] == "passed" for o in work)
    return {"benchmark_version": version, "repeats": repeats,
            "tier": "live+deterministic" if live else "deterministic",
            "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "epoch": time.time(),
            "engine": runner.engine(),
            "verified": passed, "defined": len(work),
            "verified_rate": round(passed / len(work), 3) if work else None,
            "target": TARGET, "target_met": bool(work) and passed / len(work) >= TARGET,
            "hard_gate": {"passed": not gate_failures and bool(auth), "failures": gate_failures},
            "categories": cats,
            "missing": [o["id"] for o in outcomes if o["status"] == "missing"],
            "not_run": [o["id"] for o in outcomes if o["status"] == "not_run"],
            "cases": outcomes,
            "not_measured": ["latency per task", "tokens per verified task", "human interventions",
                             "real-world tasks: these are WorkWorld scenarios over a fake world"]}


def report(s: dict) -> str:
    gate = "PASSED" if s["hard_gate"]["passed"] else "FAILED: " + ", ".join(s["hard_gate"]["failures"])
    lines = [f"Benchmark v{s['benchmark_version']} · {s['tier']} · pass^{s['repeats']} · {s['at']}",
             f"Verified {s['verified']}/{s['defined']} ({s['verified_rate']:.0%}); "
             f"target {s['target']:.0%} {'met' if s['target_met'] else 'not met'}. Hard gate {gate}."]
    for name, c in s["categories"].items():
        extra = "".join(f" · {c[k]} {k.replace('_', ' ')}" for k in ("failed", "missing", "not_run") if c[k])
        lines.append(f"  {name:14} {c['passed']}/{c['defined']}{extra}")
    failed = [o for o in s["cases"] if o["status"] == "failed"]
    if failed:
        lines.append("  failed:")
        lines += [f"    ✗ {o['id']}: {(o['failures'] or [''])[0]}" for o in failed]
    if s["missing"]:
        lines.append(f"  missing coverage ({len(s['missing'])}):")
        lines += [f"    – {o['id']}" + (f": {o['note']}" if o.get("note") else "")
                  for o in s["cases"] if o["status"] == "missing"]
    by = {e: sum(o.get("executor") == e and o["status"] == "passed" for o in s["cases"])
          for e in ("scenario", "tests")}
    lines.append(f"  verified by: {by['scenario']} scenarios, {by['tests']} by tests of the real code")
    lines.append("  not measured: " + "; ".join(s["not_measured"]))
    return "\n".join(lines)


def _out(version: int) -> Path:
    return runner.OUT_DIR / f"benchmark-v{version}.json"


def save(s: dict) -> Path:
    runner.OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = _out(s["benchmark_version"])
    out.write_text(json.dumps(s, indent=1))
    with (runner.OUT_DIR / "benchmark-runs.jsonl").open("a") as fh:
        fh.write(json.dumps({k: v for k, v in s.items() if k != "cases"}) + "\n")
    return out


def main(live: bool = False, repeats: int | None = None, save_it: bool = True) -> int:
    s = asyncio.run(run(live=live, repeats=repeats))
    if save_it:
        save(s)
    print(report(s))
    return 0 if s["hard_gate"]["passed"] else 1
