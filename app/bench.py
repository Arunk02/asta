"""How good is Asta, across everything it claims to do — measured, not felt.

`evals.py` asks one honest question well: is an answer about the booking codebase
right? That is one capability out of nine, and it is the only one with a number.
Every other complaint Arun has raised — token waste, a message that reads wrong, a
plan nobody can follow, a watcher dead for thirteen hours — was found by *him*,
in production, and fixed one at a time. Fourteen cases cannot tell you whether the
next fix made anything better.

This runs a suite of day-to-day scenarios against the REAL code paths and scores
them on four axes at once. It exists so that "is Asta improving" stops being a
feeling and starts being a series.

Three decisions that matter, and the reasons they are not the obvious ones:

  PARTIAL CREDIT, NOT PASS/FAIL. `evals.grade` answers yes-or-no, which is right
  for a regression gate and useless as a reward. A binary score has no gradient:
  a change that fixes three of five missing facts looks identical to one that
  fixes none, so nothing can hill-climb on it. Correctness here is the FRACTION
  of required facts present, and the pass/fail verdict is kept alongside it.

  MOST OF IT COSTS NOTHING. `triage.classify`, `triage.summarize`,
  `writing.fit_address`, `token_audit.detect_waste` and the plan-structure check
  are pure functions. Hundreds of cases over those are free, deterministic, and
  can run in CI on every commit. A brain is used only where one is genuinely
  required, and those cases are marked `live` and skipped unless asked for. This
  is the two-tier split `evals.py` already chose; it is inherited, not reinvented.

  STRUCTURE, NOT A JUDGE. `evals.py` says it plainly — "a judge is another
  opinion to be wrong". A drafted message is scored against `writing.profile()`,
  which already models how Arun actually writes, so "does this sound like him" is
  a measurement rather than a second model's taste.

Writing a LIVE case has one rule that is easy to get wrong, and the first live
run got it wrong twice: **a `must_not` term must be one that only a WRONG answer
can contain.** Asked "which datasource, and why", a perfectly correct answer
names Prometheus in order to rule it out; asked for the matcher it would use
"instead", a correct answer quotes `{namespace=~".+"}` in order to reject it.
Both scored zero while being exactly right. Worse than wrong, they were unstable:
whether the model volunteers the contrast varies between runs, so the case
measured phrasing and flaked. Ask for the answer alone — "one word", "the matcher
only" — and forbid terms a right answer has no reason to say.

The reward is deliberately blunt: correctness dominates, speed and thrift are
tie-breakers, and a safety violation is not a deduction but a floor — a scenario
that sends something it should have staged cannot score well by being fast.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

from . import evals, store

ROOT = Path(__file__).resolve().parent.parent

#: Real scenarios quoting real colleagues, tickets and repo names. Gitignored,
#: exactly like data/evals — see `evals.report` for why that is deliberate.
SCENARIOS_DIR = ROOT / "data" / "scenarios"

#: Anonymised scenarios that CAN be committed, so CI proves the harness itself
#: works on a fresh checkout instead of reporting 0/0 and calling it a pass.
STARTER_DIR = ROOT / "tests" / "scenarios"

#: What Asta claims to do. Every scenario declares exactly one, because a case
#: that measures two capabilities tells you nothing about which one regressed.
CAPABILITIES: dict[str, str] = {
    "triage": "spotting what actually needs him, and how urgently",
    "summarise": "compressing a pile into the part worth reading",
    "analyse": "reading a failure and naming its cause",
    "message": "writing to a colleague in his voice, not a bot's",
    "plan": "a plan he can grasp in thirty seconds",
    "code": "the change itself, and whether it holds up",
    "jira": "reading a ticket and knowing what it actually asks for",
    "context": "using what the workspace already knows",
    "recover": "healing itself without him",
    "meetings": "which meeting he meant, and whether he need be there at all",
    "ask": "not asking him something he has already answered",
    "investigate": "reading a production failure and knowing where to look",
    "burst": "ten people at once — what he gets, and what comes back later",
    "readpath": "the live plumbing: can it actually read Teams and Outlook",
}

#: Weights on the reward. Correctness dominates on purpose: a fast, cheap, wrong
#: answer is worthless, and any weighting that lets speed rescue it is lying.
W_CORRECT = 0.70
W_SPEED = 0.15
W_THRIFT = 0.15

#: A scenario that breaks a safety rule (sends without staging, writes in a chat
#: turn, names a real person in a public artefact) is capped here no matter what
#: else it scored. A cap rather than a subtraction, so it cannot be out-earned.
SAFETY_CAP = 0.20


def load(capability: str = "", include_starter: bool = True) -> list[dict]:
    """Every scenario, real ones first, optionally filtered to one capability.

    A starter case and a real case with the same id is not a conflict — the real
    one wins, because the anonymised twin exists only so CI has something to run.
    """
    seen: dict[str, dict] = {}
    dirs = [SCENARIOS_DIR] + ([STARTER_DIR] if include_starter else [])
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            cap = data.get("capability", f.stem)
            if capability and cap != capability:
                continue
            for case in data.get("cases", []):
                cid = case.get("id")
                if not cid or cid in seen:
                    continue           # first directory wins — real beats starter
                seen[cid] = {**case, "capability": cap, "suite": f.stem}
    return list(seen.values())


def correctness(answer: str, case: dict) -> tuple[float, dict]:
    """How much of what had to be there IS there — 0.0 to 1.0, plus the verdict.

    `evals.grade` does the matching so there is exactly one grader in the system
    and `any_of` / `must_not` keep their meaning. What is added is the fraction,
    because a reward needs a slope and `ok` is a cliff.
    """
    verdict = evals.grade(answer, case)
    required = (len(case.get("must", []))
                + len(case.get("any_of", []) or []))
    if verdict["wrong"] or verdict["empty"]:
        # Saying something forbidden is not partial success. A must_not exists
        # because that answer is actively harmful, not merely incomplete.
        return 0.0, verdict
    if not required:
        return (1.0 if verdict["ok"] else 0.0), verdict
    got = required - len(verdict["missing"])
    return max(0.0, got / required), verdict


def _budget_score(actual: float, budget: float | None) -> float:
    """1.0 at zero cost, 1.0 at exactly budget, decaying after — never negative.

    Deliberately flat up to the budget. Shaving 200ms off something already
    inside its budget is not an improvement worth teaching an optimiser to chase.

    An ABSENT budget and a budget of ZERO are different claims, and collapsing
    them scored every unbudgeted case as a total failure: `{"tokens": 0}` asserts
    "this must cost nothing" and is worth failing over, while declaring no token
    budget at all just means the case is about correctness.
    """
    if budget is None:
        return 1.0
    if budget <= 0:
        return 1.0 if actual <= 0 else 0.0
    if actual <= budget:
        return 1.0
    return max(0.0, budget / actual)


def score(case: dict, obs: dict) -> dict:
    """One scenario's four axes and the single number that trades them off."""
    corr, verdict = correctness(obs.get("text", ""), case)
    budget = case.get("budget", {}) or {}
    sec_budget = budget.get("seconds")
    tok_budget = budget.get("tokens")
    speed = _budget_score(obs.get("seconds", 0.0),
                          None if sec_budget is None else float(sec_budget))
    thrift = _budget_score(obs.get("tokens", 0),
                           None if tok_budget is None else float(tok_budget))
    violations = list(obs.get("violations", []))
    reward = W_CORRECT * corr + W_SPEED * speed + W_THRIFT * thrift
    if violations:
        reward = min(reward, SAFETY_CAP)
    return {
        "id": case["id"],
        "capability": case["capability"],
        "ok": verdict["ok"] and not violations,
        "correctness": round(corr, 3),
        "speed": round(speed, 3),
        "thrift": round(thrift, 3),
        "reward": round(reward, 4),
        "seconds": round(obs.get("seconds", 0.0), 3),
        "tokens": obs.get("tokens", 0),
        "violations": violations,
        "missing": verdict["missing"],
        "wrong": verdict["wrong"],
        "live": bool(case.get("live")),
        "why": case.get("why", ""),
        "source": case.get("source", ""),
        "error": obs.get("error", ""),
    }


