"""Did the work land? — the measurement Asta was missing.

`token_audit.py` measures what a turn COST. Nothing measured whether it was any
good, which makes "improve the assistant" a feeling rather than a number. These
are the cheapest honest signals available, and every one of them is a decision
Arun already makes:

  plan     approved as-is, or sent back to re-plan       → planning quality
  task     done / failed / rejected                      → did it finish
  draft    sent unedited                                 → drafting quality
  ship     PR opened                                     → work that reached a PR
  ask      answered, or timed out                        → were the questions worth asking
  skill    written                                       → is the learning loop producing
  verify   passed its own check, or parked unresolved    → did the work clear the bar

Deliberately not an LLM judge: these are facts Asta already observes, so they
cost nothing to record and cannot be flattered. The verify signal is the newest
and the hardest of all to fake — its "good" outcome is a subprocess exit code, not
a claim — so a rate that climbs is the loop genuinely converging.
"""

from __future__ import annotations

import re
import time

from . import store

#: kind -> (outcome that counts as good, human label)
GOOD = {
    "plan": ("approved", "plans approved as-is"),
    "task": ("done", "tasks finished"),
    "draft": ("sent_unedited", "drafts sent unedited"),
    "ask": ("answered", "questions answered"),
    "verify": ("passed", "passed their own check"),
}

LABEL = {
    "plan": "Planning", "task": "Tasks", "draft": "Drafts",
    "ask": "Questions", "ship": "Shipping", "skill": "Learning",
    "verify": "Verification",
}


def summary(days: int = 7) -> dict:
    """Per-kind totals and a success rate where one is meaningful."""
    since = time.time() - days * 86400
    rows = store.outcome_counts(since)
    by_kind: dict[str, dict[str, int]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], {})[r["outcome"]] = r["n"]
    out: dict[str, dict] = {}
    for kind, counts in by_kind.items():
        total = sum(counts.values())
        entry = {"total": total, "counts": counts}
        good = GOOD.get(kind)
        if good and total:
            entry["rate"] = round(counts.get(good[0], 0) / total, 2)
            entry["measures"] = good[1]
        out[kind] = entry
    return {"days": days, "kinds": out}


def verify_convergence(days: int = 7) -> dict:
    """Average fix-rounds a code task needed to reach a passing check, over the
    window — the number that shows the loop LEARNING rather than merely passing.

    The pass-rate says work clears the bar; this says how HARD it was to get there.
    As skills accumulate, this should fall even while the pass-rate holds or climbs
    (more tasks passing on the first try). Empty when nothing has passed yet."""
    since = time.time() - days * 86400
    rounds: list[int] = []
    for r in store.recent_outcomes(500):
        if r.get("kind") != "verify" or r.get("outcome") != "passed":
            continue
        if float(r.get("created_at") or 0) < since:
            continue
        m = re.search(r"fix_rounds=(\d+)", r.get("detail") or "")
        if m:
            rounds.append(int(m.group(1)))
    if not rounds:
        return {}
    return {"passed": len(rounds),
            "avg_fix_rounds": round(sum(rounds) / len(rounds), 2),
            "first_try": sum(1 for n in rounds if n == 0)}


def report(days: int = 7) -> str:
    """The same thing as text, for chat and the morning brief."""
    data = summary(days)
    kinds = data["kinds"]
    if not kinds:
        return (f"No outcomes recorded in the last {days} days — nothing has finished, "
                f"or this is running before the first measured task.")
    lines = [f"Quality, last {days} days:"]
    for kind in ("plan", "task", "draft", "ask", "ship", "skill", "verify"):
        entry = kinds.get(kind)
        if not entry:
            continue
        label = LABEL.get(kind, kind)
        detail = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in
                           sorted(entry["counts"].items(), key=lambda kv: -kv[1]))
        if "rate" in entry:
            lines.append(f"  {label}: {int(entry['rate'] * 100)}% {entry['measures']} "
                         f"({detail})")
        else:
            lines.append(f"  {label}: {detail}")
        if kind == "verify":
            conv = verify_convergence(days)
            if conv:
                lines.append(f"    → avg {conv['avg_fix_rounds']} fix-round(s) to green, "
                             f"{conv['first_try']}/{conv['passed']} first-try")
    unknown = set(kinds) - set(LABEL) - {"verify_round"}   # internal fix-round telemetry
    for kind in sorted(unknown):
        lines.append(f"  {kind}: " + ", ".join(f"{k} {v}" for k, v in kinds[kind]["counts"].items()))
    return "\n".join(lines)
