"""Running the sets, and writing down what they said.

The result file is the point of contact with everything else: the scorecard
reads it, the nightly job appends to it, and the baseline is simply the first
one. It is a FILE, not a row in Asta's database, because the run happens with
the database patched to a temp file — a runner that wrote its results "to the
database" would write them into the sandbox it just threw away.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from dataclasses import asdict
from pathlib import Path

from . import scenario as S

ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = ROOT / "data" / "workworld"
LAST = OUT_DIR / "last.json"            # the deterministic tier
LAST_LIVE = OUT_DIR / "last-live.json"  # the live tier — its own file, so a
                                        # three-scenario night run cannot
                                        # overwrite the 38-scenario result
HISTORY = OUT_DIR / "runs.jsonl"
ENGINES = ("classic", "graph")          # tasks._worker, or app/graph (ASTA_GRAPH)
BASELINE = OUT_DIR / "baseline.json"


async def run_all(sets: list[str] | None = None, k: int = 1, live: bool = False,
                  only: str = "") -> list[S.Result]:
    out: list[S.Result] = []
    for sc in S.load(sets):
        if only and only not in sc.id:
            continue
        if live != (sc.set == "live"):
            continue            # live scenarios need a real brain; the rest never use one
        out.append(await S.run_scenario(sc, k=k, live=live))
    return out


def engine() -> str:
    """Which task engine this process runs code tasks on."""
    from app.graph import runner as graph_runner
    return "graph" if graph_runner.enabled() else "classic"


def summarise(results: list[S.Result], k: int, live: bool, day_summary: dict | None = None) -> dict:
    sets: dict[str, dict] = {}
    for r in results:
        entry = sets.setdefault(r.scenario.set, {"total": 0, "passed": 0, "known_gaps": 0,
                                                 "gap_closed": 0, "failures": []})
        entry["total"] += 1
        if r.passed:
            entry["passed"] += 1
            if r.gap_marked:
                entry["gap_closed"] += 1
        elif r.gap_marked:
            entry["known_gaps"] += 1
            entry["failures"].append({"id": r.scenario.id, "gap": r.scenario.gap,
                                      "why": r.failures[0][:200] if r.failures else ""})
        else:
            entry["failures"].append({"id": r.scenario.id, "gap": "",
                                      "why": r.failures[0][:200] if r.failures else ""})
    total = sum(s["total"] for s in sets.values())
    passed = sum(s["passed"] for s in sets.values())
    return {
        "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "epoch": time.time(),
        "tier": "live" if live else "deterministic",
        "engine": engine(),
        "k": k,
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 3) if total else None,
        "sets": {name: {kk: vv for kk, vv in s.items() if kk != "failures"}
                 for name, s in sets.items()},
        "failures": [f for s in sets.values() for f in s["failures"]],
        "seconds": round(sum(r.seconds for r in results), 1),
        "day": day_summary or {},
    }


LAST_REPLAYS = OUT_DIR / "last-replays.json"   # his own corrections, replayed


def save(summary: dict, baseline: bool = False, replays: bool = False) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    live = summary.get("tier") == "live"
    if replays:
        # A run of a few replays must not stand in for the whole bench.
        LAST_REPLAYS.write_text(json.dumps(summary, indent=1))
        return LAST_REPLAYS
    target = LAST_LIVE if live else LAST
    target.write_text(json.dumps(summary, indent=1))
    if not live:
        # Per engine as well, so the two can be read side by side while both exist.
        (OUT_DIR / f"last-{summary.get('engine', 'classic')}.json").write_text(
            json.dumps(summary, indent=1))
    with HISTORY.open("a") as fh:
        fh.write(json.dumps(summary) + "\n")
    # The baseline is the deterministic tier's: it is what every later phase
    # has to beat, and a live run is a different measurement.
    if not live and (baseline or not BASELINE.exists()):
        BASELINE.write_text(json.dumps(summary, indent=1))
    return target


def report(summary: dict) -> str:
    """One screen a person can read — what passed, what is a known gap, what broke."""
    lines = [f"WorkWorld · {summary['tier']} · {summary.get('engine', 'classic')} engine "
             f"· pass^{summary['k']} · {summary['at']}",
             f"{summary['passed']}/{summary['total']} scenarios "
             f"({summary['seconds']}s)"]
    for name, s in summary["sets"].items():
        bits = f"  {name:14} {s['passed']}/{s['total']}"
        if s["known_gaps"]:
            bits += f"  · {s['known_gaps']} known gap(s)"
        if s["gap_closed"]:
            bits += f"  · {s['gap_closed']} gap(s) CLOSED — drop the marker"
        lines.append(bits)
    d = summary.get("day") or {}
    if d:
        lines.append(f"  a day in his life: {d['pushes']} pushes · {d['missed']} missed "
                     f"· {d['false_interrupts']} noise interrupts · {d['brain_calls']} brain calls")
    unexpected = [f for f in summary["failures"] if not f["gap"]]
    if unexpected:
        lines.append("  unexpected failures:")
        lines += [f"    ✗ {f['id']}: {f['why']}" for f in unexpected]
    gaps = [f for f in summary["failures"] if f["gap"]]
    if gaps:
        lines.append("  known gaps (each names the phase that closes it):")
        lines += [f"    – {f['id']} [{f['gap']}]" for f in gaps]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m app.workworld")
    ap.add_argument("command", choices=["run", "twin", "day", "baseline", "replays"])
    ap.add_argument("--set", action="append", dest="sets")
    ap.add_argument("--k", type=int, default=1)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--engine", choices=ENGINES, default="",
                    help="run code tasks on this engine (default: whatever ASTA_GRAPH says)")
    args = ap.parse_args(argv)
    if args.engine:
        import os
        os.environ["ASTA_GRAPH"] = "1" if args.engine == "graph" else ""

    if args.command == "twin":
        from . import twin
        print(json.dumps(twin.build_profile(), indent=1))
        return 0
    if args.command == "day":
        from . import day
        print(day.report(asyncio.run(day.run())))
        return 0
    if args.command == "replays":
        from . import replays
        written = replays.from_history(days=60)
        print(f"{len(written)} standing instruction(s) from the last 60 days compiled into replays")
        for line in written:
            print("  " + line)
        args.sets = ["replays"]

    async def everything():
        from . import day as day_mod
        got = await run_all(args.sets, k=args.k, live=args.live, only=args.only)
        # The day rides with the deterministic tier: it is free, and the balance
        # it measures (pushes against misses) is the number he actually feels.
        seen = await day_mod.run() if not (args.live or args.only) else None
        return got, (day_mod.summary(seen) if seen else {})

    results, day_summary = asyncio.run(everything())
    summary = summarise(results, k=args.k, live=args.live, day_summary=day_summary)
    save(summary, baseline=args.command == "baseline", replays=args.command == "replays")
    if not args.quiet:
        print(report(summary))
    return 0 if not [f for f in summary["failures"] if not f["gap"]] else 1