async def observe(case: dict) -> dict:
    """Run ONE scenario against the real path and report what happened.

    Never raises. A harness that dies on a broken case measures nothing and hides
    which case broke it — the same silent-failure shape this whole module exists
    to catch.
    """
    from . import runners, sandbox
    fn = runners.for_capability(case["capability"])
    if fn is None:
        return {"text": "", "seconds": 0.0, "tokens": 0,
                "error": f"no runner for capability '{case['capability']}'"}
    started = time.monotonic()
    try:
        obs = await fn(case)
    except sandbox.OutwardMoveBlocked as exc:
        # Not an error — a scenario that tried to reach a person. It must show up
        # as the worst kind of failure there is, not as a flaky case.
        return {"text": "", "seconds": round(time.monotonic() - started, 3),
                "tokens": 0, "violations": [str(exc).split(" was called")[0]]}
    except Exception as exc:                                   # noqa: BLE001
        return {"text": "", "seconds": round(time.monotonic() - started, 3),
                "tokens": 0, "error": f"{type(exc).__name__}: {exc}"[:200]}
    obs.setdefault("seconds", time.monotonic() - started)
    obs.setdefault("tokens", 0)
    obs.setdefault("violations", [])
    return obs


@contextlib.contextmanager
def _isolated_store():
    """Scenarios get their own database. His is read, never written.

    The recovery ladder writes cooldown keys, the ledger writes rows — run
    against the live store, a bench would leave state behind that changes the
    NEXT run's result, and a measurement that moves itself is not a measurement.
    Same rule the test suite already enforces globally in conftest.
    """
    import tempfile
    original = store.DB_PATH
    with tempfile.TemporaryDirectory(prefix="asta-bench-") as tmp:
        store.DB_PATH = Path(tmp) / "bench.db"
        try:
            store.init()
            yield
        finally:
            store.DB_PATH = original


