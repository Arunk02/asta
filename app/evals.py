"""Does Asta answer correctly — not merely does the plumbing run.

Sixteen hundred tests prove mechanism. Not one of them asks whether an answer
about the booking codebase is RIGHT, so the most-used capability in the system —
1.3 second median, 76% completion, the thing Arun reaches for most — is the only
one with no measurement at all. "Is it getting better" has been a feeling.

The cases are grounded, never invented. Every expectation traces to something
already verified: `lessons.md` written from a correction Arun made on a real
ticket, or `_pins.yml` recording how a repo actually builds. A case whose ground
truth cannot be pointed at is worse than no case — it measures agreement with a
guess and calls the result quality.

Two tiers, deliberately:

  deterministic  — no brain, no cost, runs in the suite. Classification and
                   routing decisions, where the right answer is a fact.
  live           — asks a real brain a real question and checks the answer for
                   facts that must be in it. Costs money and seconds, so it is
                   run on demand and its score is recorded over time.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import store

CASES_DIR = Path(__file__).resolve().parent.parent / "data" / "evals"


def load(workspace: str = "") -> list[dict]:
    """Every grounded case, optionally for one workspace."""
    cases: list[dict] = []
    for f in sorted(CASES_DIR.glob("*.json")):
        if workspace and f.stem != workspace:
            continue
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        for case in data.get("cases", []):
            cases.append({**case, "suite": f.stem})
    return cases


def _playbook(workspace: str) -> str:
    """The skills a suite declares, concatenated — what a real turn would hold.

    Declared per suite in its JSON (`"skills": ["grafana-analyser"]`) rather than
    hardcoded here, so adding a suite for a new area does not mean editing this
    file too.
    """
    from . import skills as skills_mod
    wanted: list[str] = []
    for f in sorted(CASES_DIR.glob("*.json")):
        if workspace and f.stem != workspace:
            continue
        try:
            wanted += json.loads(f.read_text()).get("skills", []) or []
        except (OSError, ValueError):
            continue
    parts = []
    for name in dict.fromkeys(wanted):
        body = skills_mod.load(name)
        if body:
            parts.append(f"--- {name} ---\n{body}")
    return "\n\n".join(parts)


def grade(answer: str, case: dict) -> dict:
    """Did this answer contain what it had to, and avoid what it must not?

    Substring matching rather than a judge model. A judge is another opinion to
    be wrong; "the answer names TmsServiceImpl" is a fact, and cheap.
    """
    text = (answer or "").lower()
    missing = [m for m in case.get("must", []) if m.lower() not in text]
    # `any_of` is a group where ONE member is enough. Without it a case has to
    # name a single English word, which tests vocabulary rather than correctness:
    # "verify the label first" and "confirm the label first" are the same answer,
    # and a suite that fails one of them is measuring phrasing.
    for group in case.get("any_of", []) or []:
        if not any(str(m).lower() in text for m in group):
            missing.append(" or ".join(str(m) for m in group))
    wrong = [m for m in case.get("must_not", []) if m.lower() in text]
    return {
        "id": case["id"], "ok": not missing and not wrong and bool(text.strip()),
        "missing": missing, "wrong": wrong,
        "empty": not text.strip(),
        "why": case.get("why", ""), "source": case.get("source", ""),
    }


async def run(workspace: str = "booking", ask=None) -> dict:
    """Ask every case and grade the answers. `ask` lets tests supply their own.

    Never raises: an eval run that dies tells him less than one that reports
    which cases it could not reach.
    """
    cases = load(workspace)
    if ask is None:
        from . import meetings                      # the in-call brain: read-only tools
        # A suite may name the playbooks a real turn would already have loaded —
        # the grafana-analyser skill for a logs question. Without them the eval
        # measures a brain working blind, scores it badly, and the number says
        # nothing about the path Arun actually uses.
        playbook = _playbook(workspace)

        async def ask(question: str) -> str:        # noqa: ANN001
            return await meetings.answer_from_knowledge(question, playbook)
    results, started = [], time.monotonic()
    for case in cases:
        try:
            answer = await ask(case["ask"])
        except Exception as exc:                    # noqa: BLE001
            results.append({"id": case["id"], "ok": False, "missing": [],
                            "wrong": [], "empty": True, "why": case.get("why", ""),
                            "source": case.get("source", ""),
                            "error": f"{type(exc).__name__}: {exc}"[:160]})
            continue
        results.append(grade(answer, case))
    passed = sum(1 for r in results if r["ok"])
    out = {"workspace": workspace, "total": len(results), "passed": passed,
           "rate": round(passed / len(results), 3) if results else 0.0,
           "seconds": round(time.monotonic() - started, 1), "results": results}
    if results:
        store.record_outcome("eval", "scored", subject=workspace,
                             detail=f"{passed}/{len(results)} rate={out['rate']}")
    return out


def report(out: dict) -> str:
    """What he would want to read: what is wrong, and what it was grounded in."""
    # No cases is not a score of zero, and it must not read as one. The cases live
    # under data/, which is gitignored — they quote internal repo names, a real
    # ticket id and an internal class name, and that is exactly what the ignore
    # rule is for. So a fresh checkout has none, and saying "0/0 (0%)" would report
    # a measurement that never ran as a measurement that failed.
    if not out["total"]:
        return (f"No eval cases for '{out['workspace']}'. They live in "
                f"data/evals/{out['workspace']}.json, which is gitignored because "
                f"the cases quote internal names — so a fresh checkout has none. "
                f"Ground each case in .contmark/lessons.md or _pins.yml; a case "
                f"whose ground truth cannot be pointed at measures agreement with "
                f"a guess.")
    lines = [f"Answer quality — {out['workspace']}: "
             f"{out['passed']}/{out['total']} ({out['rate']:.0%}) in {out['seconds']}s"]
    for r in out["results"]:
        if r["ok"]:
            continue
        why = "no answer at all" if r.get("empty") else ""
        if r.get("error"):
            why = r["error"]
        elif r["missing"]:
            why = "never mentioned " + ", ".join(r["missing"])
        if r["wrong"]:
            why = (why + "; " if why else "") + "said " + ", ".join(r["wrong"])
        lines.append(f"  ✗ {r['id']}: {why}")
        if r["source"]:
            lines.append(f"      ground truth: {r['source']}")
    return "\n".join(lines)


if __name__ == "__main__":                          # pragma: no cover
    import asyncio
    import sys
    ws = sys.argv[1] if len(sys.argv) > 1 else "booking"
    print(report(asyncio.run(run(ws))))