async def run(capability: str = "", live: bool = False,
              include_starter: bool = True, isolate: bool = True,
              concurrency: int = 1) -> dict:
    """Score every scenario. `live=False` skips the ones that cost money.

    `concurrency` > 1 runs scenarios simultaneously. That is a different
    experiment, not a faster version of the same one, and the report says so:
    under contention a case's elapsed time includes waiting for everything it is
    sharing a process with, so the speed axis stops measuring the code and starts
    measuring the load. Correctness and safety stay exactly as meaningful — which
    is the point, because running them together is how a bug that only appears
    when two tasks touch one resource gets found. That class of bug is precisely
    what cost fourteen hours of Teams.
    """
    cases = [c for c in load(capability, include_starter)
             if live or not c.get("live")]
    skipped = len([c for c in load(capability, include_starter) if c.get("live")]) \
        if not live else 0
    started = time.monotonic()
    # Measuring Asta must never place a call, join a meeting or message a
    # colleague. The seal makes that structural rather than a property of the
    # runners happening to be pure today — see app/sandbox.py.
    from . import sandbox
    with contextlib.ExitStack() as stack:
        stack.enter_context(sandbox.sealed())
        if isolate:
            stack.enter_context(_isolated_store())
        if concurrency <= 1:
            results = [score(c, await observe(c)) for c in cases]
        else:
            import asyncio
            gate = asyncio.Semaphore(concurrency)

            async def one(case: dict) -> dict:
                async with gate:
                    return score(case, await observe(case))

            # return_exceptions is deliberately NOT set: `observe` already turns
            # every failure into an observation, so an exception escaping here
            # would be a bug in the harness rather than in a scenario, and it
            # should be loud instead of quietly becoming one bad row.
            results = list(await asyncio.gather(*(one(c) for c in cases)))
    by_cap: dict[str, list[dict]] = {}
    for r in results:
        by_cap.setdefault(r["capability"], []).append(r)
    caps = {
        name: {
            "n": len(rs),
            "passed": sum(1 for r in rs if r["ok"]),
            "reward": round(sum(r["reward"] for r in rs) / len(rs), 4),
            "correctness": round(sum(r["correctness"] for r in rs) / len(rs), 3),
            "seconds": round(sum(r["seconds"] for r in rs), 2),
            "tokens": sum(r["tokens"] for r in rs),
        }
        for name, rs in sorted(by_cap.items())
    }
    out = {
        "total": len(results),
        "passed": sum(1 for r in results if r["ok"]),
        "reward": round(sum(r["reward"] for r in results) / len(results), 4)
        if results else 0.0,
        "seconds": round(time.monotonic() - started, 2),
        "tokens": sum(r["tokens"] for r in results),
        "skipped_live": skipped,
        "concurrency": concurrency,
        "capabilities": caps,
        "results": results,
    }
    if results:
        store.record_outcome(
            "bench", "scored", subject=capability or "all",
            detail=f"reward={out['reward']} {out['passed']}/{out['total']}")
    return out


def report(out: dict) -> str:
    """The scoreboard, then every failure with what it was grounded in."""
    if not out["total"]:
        return ("No scenarios. Real ones live in data/scenarios/ (gitignored — they "
                "quote internal names); the committed starter set is in "
                "tests/scenarios/. Ground each case in something already verified: "
                "a lesson, a pin, or a failure that actually happened.")
    lines = [
        f"Asta capability bench — reward {out['reward']:.3f}  "
        f"({out['passed']}/{out['total']} clean) in {out['seconds']}s, "
        f"{out['tokens']} tokens",
        "",
        f"  {'capability':<12} {'n':>3} {'pass':>5} {'reward':>7} {'correct':>8} {'sec':>6}",
    ]
    for name, c in out["capabilities"].items():
        lines.append(f"  {name:<12} {c['n']:>3} {c['passed']:>5} "
                     f"{c['reward']:>7.3f} {c['correctness']:>8.2f} {c['seconds']:>6.2f}")
    if out.get("skipped_live"):
        lines.append(f"\n  ({out['skipped_live']} live case(s) skipped — pass live=True to spend tokens)")
    if out.get("concurrency", 1) > 1:
        lines.append(f"\n  ⚠ ran {out['concurrency']}-way parallel — correctness and safety hold, "
                     f"but the speed column measured contention, not the code.")
    bad = [r for r in out["results"] if not r["ok"]]
    if bad:
        lines.append("")
        for r in sorted(bad, key=lambda r: r["reward"]):
            why = r["error"] or ""
            if r["violations"]:
                why = "SAFETY: " + ", ".join(r["violations"])
            elif r["wrong"]:
                why = "said " + ", ".join(r["wrong"])
            elif r["missing"]:
                why = "never mentioned " + ", ".join(r["missing"])
            elif r["speed"] < 1.0:
                why = f"over its time budget ({r['seconds']}s)"
            lines.append(f"  ✗ [{r['capability']}] {r['id']}  reward={r['reward']:.2f}"
                         f"  — {why}")
            if r["source"]:
                lines.append(f"        ground truth: {r['source']}")
    return "\n".join(lines)


def trend(limit: int = 20) -> list[dict]:
    """Past bench runs, oldest first — the series that says 'better or worse'."""
    rows = [r for r in store.recent_outcomes(limit * 4) if r.get("kind") == "bench"]
    out = []
    for r in reversed(rows[:limit]):
        detail = r.get("detail", "")
        reward = 0.0
        for part in detail.split():
            if part.startswith("reward="):
                try:
                    reward = float(part.split("=", 1)[1])
                except ValueError:
                    pass
        out.append({"at": r.get("created_at"), "subject": r.get("subject"),
                    "reward": reward, "detail": detail})
    return out


if __name__ == "__main__":                                      # pragma: no cover
    import asyncio
    import sys
    store.init()
    cap, jobs = "", 1
    live = "--live" in sys.argv
    for a in sys.argv[1:]:
        if a.startswith("--jobs="):
            jobs = int(a.split("=", 1)[1])
        elif not a.startswith("-"):
            cap = a
    print(report(asyncio.run(run(cap, live=live, concurrency=jobs))))
